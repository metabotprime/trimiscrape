"""
Per-hashtag scrape loop — FEED-FIRST.

Loads a hashtag page, extracts each creator's full bio (signature), stats and
bioLink straight from the feed JSON, and pulls emails from those bios WITHOUT
visiting individual profile pages. Only creators whose bio has a link but no
inline email get a network visit (to scrape the linktree). This collapses
~1000 profile loads per run into ~25 hashtag-page loads, cutting residential-
proxy bandwidth (the dominant cost) by ~85%.

Pushes qualifying records to the Apify dataset as it goes (no big batched write
at the end → results survive a mid-run abort).
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Set

from apify import Actor

from .helpers import (
    EXTRACT_EMAILS_JS,
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
    link_tabs: list,
    hashtag: str,
    input_data: dict,
    seen_usernames: Set[str],
) -> int:
    """Scrape one hashtag end-to-end. Returns the count of records pushed."""
    hashtag = (hashtag or '').strip().lstrip('#')
    if not hashtag:
        return 0

    # Collect creators by intercepting the hashtag feed API responses. When
    # you scroll, TikTok loads more videos via XHR (/api/challenge/item_list,
    # etc.) — those JSON bodies carry each author's full signature (bio),
    # stats and bioLink. Reading them as they stream in captures EVERYTHING
    # the scroll surfaces (the script-tag JSON only holds the initial ~30).
    collected: dict = {}

    async def _on_response(response):
        u = response.url
        if not any(p in u for p in ('/api/challenge/item_list', '/api/search/',
                                    '/api/post/item_list', '/api/related/item_list')):
            return
        try:
            data = await response.json()
        except Exception:
            return
        for item in (data.get('itemList') or []):
            author = item.get('author') or {}
            stats = item.get('authorStats') or {}
            uid = (author.get('uniqueId') or '').lower()
            if uid and uid not in collected:
                collected[uid] = {
                    'username': uid,
                    'nickname': author.get('nickname', ''),
                    'bio': author.get('signature', ''),
                    'region': author.get('region', ''),
                    'bioLink': (author.get('bioLink') or {}).get('link', ''),
                    'followers': (stats or {}).get('followerCount', 0),
                    'likes': (stats or {}).get('heartCount', 0),
                }

    main_page = await context.new_page()
    main_page.on('response', _on_response)
    try:
        url = f'https://www.tiktok.com/tag/{hashtag.replace(" ", "")}'
        try:
            await main_page.goto(url, wait_until='domcontentloaded', timeout=20000)
        except Exception as e:
            Actor.log.warning(f'#{hashtag}: navigation failed ({type(e).__name__})')
            return 0

        # Scroll to trigger the feed API calls. 15 wheels reliably surfaces
        # 30–80 creators on top hashtags; more hits diminishing returns.
        for _ in range(15):
            await main_page.mouse.wheel(0, random.randint(1000, 2000))
            await asyncio.sleep(random.uniform(0.4, 0.9))

        # Merge in the initial server-rendered data (script tags) as a backstop
        try:
            for c in (await main_page.evaluate(EXTRACT_USERS_WITH_STATS_JS)) or []:
                uid = (c.get('username') or '').lower()
                if uid and uid not in collected:
                    collected[uid] = c
        except Exception:
            pass
    finally:
        try:
            main_page.remove_listener('response', _on_response)
        except Exception:
            pass
        await main_page.close()

    candidates = list(collected.values())
    if not candidates:
        Actor.log.info(f'#{hashtag}: no creators surfaced')
        return 0

    # Pre-filter by follower count + brand skiplist + dedup across run
    min_fol = int(input_data.get('minFollowers', 1500))
    max_fol = int(input_data.get('maxFollowers', 150000))
    brand_skip = {s.lower() for s in (input_data.get('brandSkiplist') or [])}
    max_profiles = int(input_data.get('maxProfilesPerHashtag', 200))
    scrape_link_in_bio = bool(input_data.get('scrapeLinkInBio', True))
    us_only = bool(input_data.get('usOnly', True))
    skip_ghost = bool(input_data.get('skipGhostAccounts', True))

    pre_filtered = []  # full candidate dicts (carry feed bio/region/bioLink/likes)
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
        pre_filtered.append(c)
        if len(pre_filtered) >= max_profiles:
            break

    Actor.log.info(f'#{hashtag}: {len(pre_filtered)} candidates after pre-filter')
    if not pre_filtered:
        return 0

    pushed = 0
    feed_emails = 0       # emails found directly in the feed bio (zero proxy cost)
    link_jobs = []        # (candidate) needing a bioLink visit — no inline email

    for c in pre_filtered:
        profile = {
            'username': c.get('username'),
            'display_name': c.get('nickname', ''),
            'bio': c.get('bio', ''),
            'followers': int(c.get('followers') or 0),
            'likes': int(c.get('likes') or 0),
            'region': c.get('region', ''),
            'bioLink': c.get('bioLink', ''),
        }

        # Ghost filter (sizeable followers, zero likes = bought/dead account)
        if skip_ghost and profile['followers'] >= 5000 and profile['likes'] == 0:
            continue
        # US filter — uses feed bio + region, no page visit needed
        if us_only and not is_likely_us(profile):
            continue

        email = extract_email_from_bio(profile['bio'])
        if email:
            rec = _build_record(profile, hashtag, email, 'bio')
            try:
                await Actor.push_data(rec)
                pushed += 1
                feed_emails += 1
            except Exception as e:
                Actor.log.warning(f'push_data failed: {type(e).__name__}: {e}')
            continue

        # No inline email — only worth a network visit if there's a link to chase
        if scrape_link_in_bio and (profile['bioLink'] or extract_scrapeable_links(profile['bio'])):
            link_jobs.append(profile)

    # Link-in-bio scraping for the no-inline-email subset (much smaller than
    # the old "visit every profile" path). Run through the link tabs in parallel.
    link_emails = 0
    if link_jobs and link_tabs:
        n = len(link_tabs)
        for i in range(0, len(link_jobs), n):
            batch = link_jobs[i:i + n]
            coros = [
                _scrape_profile_links(link_tabs[j % n], batch[j], hashtag)
                for j in range(len(batch))
            ]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception) or not r:
                    continue
                try:
                    await Actor.push_data(r)
                    pushed += 1
                    link_emails += 1
                except Exception as e:
                    Actor.log.warning(f'push_data failed: {type(e).__name__}: {e}')

    Actor.log.info(
        f'#{hashtag}: {feed_emails} from feed bios (free) + {link_emails} from links '
        f'= {pushed} pushed'
    )
    return pushed


def _build_record(profile: dict, hashtag: str, email: str, source: str) -> dict:
    """Assemble a dataset record from a profile dict + extracted email."""
    bio = profile.get('bio', '')
    username = profile.get('username')
    return {
        'username': username,
        'displayName': profile.get('display_name', ''),
        'bio': bio,
        'email': email,
        'emailSource': source,
        'followers': int(profile.get('followers') or 0),
        'totalLikes': int(profile.get('likes') or 0),
        'region': profile.get('region', ''),
        'ageScore': age_score(profile),
        'instagramHandle': extract_instagram_handle(bio),
        'hashtag': hashtag,
        'profileUrl': f'https://tiktok.com/@{username}',
        'source': 'trimi_custom',
        'scrapedAt': datetime.now(timezone.utc).isoformat(),
    }


async def _scrape_profile_links(tab, profile: dict, hashtag: str):
    """Visit a candidate's bioLink (and other bio URLs) to find an email.
    Only called for creators whose feed bio had no inline email but did have
    a link. Returns a record dict or None."""
    bio = profile.get('bio', '')
    urls = []
    if profile.get('bioLink'):
        urls.append(profile['bioLink'])
    urls.extend(extract_scrapeable_links(bio)[:2])

    for url in urls:
        email = await _scrape_link_for_email(tab, url)
        if email:
            return _build_record(profile, hashtag, email, 'biolink')
    return None


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
