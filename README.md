# Probiotic Price Tracking — Thailand

Daily competitor price and assortment tracking for probiotic supplements, replacing the
manual browse-and-paste loop. Four stages behind one batch file: **scrape, store, process,
dashboard.**

Covers **Lazada Thailand** only. Shopee was built and TikTok Shop was probed; both were
abandoned on 2026-08-20 because anti-bot measures beat every free local approach. The
reasoning is in `MEMORY.md` and the full investigation in `notes/build-log-v1-20260819.md`.

## Run it

Double-click **`scripts\run.bat`** in Explorer. That is the normal way.

From a PowerShell prompt, give it a path. `run.bat` on its own will not work, because
PowerShell does not search the current folder, and the file lives in `scripts\`:

```bash
.\scripts\run.bat
```

If Lazada asks for a slider verification, double-click **`scripts\run-headed.bat`** instead.
See "If a run comes back blocked" below.

| Command | What it does |
|---|---|
| `run.bat` | collect, process, open the dashboard |
| `run.bat offline` | rebuild from the snapshots already on disk, no network |
| `run.bat browser` | force the browser path, skipping the HTTP attempt |
| `run.bat headed` | force the browser path with the window visible, to solve a challenge |
| `run.bat rebuild` | re-parse today's saved JSON payloads, then rebuild |
| `run.bat dashboard` | just open the dashboard |
| `run.bat test` | pipeline self-checks, never touches `data\` |

Useful flags while debugging:

```bash
py collect_lazada.py --check              # one request, prints a sample row, writes nothing
py collect_lazada.py --pages 2            # shorter run
py collect_lazada.py --bucket immune      # one shelf only
py collect_lazada.py --browser            # skip the HTTP attempt, go straight to the browser
py collect_lazada.py --browser --headed   # show the window, to solve a captcha by hand once
py collect_lazada.py --rebuild            # re-parse today's saved payloads, no network
```

### If a run comes back blocked

Lazada escalates in stages: first it walls plain HTTP, then it puts a slide-to-verify
challenge in front of the browser too. The second stage looks like this in the output:

```
page 1: BLOCKED by Lazada's anti-bot wall
```

The fix is a one-off, and it needs a human. Double-click **`scripts\run-headed.bat`**, or:

```bash
.\scripts\run.bat headed
```

A browser window opens. If a slider appears, **solve it yourself** — the collector detects
the challenge, prints a notice, and waits up to five minutes for you, then carries on by
itself. The profile is persistent, so the verified session is remembered for later runs.

Two things worth knowing. Trust score, not the URL, is what gets depleted, so if you have
just been hammering it, leave it a few hours before trying. And if it keeps happening,
raise `delay_seconds_min` / `delay_seconds_max` in `config/settings.json` and cut `pages`
in `config/targets.csv`; a smaller, slower daily sweep is far more sustainable than a big
one that gets you walled.

The collector never retries a wall. It stops at the first one, writes nothing, and leaves
the previous snapshot intact.

## How it works

**1. Collect** — `scripts/collect_lazada.py`

Lazada renders its category pages from a JSON payload the same URL returns when you append
`?ajax=true`, 40 listings at a time:

```
https://www.lazada.co.th/shop-digestion-and-absorption/?ajax=true&isFirstRequest=true&page=1&q=probiotic
```

The collector tries that as a plain HTTP request first because it is fast and cheap. Since
**2026-08-11** Lazada walls plain HTTP clients behind Alibaba's x5sec captcha, so when the
collector sees the wall it automatically switches to a headless Chromium via Playwright:
it opens the ordinary category page, then issues the same ajax calls from *inside* that
page. Those carry the browser's real fingerprint and its JavaScript-set cookies, and they
return byte-identical JSON, so nothing downstream changes.

The switch is announced in the output and happens at most once per run. If Lazada ever
relaxes, runs quietly go back to the fast path with no code change.

Requests are sequential with a randomised 2 to 4 second gap and a hard cap of 90 per run,
set in `config/settings.json`. A full sweep is about 45 requests: roughly 3 minutes over
HTTP, 6 to 8 through the browser.

The browser profile is persistent and lives at
`%LOCALAPPDATA%\ecom_price_tracking\browser_profile`, deliberately outside OneDrive since
a Chromium profile is tens of thousands of small files. Keeping it means cookies and the
site's trust score carry over between runs, which is most of what keeps the wall down.

Writes `data/raw/YYYY-MM-DD-lazada.csv`, one row per unique SKU. Raw JSON is also gzipped
into `data/raw/_payloads/` so a parser bug can be fixed with `--rebuild` instead of another
round of scraping.

**2. Process** — `scripts/pipeline.py`

Merges every dated snapshot, de-duplicates on date plus SKU, works out what moved against
each SKU's own previous observation, and writes into `data/processed/`:

| File | What it holds |
|---|---|
| `products_master.csv` | full history, one row per SKU per day |
| `latest.csv` | the newest snapshot per SKU |
| `price_changes.csv` | drops, rises, promos started and ended, new and delisted SKUs |
| `brand_daily.csv` | brand rollup with share of shelf |
| `seller_daily.csv` | seller rollup |
| `subcategory_daily.csv` | shelf rollup with promo penetration |
| `market_daily.csv` | one row per date per platform - the daily price series |
| `data_quality.csv` | **read this before quoting any number** |
| `app_data.js` | `window.ECOM_DATA`, the dashboard's only input |

**3. Dashboard** — `dashboard/index.html`

One self-contained file, opens straight off disk. Five tabs:

- **Market today** — where the money sits right now: price distribution, median by shelf,
  promo penetration.
- **Daily trend** — two things. First, **product price movement**: filter by product name,
  brand or shelf and chart up to 8 individual listings day by day, with a table of first
  price, latest price, change, low and high. Second, the market series: median with
  25th/75th percentiles, promo share, and coverage. A day with no collection renders as a
  **gap**, not a joined line, so a missed run never looks like a price move.
- **Price moves** — what changed since the previous run: drops, rises, new, delisted.
- **Brands and sellers** — share of shelf, rating against price, seller leaderboard.
- **SKU explorer** — filter and sort every listing; click one for its own price history.

Every chart has a table view underneath it.

## What gets captured

`date, platform, item_id, sku_id, product_name, brand_name, brand_raw, seller_name,
seller_id, category, subcategory, platform_category_ids, retail_price, promo_price,
discount_pct, currency, units_sold, units_sold_raw, rating, review_count, in_stock,
is_sponsored, location, url, query_bucket, collected_at`

The schema is deliberately platform-neutral: a future collector fills the same columns, so
the pipeline and dashboard never need to know which platform a row came from.

## Configuration

Edit these rather than the code.

- **`config/targets.csv`** — which shelves and keywords to sweep, and how many pages each.
  Order matters: a SKU found on several shelves keeps the subcategory of the first bucket
  that saw it, so keep specific shelves above the unfiltered search.
- **`config/brands.csv`** — brand alias to canonical name, plus an `is_mine` flag for when
  your own SKUs go live. Brands not listed pass through as Lazada reports them.
- **`config/settings.json`** — pacing, request cap, payload retention, locale, and
  **`dashboard_platforms`**: which platforms the processed tables and dashboard include.
  Currently `["lazada"]`, which is a no-op while Lazada is the only platform. It exists
  because a platform with a partial history distorts every day-over-day comparison, so such
  a platform can be held out rather than mixed in. **Raw snapshots in `data/raw/` are never
  touched by this.** `[]` means include everything.

## Reading the numbers honestly

- **`retail_price` is seller-declared.** Lazada lets sellers set their own list price, and
  many inflate it. In the first collection the median discount was 38.5%, and 35% of
  promoted listings claimed more than 50% off. Treat `promo_price` as the real number and
  `discount_pct` as a marketing claim.
- **A price change on one `item_id` is not always a repricing.** Lazada's search returns
  whichever **variant** is cheapest, so the price attached to a listing can jump because a
  different pack size took over. On 2026-08-17, 12 of 39 apparent price moves (31%) were
  variant switches. The pipeline compares `sku_id` between days and labels those
  `variant_switch` instead of a drop or rise; the dashboard tags them "pack size changed"
  and keeps them out of the movers chart. If you read a price series straight from
  `products_master.csv`, check `variant_changed` before trusting a jump.
- **`units_sold` is missing for about 45% of listings** and `rating` for about 40%. That is
  Lazada declining to show a badge, not a scraping failure. `units_sold_delta` in the
  pipeline is the more honest demand signal.
- **The sold counter can go backwards** when the platform re-windows it. Deltas are kept
  visible but clipped at zero wherever they feed a revenue figure.
- **Sponsored listings are flagged** and excluded from share-of-shelf so paid position does
  not read as real presence.
- **Day one has no comparison.** Price movement appears on the second run.

## Where this lives

The project moved to `C:\dev\ecom-price-tracker` on 2026-08-20, out of OneDrive, because sync
corrupts `.git`. The old folder under `personal_project\ecom_price_tracking` is frozen and
carries a MOVED banner; do not run anything from it.

Two guards written for the OneDrive era are kept, because both are cheap and still correct:

1. Every output is written to a temp file and then atomically swapped into place, so nothing
   can ever read a half-written CSV.
2. The pipeline aborts if it finds a `(conflict copy)` file in `data/`. If that happens,
   delete the file it names and re-run, otherwise the same day would be counted twice.

Also: `data/raw/` is append-only. Never edit or delete a past daily file. If a number is
wrong, fix `pipeline.py` and re-run, which is what keeps the output reproducible.

## Requirements

Python 3.11 via the `py` launcher, with `requests`, `pandas` and `numpy` from the existing
Anaconda install, plus:

```bash
py -m pip install playwright && py -m playwright install chromium
```

No virtual environment, deliberately. Chromium lives in Playwright's own cache, not in this
folder. On a CI runner the dependencies come from `requirements.txt` instead.

## Roadmap

- **Unattended collection** — the open question. A GitHub-hosted runner uses a US datacenter
  IP, which Thai marketplaces are expected to block and which would return non-Thai pricing.
  `.github/workflows/phase0-reachability.yml` is a manual-trigger spike that settles this before
  any schedule is written. If it fails, the two remaining routes are a self-hosted runner on
  Max's machine, which keeps the Thai home IP but needs the machine powered on, or a paid
  scraping API with Thai residential exit IPs.
- **More platforms** — only via a paid scraping API. Everything free and local has been tried.
