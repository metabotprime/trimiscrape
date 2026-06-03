"""
Actor lifecycle entry point.

Reads input, sets up an Apify-proxied Playwright context with stealth +
resource-blocking, warms up TikTok cookies, then iterates the hashtag list
calling scraper.process_hashtag for each. Pushes qualifying records to the
Apify dataset (the SDK handles platform persistence + retries).

Uses Actor.log throughout (not print()) per Apify security best practices
— the logger censors APIFY_TOKEN and other secrets if they ever appear.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from apify import Actor
from playwright.async_api import async_playwright

from .helpers import (
    STEALTH_INIT_JS,
    block_heavy_resources,
    get_random_ua,
)
from .scraper import process_hashtag


def _parse_proxy_url(url: str) -> dict | None:
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


async def main() -> None:
    async with Actor:
        input_data = await Actor.get_input() or {}
        _validate_input(input_data)

        hashtags = [h.strip().lstrip('#') for h in input_data['hashtags'] if h.strip()]
        Actor.log.info(
            f'Trimi TikTok Email Hunter — {len(hashtags)} hashtag(s), '
            f'{input_data.get("minFollowers", 1500):,}-{input_data.get("maxFollowers", 150000):,} followers, '
            f'US-only={input_data.get("usOnly", True)}'
        )

        # Apify proxy configuration. Will use RESIDENTIAL + US by default
        # per the input schema. The SDK reads the platform's APIFY_TOKEN
        # automatically — we never log it.
        proxy_config = await Actor.create_proxy_configuration(
            actor_proxy_input=input_data.get('proxyConfiguration'),
        )
        proxy_url = await proxy_config.new_url() if proxy_config else None
        playwright_proxy = _parse_proxy_url(proxy_url) if proxy_url else None
        if playwright_proxy:
            Actor.log.info(f'Proxy: {playwright_proxy["server"]}')
        else:
            Actor.log.warning(
                'No proxy configured — TikTok will likely rate-limit. '
                'Set proxyConfiguration.useApifyProxy = true.'
            )

        n_tabs = int(input_data.get('concurrentProfiles', 3))
        n_tabs = max(1, min(n_tabs, 8))  # Clamp to sane range

        seen_usernames: set[str] = set()
        total_pushed = 0

        async with async_playwright() as p:
            launch_kwargs: dict = {'headless': True}
            if playwright_proxy:
                launch_kwargs['proxy'] = playwright_proxy
            browser = await p.chromium.launch(**launch_kwargs)

            try:
                ua = get_random_ua()
                Actor.log.info(f'UA: {ua[:80]}...')

                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 900},
                    user_agent=ua,
                    locale='en-US',
                    timezone_id='America/New_York',
                )

                # Hide automation tells before any page JS runs
                await context.add_init_script(STEALTH_INIT_JS)
                # Drop image/media/font requests for bandwidth + detection
                await block_heavy_resources(context)

                # Warm up — visit TikTok homepage to seed cookies
                warmup = await context.new_page()
                try:
                    await warmup.goto(
                        'https://www.tiktok.com',
                        wait_until='domcontentloaded',
                        timeout=30000,
                    )
                    await asyncio.sleep(2.5)
                except Exception as e:
                    Actor.log.warning(f'Warm-up failed (continuing): {type(e).__name__}')
                finally:
                    await warmup.close()

                # Tab pool — n_tabs for profiles, 2 for link-in-bio scraping
                profile_tabs = [await context.new_page() for _ in range(n_tabs)]
                link_tabs = [await context.new_page(), await context.new_page()]

                try:
                    for hashtag in hashtags:
                        try:
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
                                f'#{hashtag} → +{pushed} pushed | run total: {total_pushed}'
                            )
                        except Exception as e:
                            # Log + continue — one bad hashtag shouldn't halt the run
                            Actor.log.exception(f'#{hashtag} failed: {type(e).__name__}: {e}')
                            continue
                finally:
                    for tab in profile_tabs + link_tabs:
                        try:
                            await tab.close()
                        except Exception:
                            pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        Actor.log.info(
            f'Done. {total_pushed} qualifying creators pushed to dataset. '
            f'Unique usernames seen this run: {len(seen_usernames):,}'
        )
