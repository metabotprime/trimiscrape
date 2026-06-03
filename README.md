# Trimi TikTok Email Hunter

ICP-tuned TikTok email-discovery actor. Finds partnership-ready mom-creator and wellness-routine influencers (15k–100k followers, US-only, age-35-55 signal-scored) with discoverable emails. Built specifically for the Trimi brand's outreach pipeline.

## What this actor does

For each hashtag you supply, the actor:

1. Loads the TikTok hashtag page via Apify **residential proxies**
2. Pre-filters surfaced creators by **follower band** + a **brand skiplist**
3. Loads up to N candidates in parallel (3 tabs by default)
4. Extracts profile data from `__UNIVERSAL_DATA_FOR_REHYDRATION__` (with `SIGI_STATE` and DOM fallbacks)
5. Applies the **US-only** filter, **ghost-account** filter, and **age-35-55** scoring
6. Pulls email from bio; if none, follows the bio link (linktr.ee, beacons.ai, personal sites — including `/contact` and `/about` deep-scrape)
7. Pushes qualifying records to the dataset with email source, age score, IG handle, and hashtag attribution

## Why use this actor?

- **Higher conversion than generic TikTok scrapers** — pre-filters to creators who already match Trimi's ICP, instead of returning everything and forcing post-hoc filtering.
- **Residential proxies** — TikTok actively rate-limits datacenter IPs. The Apify SCALE plan includes residential bandwidth at $7.50/GB; this actor uses it by default with US geo.
- **Hashtag defaults baked in from real performance data** — the prefilled hashtag list is the 40 highest-yielding hashtags from Trimi's actual historical email-discovery data (mom-life, wellness-routine, midsize-fashion, UGC-creator clusters).
- **Avoids known-dead hashtags** — medication-specific tags (`#ozempic`, `#mounjarojourney`, etc.) are explicitly excluded; historical yield is 0% because creators using those tags are patients sharing journeys, not partnership-ready influencers.
- **Stealth + resource-blocking** — abort image/media/font requests for ~70% bandwidth reduction and a smaller bot-detection footprint.
- **Apify platform advantages** — scheduling, API access, monitoring, dataset export to CSV/JSON/Excel.

## What data does the actor extract?

| Field | Type | Description |
|---|---|---|
| `username` | string | TikTok handle (unique ID) |
| `displayName` | string | Display name (used for first-name extraction downstream) |
| `email` | string | Deliverable email — bio first, then link-in-bio |
| `emailSource` | string | `"bio"` or `"biolink"` |
| `followers` | number | Follower count at scrape time |
| `totalLikes` | number | Cumulative likes across all videos |
| `ageScore` | number | 0-3 score for age-35-55 signal (mom/parent/professional/menopause/etc) |
| `region` | string | TikTok-reported region code |
| `hashtag` | string | Which hashtag surfaced this creator |
| `instagramHandle` | string | IG handle parsed from bio, if present |
| `bio` | string | Raw bio text |
| `profileUrl` | string | Direct link to TikTok profile |
| `scrapedAt` | string | ISO timestamp |

## How to scrape TikTok with this actor

1. **Open the actor in Apify Console** (or call via API).
2. **Customize the hashtag list** in the input form — or leave the defaults for Trimi's proven-winner set.
3. **Confirm follower range** (default 1500–150000 — the sweet spot for partnership offers).
4. **Verify proxy config** — `RESIDENTIAL` group, `US` country (the default). Datacenter proxies will get challenged.
5. **Click Run.** Results stream into the dataset as they're found — you can watch the Output tab live.
6. **Export results** as CSV / JSON / Excel from the Output tab when done.

## How much will it cost?

Pricing scales with your Apify plan. On the **SCALE plan ($199/mo, $250 usage ceiling)**:

| Resource | Rate | Typical run |
|---|---|---|
| Compute units (4 GB × hour) | $0.16 / CU | ~0.3 CU per hashtag with 200 profiles = $0.05 |
| Residential proxy | $7.50 / GB | Image/media-blocked, ~50–100 KB / profile = $0.40 / 1k profiles |
| Dataset writes | $4.5e-6 / write | Negligible |

**Estimated cost: ~$1.50 per 1,000 qualifying emails** (residential-proxy-dominated). Under the $250 monthly ceiling, that's ~165,000 emails/month theoretical maximum.

Hard daily cap is enforced upstream in the calling Python pipeline (see `apify_budget.py`); this actor itself runs until its input hashtag list is exhausted or the platform stops it.

## Input

See the **Input** tab in Apify Console for full configuration. Highlights:

- `hashtags` — array of strings, no `#` prefix. Default: 40 proven-winner Trimi hashtags.
- `minFollowers` / `maxFollowers` — integers, default 1500–150000.
- `maxProfilesPerHashtag` — soft cap; 200 hits diminishing returns.
- `concurrentProfiles` — parallel tabs, default 3 (sweet spot for residential).
- `usOnly` / `skipGhostAccounts` / `scrapeLinkInBio` — booleans, all default true.
- `brandSkiplist` — usernames to skip (Aerie, Old Navy, Skims, Gymshark, TikTok official accounts, etc.). Pre-populated.
- `proxyConfiguration` — defaults to Apify Proxy, RESIDENTIAL group, US country.
- `earlyExitNoEmailThreshold` — skip rest of a hashtag after N profiles with 0 emails. Default 12.

## Output

You can download the dataset in various formats: JSON, CSV, Excel, or HTML.

Example output item:

```json
{
  "username": "wellnesswithjane",
  "displayName": "Jane | Mom of 3",
  "email": "jane@wellnesswithjane.com",
  "emailSource": "bio",
  "followers": 28400,
  "totalLikes": 1240000,
  "ageScore": 3,
  "region": "US",
  "hashtag": "momlife",
  "instagramHandle": "wellnesswithjane",
  "bio": "Mom of 3 | Pilates instructor | NYC | 📩 jane@wellnesswithjane.com",
  "profileUrl": "https://tiktok.com/@wellnesswithjane",
  "scrapedAt": "2026-05-08T19:23:14+00:00"
}
```

## Deploy from this directory

> You don't have `npx` / `node` on this machine, so the standard `apify push` flow needs either of:

**Path A — Apify Console multi-file paste (no CLI required):**

1. Sign in to https://console.apify.com.
2. Go to **Actors → Develop new** → choose **Empty Python project**.
3. In the multi-file editor, replace each generated file with the matching one from this directory:
   - `.actor/actor.json`
   - `.actor/input_schema.json`
   - `.actor/dataset_schema.json`
   - `.actor/output_schema.json`
   - `Dockerfile`
   - `requirements.txt`
   - `src/__init__.py`
   - `src/__main__.py`
   - `src/main.py`
   - `src/scraper.py`
   - `src/helpers.py`
4. Click **Build** → wait for green.
5. Copy the actor ID shown on the Actor page (format: `<your-username>/trimi-tiktok-email-hunter`).
6. Paste it into `config.py` → `APIFY["trimi_hunter_actor_id"]`.
7. Run a small test: set `hashtags: ["momlife"]`, `maxProfilesPerHashtag: 20`, click **Start**. Watch the log + output tab.

**Path B — install Apify CLI (if you'd rather):**

```bash
# Mac (no curl-piping per Apify security guidance)
brew install apify-cli

# Verify install and auth
apify --help
apify info        # Should print "Trimi" as the user

# If not logged in:
export APIFY_TOKEN=apify_api_...   # from console.apify.com/settings/integrations
# (Or run `apify login` for OAuth in browser)

# From the trimi_actor/ directory:
apify push                          # Builds + uploads in one step
```

After deploy, set `config.APIFY["trimi_hunter_actor_id"]` and the parent Python pipeline will route through it as the primary acquisition pass.

### Test locally before deploying (if CLI installed)

```bash
# From trimi_actor/, drop a small test input:
mkdir -p storage/key_value_stores/default
cat > storage/key_value_stores/default/INPUT.json <<'EOF'
{
  "hashtags": ["momlife"],
  "minFollowers": 1500,
  "maxFollowers": 150000,
  "maxProfilesPerHashtag": 20,
  "concurrentProfiles": 3,
  "usOnly": true,
  "skipGhostAccounts": true,
  "scrapeLinkInBio": true,
  "proxyConfiguration": {
    "useApifyProxy": true,
    "apifyProxyGroups": ["RESIDENTIAL"],
    "apifyProxyCountry": "US"
  }
}
EOF

apify run --purge   # Runs in a local Docker-like sim. Output: ./storage/datasets/default/
```

**Note:** `apify run` results are LOCAL ONLY — they are never pushed to Apify Console. To verify cloud behavior, run on the platform via the Console UI or `apify call`.

## Tips

- **Don't crank up `maxProfilesPerHashtag` past 300** — TikTok's hashtag-page JSON stops returning new authors after ~80 unique creators per scroll session.
- **Watch the daily cap** — the parent Python pipeline checks Apify usage before each run; see `apify_budget.py`.
- **A/B test hashtag groups** by passing different `hashtags` arrays per run.

## FAQ

**Q: Why these hashtags by default?**
A: They're the 40 highest-yielding hashtags from Trimi's historical scraping data (450k+ profiles visited, 7k+ emails extracted). Mom-life, wellness-routine, midsize-fashion, and UGC-creator clusters convert at 25–45%. Medication-explicit tags convert at 0%.

**Q: Will this actor scrape any TikTok creator?**
A: It only stores creators that pass all of: in follower band, US region (or unknown), non-ghost (followers vs likes ratio), and have a discoverable email. Most surfaced candidates are filtered out before being pushed — that's by design.

**Q: Disclaimer**
> This actor is for ethical outreach. It does not extract private data — only what creators have chosen to share publicly in their bio or link-in-bio. You should be aware that results may contain personal data subject to GDPR (EU) and other regulations. You should not scrape personal data unless you have a legitimate reason to do so. If you're unsure whether your reason is legitimate, consult your lawyers.

**Support:** Use the Issues tab on the Actor's page in Apify Console.
