"""
Filters, validators, JS extractors. Ported from the parent codebase
(discovery_free.py + scrape_utils.py) — kept aligned in logic so the
actor's ICP filtering matches the free bot's.

Treats all scraped text as untrusted input: regex matchers only, no
eval / shell / template-engine substitution of bio content.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import List
from urllib.parse import urlparse


# ═══════════════════════════════════════════════
# USER-AGENT POOL (no third-party dep)
# ═══════════════════════════════════════════════

_UA_POOL = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
]


def get_random_ua() -> str:
    return random.choice(_UA_POOL)


# ═══════════════════════════════════════════════
# STEALTH (init script — hides automation tells)
# ═══════════════════════════════════════════════

STEALTH_INIT_JS = r"""
(() => {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  Object.defineProperty(navigator, 'plugins', {
    get: () => [
      { name: 'Chrome PDF Plugin' },
      { name: 'Chrome PDF Viewer' },
      { name: 'Native Client' },
    ],
  });
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  try {
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({ state: 'prompt' })
        : origQuery.call(window.navigator.permissions, params);
  } catch (e) {}
  try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParam.call(this, p);
    };
  } catch (e) {}
})();
"""


# ═══════════════════════════════════════════════
# RESOURCE BLOCKING (image/media/font abort)
# ═══════════════════════════════════════════════

_BLOCK_RESOURCE_TYPES = {'image', 'media', 'font'}


async def block_heavy_resources(context) -> None:
    """Abort image/media/font requests across the whole context. Cuts bandwidth
    ~70% on TikTok hashtag pages (autoplaying video previews) and reduces
    the page footprint that bot-detection heuristics observe."""

    async def _handler(route):
        try:
            if route.request.resource_type in _BLOCK_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await context.route('**/*', _handler)
    except Exception:
        pass


# ═══════════════════════════════════════════════
# EMAIL EXTRACTION + VALIDATION
# ═══════════════════════════════════════════════

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

# Role-style addresses we never want to email
ROLE_PREFIXES = {
    'admin', 'support', 'help', 'noreply', 'no-reply', 'donotreply',
    'info', 'sales', 'marketing', 'press', 'media', 'contact',
    'webmaster', 'postmaster', 'abuse', 'security', 'legal',
    'jobs', 'hr', 'careers', 'billing', 'accounts', 'finance',
}

# Free-mail + corporate domains we keep; blacklist big nopes
BLACKLISTED_DOMAINS = {
    'tiktok.com', 'instagram.com', 'youtube.com', 'twitter.com', 'x.com',
    'facebook.com', 'example.com', 'test.com', 'localhost',
}

BLACKLISTED_TLDS = ('.gov', '.mil', '.edu')


def is_good_email(email: str) -> bool:
    """Validator: deliverable address shape, not role, not gov/mil/edu."""
    if not email or '@' not in email:
        return False
    email = email.lower().strip()
    parts = email.split('@')
    if len(parts) != 2:
        return False
    local, domain = parts
    if not local or not domain or '.' not in domain:
        return False
    if domain in BLACKLISTED_DOMAINS:
        return False
    if domain.endswith(('.png', '.jpg', '.gif', '.jpeg', '.mp4', '.mov')):
        return False
    for tld in BLACKLISTED_TLDS:
        if domain.endswith(tld):
            return False
    if local in ROLE_PREFIXES:
        return False
    return True


def extract_email_from_bio(bio: str) -> str:
    """First deliverable email-shaped token in bio, or ''."""
    if not bio:
        return ''
    for m in EMAIL_REGEX.finditer(bio):
        candidate = m.group(0).lower().strip().rstrip('.')
        if is_good_email(candidate):
            return candidate
    return ''


# ═══════════════════════════════════════════════
# LINK-IN-BIO HANDLING
# ═══════════════════════════════════════════════

LINK_IN_BIO_DOMAINS = {
    'linktr.ee', 'linktree.com', 'beacons.ai', 'stan.store',
    'bio.link', 'linkbio.co', 'tap.bio', 'campsite.bio',
    'lnk.bio', 'hoo.be', 'solo.to', 'snipfeed.co',
    'allmylinks.com', 'carrd.co', 'direct.me', 'withkoji.com',
    'milkshake.app', 'lynk.id', 'biolinky.co', 'msha.ke',
    'flow.page', 'lumi.link', 'unfold.com',
}

URL_REGEX = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)

DEEP_SCRAPE_PATHS = ('/contact', '/about')


def is_link_in_bio_service(url: str) -> bool:
    try:
        domain = urlparse(url).hostname or ''
        return any(lib in domain.lower() for lib in LINK_IN_BIO_DOMAINS)
    except Exception:
        return False


def get_base_url(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.hostname:
            return f'{p.scheme}://{p.hostname}'
        return ''
    except Exception:
        return ''


_SOCIAL_SKIP_DOMAINS = {
    'tiktok.com', 'instagram.com', 'youtube.com', 'twitter.com',
    'x.com', 'facebook.com', 'snapchat.com', 'wa.me', 't.me',
}


def extract_scrapeable_links(bio: str) -> List[str]:
    """External URLs from bio that might host an email. Drops social links."""
    if not bio:
        return []
    out = []
    for m in URL_REGEX.finditer(bio):
        url = m.group(0).rstrip('.,;:!?)')
        try:
            domain = (urlparse(url).hostname or '').lower()
        except Exception:
            continue
        if any(sd in domain for sd in _SOCIAL_SKIP_DOMAINS):
            continue
        out.append(url)
    return out


# Emails-from-page extractor — JS, runs in browser context
EXTRACT_EMAILS_JS = r"""
() => {
    let emails = new Set();
    document.querySelectorAll('a[href^="mailto:"]').forEach(a => {
        let e = a.href.replace('mailto:', '').split('?')[0].trim().toLowerCase();
        if (e.includes('@') && e.includes('.')) emails.add(e);
    });
    let text = document.body ? document.body.innerText : '';
    let matches = text.match(/[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}/g) || [];
    matches.forEach(m => emails.add(m.toLowerCase()));
    return Array.from(emails);
}
"""


# ═══════════════════════════════════════════════
# PROFILE EXTRACTOR — cascades 3 strategies
# ═══════════════════════════════════════════════

EXTRACT_PROFILE_JS = r"""
() => {
    let result = {username: '', display_name: '', bio: '', followers: 0, likes: 0, bioLink: '', region: ''};

    // Method 1: __UNIVERSAL_DATA__ (most reliable)
    try {
        let script = document.querySelector('script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (script) {
            let data = JSON.parse(script.textContent);
            let scope = data['__DEFAULT_SCOPE__'] || {};
            let detail = scope['webapp.user-detail'] || {};
            let info = detail['userInfo'] || {};
            let user = info['user'] || {};
            let stats = info['stats'] || {};
            if (user['uniqueId']) {
                return {
                    username: user['uniqueId'],
                    display_name: user['nickname'] || '',
                    bio: user['signature'] || '',
                    followers: stats['followerCount'] || 0,
                    likes: stats['heartCount'] || 0,
                    bioLink: (user['bioLink'] || {})['link'] || '',
                    region: user['region'] || '',
                };
            }
        }
    } catch(e) {}

    // Method 2: SIGI_STATE (alternative data source)
    try {
        let script = document.querySelector('script#SIGI_STATE');
        if (script) {
            let data = JSON.parse(script.textContent);
            let users = data['UserModule'] || {};
            let userList = users['users'] || {};
            let statsMap = users['stats'] || {};
            for (let uid in userList) {
                let user = userList[uid];
                let stats = statsMap[uid] || {};
                return {
                    username: user['uniqueId'] || uid,
                    display_name: user['nickname'] || '',
                    bio: user['signature'] || '',
                    followers: stats['followerCount'] || 0,
                    likes: stats['heartCount'] || 0,
                    bioLink: (user['bioLink'] || {})['link'] || '',
                    region: user['region'] || '',
                };
            }
        }
    } catch(e) {}

    // Method 3: DOM fallback
    try {
        let bioEl = document.querySelector('[data-e2e="user-bio"]');
        if (bioEl) result.bio = bioEl.textContent.trim();
        let nameEl = document.querySelector('[data-e2e="user-title"]');
        if (nameEl) result.display_name = nameEl.textContent.trim();
        let subtitleEl = document.querySelector('[data-e2e="user-subtitle"]');
        if (subtitleEl) result.username = subtitleEl.textContent.trim().replace('@', '');
        let followersEl = document.querySelector('[data-e2e="followers-count"]');
        if (followersEl) {
            let t = followersEl.textContent.trim().replace(/,/g, '');
            if (t.includes('K')) result.followers = Math.round(parseFloat(t) * 1000);
            else if (t.includes('M')) result.followers = Math.round(parseFloat(t) * 1000000);
            else result.followers = parseInt(t) || 0;
        }
    } catch(e) {}

    return result;
}
"""

# Hashtag-page users with stats (pre-filter without per-profile visits)
EXTRACT_USERS_WITH_STATS_JS = r"""
() => {
    let users = {};
    try {
        let script = document.querySelector('script#__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (script) {
            let data = JSON.parse(script.textContent);
            let scope = data['__DEFAULT_SCOPE__'] || {};
            let challenge = scope['webapp.challenge-detail'] || {};
            let itemList = challenge['itemList'] || [];
            for (let item of itemList) {
                let author = item['author'] || {};
                let stats = item['authorStats'] || author['stats'] || {};
                let uid = (author['uniqueId'] || '').toLowerCase();
                if (uid && !users[uid]) {
                    users[uid] = {
                        username: uid,
                        followers: stats['followerCount'] || 0,
                        nickname: author['nickname'] || '',
                    };
                }
            }
        }
    } catch(e) {}
    if (Object.keys(users).length === 0) {
        let links = document.querySelectorAll('a[href*="/@"]');
        for (let link of links) {
            let match = link.href.match(/\/@([a-zA-Z0-9_.]+)/);
            if (match) {
                let u = match[1].toLowerCase();
                if (u.length > 1 && !['explore','discover','tag','search','live'].includes(u) && !users[u]) {
                    users[u] = {username: u, followers: 0, nickname: ''};
                }
            }
        }
    }
    return Object.values(users);
}
"""


# ═══════════════════════════════════════════════
# US + AGE-35-55 FILTERS
# ═══════════════════════════════════════════════

NON_US_SIGNALS = re.compile(
    r'\b(?:UK|London|Manchester|Lagos|Nigeria|Naija|Ghana|India|Mumbai|Delhi|'
    r'Philippines|Manila|PH|Jakarta|Indonesia|Brasil|Brazil|Mexico|CDMX|'
    r'Pakistan|Karachi|Lahore|Bangladesh|Dhaka|Egypt|Cairo|'
    r'South Africa|Johannesburg|Cape Town|Kenya|Nairobi|'
    r'Australia|Sydney|Melbourne|Canada|Toronto|Vancouver|'
    r'Dubai|UAE|Emirates|Saudi|KSA|Riyadh)\b',
    re.IGNORECASE,
)

US_SIGNALS = re.compile(
    r'\b(?:NYC|LA|ATL|Chicago|Houston|Phoenix|Dallas|Austin|Miami|'
    r'San Diego|San Antonio|Denver|Nashville|Charlotte|Portland|'
    r'Seattle|Tampa|Boston|Detroit|Atlanta|Orlando|Minneapolis|'
    r'Las Vegas|Raleigh|Memphis|Sacramento|San Francisco|'
    r'California|Texas|Florida|New York|Georgia|Ohio|Illinois|'
    r'Pennsylvania|Michigan|North Carolina|Tennessee|Arizona|'
    r'Colorado|Virginia|Wisconsin|Minnesota|Indiana|Missouri|'
    r'Maryland|Connecticut|Oregon|Washington|Kentucky|'
    r'USA|United States|US based|\U0001F1FA\U0001F1F8)\b',
    re.IGNORECASE,
)

AGE_35_55_SIGNALS = re.compile(
    r'\b(?:mom|mama|mother|momma|momof|momto|parent|wife|husband|dad|father|'
    r'kids?|son|daughter|family|stepmom|grandma|granny|auntie|'
    r'nurse|RN|NP|PA|teacher|coach|therapist|counselor|'
    r'doctor|MD|DO|RD|dietitian|nutritionist|realtor|'
    r'over ?40|over ?50|over ?35|over ?45|midlife|mid-life|midage|'
    r'40s|50s|30s|fortysomething|fiftysomething|'
    r'perimenopause|menopause|hormonal|hot ?flashes|'
    r'empty ?nest|grown ?kids|teenager|teen ?mom|toddler|'
    r'married ?\d+|wed ?\d+|husband ?of|wife ?of|'
    r'career|professional|corporate|executive|founder|ceo|owner|'
    r'years ?old|yr ?old|y/o|yo)\b',
    re.IGNORECASE,
)


def is_likely_us(profile: dict) -> bool:
    region = (profile.get('region') or '').upper()
    if region and region not in ('US', 'USA', ''):
        return False
    bio = profile.get('bio') or ''
    display = profile.get('display_name') or ''
    text = bio + ' ' + display
    if NON_US_SIGNALS.search(text):
        return False
    if US_SIGNALS.search(text):
        return True
    if region in ('US', 'USA'):
        return True
    return True  # Unknown region with no negative signal — accept


def age_score(profile: dict) -> int:
    """0-3 score for likelihood of age-35-55 ICP. Soft signal for prioritization."""
    bio = profile.get('bio') or ''
    display = profile.get('display_name') or ''
    text = bio + ' ' + display
    matches = AGE_35_55_SIGNALS.findall(text)
    return min(len(matches), 3)


# ═══════════════════════════════════════════════
# INSTAGRAM HANDLE EXTRACTION
# ═══════════════════════════════════════════════

_IG_HANDLE_RE = re.compile(
    r'(?:instagram\.com/|ig[:\s]*@|insta[:\s]*@|@)([A-Za-z0-9_.]{3,30})',
    re.IGNORECASE,
)


def extract_instagram_handle(bio: str) -> str:
    if not bio:
        return ''
    for m in _IG_HANDLE_RE.finditer(bio):
        handle = m.group(1).lstrip('@').lower()
        # Heuristic: avoid catching emails or other false positives
        if 3 <= len(handle) <= 30 and not handle.startswith('.'):
            return handle
    return ''


# ═══════════════════════════════════════════════
# PER-DOMAIN RATE LIMITER (async)
# ═══════════════════════════════════════════════

class DomainRateLimiter:
    """Enforce a minimum gap between requests to the same host.

    Without this, the 2 link-in-bio tabs can hit the same linktr.ee within
    milliseconds when two creators in a row use linktree — looks botty,
    gets challenged. Per-domain enforcement keeps each host below its
    natural request rate while letting the tabs run flat-out across
    diverse hosts.
    """

    def __init__(self, min_gap_seconds: float = 1.5, jitter: float = 0.6):
        self.min_gap = float(min_gap_seconds)
        self.jitter = float(jitter)
        self._last_hit: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _domain(url: str) -> str:
        try:
            host = urlparse(url).hostname or ''
            return host.lower()
        except Exception:
            return ''

    async def wait_for(self, url: str) -> None:
        domain = self._domain(url)
        if not domain:
            return
        async with self._lock:
            last = self._last_hit.get(domain, 0.0)
            now = time.time()
            # Reserve a slot at `wait_until` so a concurrent caller for the
            # same domain queues *after* us, not in parallel. Sleeping happens
            # outside the lock so other domains aren't blocked.
            wait_until = max(last + self.min_gap, now) + random.uniform(0, self.jitter)
            self._last_hit[domain] = wait_until
            sleep_for = wait_until - now
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
