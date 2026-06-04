"""
Actor lifecycle entry point.

Per-hashtag proxy rotation: for each hashtag we spin up a fresh Playwright
context with a NEW Apify residential proxy session (= new exit IP). When
TikTok rate-limits one IP — which empirically affects ~40% of hashtags
per run — only that single hashtag is impacted, not the rest of the run.
Trade-off: ~3-5s context-setup overhead per hashtag, paid once per IP.

Uses Actor.log throughout (not print()) per Apify security best practices
— the logger censors APIFY_TOKEN and other secrets if they ever appear.
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional
from urllib.parse import urlparse

from apify import Actor
from playwright.async_api import async_playwright

from .helpers import (
    STEALTH_INIT_JS,
    block_heavy_resources,
    get_random_ua,
)
from .scraper import process_hashtag


def _parse_proxy_url(url: str) -> Optional[dict]:
    """Convert a proxy URL (http://user:pass@host:port) into Playwright's
    proxy dict. Returns None on parse failure."""
    if not url:
        return None
    try:
        p = urlparse(url)
        if not p.hostname or not p.port:
            return None
        return {
            'server': f'{p.scheme}://{p.hostname}:{p.port}',
            'username': p.username or '',
            'password': p.password or '',
        }
    except Exception:
        return None


def _validate_input(input_data: dict) -> None:
    """Reject obviously malformed input early — Actor SDK already enforces
    the schema, but defense in depth doesn't cost anything."""
    hashtags = input_data.get('hashtags')
    if not hashtags or not isinstance(hashtags, list):
        raise ValueError('Input field "hashtags" is required and must be a non-empty list of strings')
    if not all(isinstance(h, str) and h.strip() for h in hashtags):
        raise ValueError('All entries in "hashtags" must be non-empty strings')

    min_fol = input_data.get('minFollowers')
    max_fol = input_data.get('maxFollowers')
    if isinstance(min_fol, int) and isinstance(max_fol, int) and min_fol > max_fol:
        raise ValueError(f'minFollowers ({min_fol}) cannot exceed maxFollowers ({max_fol})')


async def _setup_context(browser, input_data, proxy_config, hashtag_for_session: str):
    """Spin up a fresh context with a NEW residential proxy session token.

    Apify's create_proxy_configuration().new_url(session_id=X) returns a
    proxy URL anchored to a specific exit IP for that session token.
    Using the hashtag (plus a per-run salt) as the session_id gives us:
      - A different IP per hashtag (rate-limit isolation)
      - Stable IP if a hashtag is retried within the run
    """
    if proxy_config:
        # Apify sessions accept [a-zA-Z0-9.\-_]; hashtags are already safe.
        session_id = f"trimi-{hashtag_for_session}-{random.randint(1000,9999)}"
        proxy_url = await proxy_config.new_url(session_id=session_id)
    else:
        proxy_url = None
    pw_proxy = _parse_proxy_url(proxy_url) if proxy_url else None

    ua = get_random_ua()
    context = await browser.new_context(
        viewport={'width': 1280, 'height': 900},
        user_agent=ua,
        locale='en-US',
        timezone_id='America/New_York',
        proxy=pw_proxy,
    )
    # Stealth init script before any page JS runs
    await context.add_init_script(STEALTH_INIT_JS)
    # Drop image/media/font + video-CDN requests
    await block_heavy_resources(context)
    return context, pw_proxy


async def main() -> None:
    async with Actor:
        input_data = await Actor.get_input() or {}
        _validate_input(input_data)

        hashtags = [h.strip().lstrip('#') for h in input_data['hashtags'] if h.strip()]
        Actor.log.info(
            f'Trimi TikTok Email Hunter — {len(hashtags)} hashtag(s), '
            f'{input_data.get("minFollowers", 1500):,}-{input_data.get("maxFollowers", 150000):,} followers, '
            f'US-only={input_data.get("usOnly", True)} | per-hashtag proxy rotation: ON'
        )

        # Single ProxyConfiguration object — reused, but each new_url(session_id=X)
        # call returns a different exit IP based on the session token.
        proxy_config = await Actor.create_proxy_configuration(
            actor_proxy_input=input_data.get('proxyConfiguration'),
        )
        if not proxy_config:
            Actor.log.warning(
                'No proxy configured — TikTok will likely rate-limit. '
                'Set proxyConfiguration.useApifyProxy = true.'
            )

        n_tabs = int(input_data.get('concurrentProfiles', 3))
        n_tabs = max(1, min(n_tabs, 8))  # Clamp to sane range

        seen_usernames: set = set()
        total_pushed = 0
        ip_rotations = 0

        async with async_playwright() as p:
            # Launch browser ONCE without a proxy — proxies are per-context.
            browser = await p.chromium.launch(headless=True)
            try:
                for h_idx, hashtag in enumerate(hashtags):
                    context = None
                    profile_tabs: list = []
                    link_tabs: list = []
                    try:
                        # Fresh proxy session per hashtag = fresh exit IP
                        context, pw_proxy = await _setup_context(
                            browser, input_data, proxy_config, hashtag,
                        )
                        ip_rotations += 1
                        if h_idx == 0 and pw_proxy:
                            Actor.log.info(f'Proxy: {pw_proxy["server"]} (rotating session per hashtag)')

                        profile_tabs = [await context.new_page() for _ in range(n_tabs)]
                        link_tabs = [await context.new_page(), await context.new_page()]

                        pushed = await process_hashtag(
                            context=context,
                            profile_tabs=profile_tabs,
                            link_tabs=link_tabs,
                            hashtag=hashtag,
                            input_data=input_data,
                            seen_usernames=seen_usernames,
                        )
                        total_pushed += pushed
                        Actor.log.info(
                            f'#{hashtag} → +{pushed} pushed | run total: {total_pushed} '
                            f'(IP rotation #{ip_rotations})'
                        )
                    except Exception as e:
                        # Log + continue — one bad hashtag shouldn't halt the run
                        Actor.log.exception(f'#{hashtag} failed: {type(e).__name__}: {e}')
                    finally:
                        for tab in profile_tabs + link_tabs:
                            try:
                                await tab.close()
                            except Exception:
                                pass
                        if context:
                            try:
                                await context.close()
                            except Exception:
                                pass
                        # Small jitter between hashtags to avoid burst-detection
                        await asyncio.sleep(random.uniform(1.0, 2.5))
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        Actor.log.info(
            f'Done. {total_pushed} qualifying creators pushed to dataset. '
            f'Unique usernames seen this run: {len(seen_usernames):,} | '
            f'IP rotations: {ip_rotations}'
        )
