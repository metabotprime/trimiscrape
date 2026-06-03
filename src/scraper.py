"""
Per-hashtag scrape loop. Loads a hashtag page, pre-filters candidates by
follower count and brand skiplist, then parallel-loads profiles to extract
emails. Pushes qualifying records to the Apify dataset as it goes (no big
batched write at the end → results survive a mid-run abort).
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Set

from apify import Actor

from .helpers import (
    EXTRACT_EMAILS_JS,
    EXTRACT_PROFILE_JS,
    EXTRACT_USERS_WITH_STATS_JS,
    DEEP_SCRAPE_PATHS,
    DomainRateLimiter,
    age_score,
    extract_email_from_bio,
    extract_instagram_handle,
    extract_scrapeable_links,
    get_base_url,
    is_good_email,
    is_likely_us,
    is_link_in_bio_service,
)


# Shared across all process_hashtag calls so concurrent link-in-bio scrapes
# stay polite per-host across the whole actor run.
_LINK_RATE_LIMITER = DomainRateLimiter(min_gap_seconds=1.5, jitter=0.7)


async def process_hashtag(
    context,
    profile_tabs: list,
    link_tabs: list,
    hashtag: str,
    input_data: dict,
    seen_usernames: Set[str],
) -> int:
    """Scrape one hashtag end-to-end. Returns the count of records pushed."""
    hashtag = (hashtag or '').strip().lstrip('#')
    if not hashtag:
        return 0

    main_page = await context.new_page()
    try:
        url = f'https://www.tiktok.com/tag/{hashtag.replace(" ", "")}'
        try:
            await main_page.goto(url, wait_until='domcontentloaded', timeout=20000)
        except Exception as e:
            Actor.log.warning(f'#{hashtag}: navigation failed ({type(e).__name__})')
            return 0

        # Scroll to surface more creators. Adaptive: keep going while the page
        # is still appending. 15 wheels reliably loads 30–80 profiles on top
        # hashtags; more than that hits diminishing returns.
        for _ in range(15):
            await main_page.mouse.wheel(0, random.randint(1000, 2000))
            await asyncio.sleep(random.uniform(0.4, 0.9))

        try:
            candidates = await main_page.evaluate(EXTRACT_USERS_WITH_STATS_JS)
        except Exception as e:
            Actor.log.warning(f'#{hashtag}: extractor failed ({type(e).__name__})')
            return 0
    finally:
        await main_page.close()

    if not candidates:
        Actor.log.info(f'#{hashtag}: no creators surfaced')
        return 0

    # Pre-filter by follower count + brand skiplist + dedup across run
    min_fol = int(input_data.get('minFollowers', 1500))
    max_fol = int(input_data.get('maxFollowers', 150000))
    brand_skip = {s.lower() for s in (input_data.get('brandSkiplist') or [])}
    max_profiles = int(input_data.get('maxProfilesPerHashtag', 200))

    pre_filtered = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        uname = (c.get('username') or '').lower()
        if not uname or uname in seen_usernames or uname in brand_skip:
            continue
        seen_usernames.add(uname)
        fcount = int(c.get('followers') or 0)
        if fcount > 0 and (fcount < min_fol or fcount > max_fol):
            continue
        pre_filtered.append(uname)
        if len(pre_filtered) >= max_profiles:
            break

    Actor.log.info(f'#{hashtag}: {len(pre_filtered)} candidates after pre-filter')
    if not pre_filtered:
        return 0

    early_exit_threshold = int(input_data.get('earlyExitNoEmailThreshold', 12))
    scrape_link_in_bio = bool(input_data.get('scrapeLinkInBio', True))
    us_only = bool(input_data.get('usOnly', True))
    skip_ghost = bool(input_data.get('skipGhostAccounts', True))

    n_tabs = len(profile_tabs)
    pushed = 0
    profiles_checked = 0

    for batch_start in range(0, len(pre_filtered), n_tabs):
        if profiles_checked >= early_exit_threshold and pushed == 0:
            Actor.log.info(
                f'#{hashtag}: early-exit ({profiles_checked} profiles, 0 emails)'
            )
            break

        batch = pre_filtered[batch_start:batch_start + n_tabs]
        coros = [
            _process_profile(
                profile_tabs[i], batch[i], hashtag, input_data, link_tabs,
                scrape_link_in_bio, us_only, skip_ghost,
                min_fol, max_fol,
            )
            for i in range(len(batch))
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for r in results:
            profiles_checked += 1
            if isinstance(r, Exception):
                Actor.log.debug(f'profile error: {type(r).__name__}: {r}')
                continue
            if not r:
                continue
            try:
                await Actor.push_data(r)
                pushed += 1
            except Exception as e:
                Actor.log.warning(f'push_data failed: {type(e).__name__}: {e}')

    return pushed


async def _process_profile(
    tab,
    username: str,
    hashtag: str,
    input_data: dict,
    link_tabs: list,
    scrape_link_in_bio: bool,
    us_only: bool,
    skip_ghost: bool,
    min_fol: int,
    max_fol: int,
):
    """Load a single profile, run extractor + filters, return record dict or None."""
    url = f'https://www.tiktok.com/@{username}'
    # One retry on transient errors (proxy hiccups, slow first byte). Two
    # tries cover ~95% of non-blocked TikTok responses with negligible cost.
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            await tab.goto(url, wait_until='domcontentloaded', timeout=15000)
            last_err = None
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                await asyncio.sleep(2.0 + random.uniform(0, 1.5))
    if last_err is not None:
        return None

    # Some profiles need a beat for SIGI_STATE to inject. Retry once.
    profile = None
    try:
        profile = await tab.evaluate(EXTRACT_PROFILE_JS)
    except Exception:
        profile = None

    if not profile or not profile.get('username'):
        await asyncio.sleep(0.6)
        try:
            profile = await tab.evaluate(EXTRACT_PROFILE_JS)
        except Exception:
            return None

    if not profile or not profile.get('username'):
        return None

    followers = int(profile.get('followers') or 0)
    total_likes = int(profile.get('likes') or 0)
    bio = profile.get('bio') or ''

    # Follower band (also a check after the fact in case the hashtag page was stale)
    if followers > 0 and (followers < min_fol or followers > max_fol):
        return None

    if skip_ghost and followers >= 5000 and total_likes == 0:
        return None

    if us_only and not is_likely_us(profile):
        return None

    # Email — bio first (fast, no extra page visit)
    email = extract_email_from_bio(bio)
    email_source = 'bio' if email else ''

    # Fallback — link-in-bio
    if not email and scrape_link_in_bio:
        bio_link = profile.get('bioLink') or ''
        urls_to_try = []
        if bio_link:
            urls_to_try.append(bio_link)
        urls_to_try.extend(extract_scrapeable_links(bio)[:2])

        for ix, link_url in enumerate(urls_to_try):
            link_tab = link_tabs[ix % len(link_tabs)] if link_tabs else None
            if not link_tab:
                break
            email = await _scrape_link_for_email(link_tab, link_url)
            if email:
                email_source = 'biolink'
                break

    if not email:
        return None

    # Final defensive validation
    if not is_good_email(email):
        return None

    return {
        'username': profile.get('username'),
        'displayName': profile.get('display_name', ''),
        'bio': bio,
        'email': email,
        'emailSource': email_source,
        'followers': followers,
        'totalLikes': total_likes,
        'region': profile.get('region', ''),
        'ageScore': age_score(profile),
        'instagramHandle': extract_instagram_handle(bio),
        'hashtag': hashtag,
        'profileUrl': f'https://tiktok.com/@{profile.get("username")}',
        'source': 'trimi_custom',
        'scrapedAt': datetime.now(timezone.utc).isoformat(),
    }


async def _scrape_link_for_email(tab, url: str) -> str:
    """Visit a link-in-bio / personal site, extract first deliverable email.
    Honors a per-domain rate limit so concurrent tabs don't hammer any
    single host (mainly relevant for linktr.ee/beacons.ai which see most
    of the traffic)."""
    try:
        await _LINK_RATE_LIMITER.wait_for(url)
        await tab.goto(url, wait_until='domcontentloaded', timeout=10000)
        await asyncio.sleep(1.2)
        emails = await tab.evaluate(EXTRACT_EMAILS_JS)
        for e in emails or []:
            if is_good_email(e):
                return e

        # Deep scrape — only personal sites (not link-in-bio services)
        if not is_link_in_bio_service(url):
            base = get_base_url(url)
            if base:
                for sub in DEEP_SCRAPE_PATHS:
                    try:
                        sub_url = base + sub
                        await _LINK_RATE_LIMITER.wait_for(sub_url)
                        await tab.goto(sub_url, wait_until='domcontentloaded', timeout=8000)
                        await asyncio.sleep(0.7)
                        sub_emails = await tab.evaluate(EXTRACT_EMAILS_JS)
                        for e in sub_emails or []:
                            if is_good_email(e):
                                return e
                    except Exception:
                        continue
        return ''
    except Exception:
        return ''
