# ecom_price_tracking Memory

_Split 2026-08-19. This file holds only what still governs a decision. The full build history,
including every step of the abandoned Shopee and TikTok investigations, is preserved verbatim
in `notes/build-log-v1-20260819.md`. Read that when you need the story behind a choice, or when
something behaves in a way this file does not explain._

## Contacts

_None yet._

## Current Scope

- **Lazada only, decided 2026-08-17. Shopee and TikTok were deleted from the project on
  2026-08-20.** Do not rebuild either from scratch without reading this bullet first.
  - **Shopee** was fully built and worked, but only just: search is gated even for a logged-in
    real Chrome, so collection had to attach to a Chrome Max started himself, and the best
    observed reliability was roughly **2 good days in 5**. Not worth the operational cost.
  - **TikTok Shop** was probed and rejected: captcha on first page load, an SPA that renders
    nothing without it, and web API requests signed with X-Bogus/msToken that cannot be
    reproduced by hand. Harder than Shopee, never viable.
  - **If either is ever revisited, the only route is a paid scraping API.** That is a real open
    option, not a dead end, and one subscription would cover both. Everything free and local has
    been tried. The full investigation is in `notes/build-log-v1-20260819.md`.
- **The dashboard is scoped by config.** `dashboard_platforms` in `config/settings.json`;
  `scope_platforms()` filters the loaded frame before de-duplication so every processed table and
  the dashboard agree. `[]` means include everything. It is a no-op while Lazada is the only
  platform, and is kept as the guard for whatever comes next. **Raw snapshots are never touched
  by it.**

## Decisions That Still Bind

- **Lazada needs no browser, in the normal case.**
  `https://www.lazada.co.th/{slug}/?ajax=true&q={query}&page={n}` returns the page's own JSON:
  40 listings per request, 10 of the 11 required fields directly. Only subcategory is derived,
  from the category slug being crawled. Full sweep is 36 requests.
- **`lang=th` is required, or Lazada auto-translates product names to English** for a visitor it
  reads as non-Thai. Set via `product_name_language` in `config/settings.json`.
- **The collector falls back to a real browser when Lazada walls plain HTTP.**
  `scripts/browser_fetch.py`'s `AutoFetcher` tries HTTP first, then headless Chromium via
  Playwright, loading the ordinary category page and issuing the same ajax calls from inside it.
  Identical JSON, so schema, pipeline and dashboard were untouched. Runs go from ~3 to 6-8 minutes.
- **Wall markers must stay in `is_antibot_wall()`.** A slider-captcha response is *valid* JSON
  (`{"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::..."]}`), so the first version waved it
  through as an empty result. `FAIL_SYS_USER_VALIDATE`, `RGV587_ERROR` and
  `FAIL_SYS_ILLEGAL_ACCESS` are the markers. Never let a parseable envelope count as success.
- **Solving a captcha is Max's step, never Claude's.** `scripts\run.bat headed`, solve by hand
  once, and the persistent profile keeps the verified session. Best after a few quiet hours;
  trust score is what is actually depleted. Never retry a wall: it is raised against the client,
  not the URL, so knocking again deepens the block.
- **Diagnostics are not free.** Repeated `--check` runs and back-to-back test collections
  demonstrably earned later blocks. Budget requests to a walled platform deliberately.
- **The project stays inside OneDrive, with three guards**, at Max's request: atomic writes via
  `os.replace`, a trailing-newline check that refuses truncated reads, and a conflict-copy scan
  that aborts the pipeline. Plus one manual step, mark the folder "Always keep on this device".
  **When git arrives, use `git init --separate-git-dir C:\dev\git\ecom-price-tracker.git`** so the
  object database never sits in OneDrive.
- **A GitHub-hosted runner CAN collect Lazada. Measured 2026-08-20, and it overturns what
  this file used to say.** The old entry claimed cloud runners get US datacenter IPs which
  Thai marketplaces block and which return non-Thai pricing. That was assumed, carried over
  from the flight tracker's notes, and never tested. The Phase 0 spike tested it: from
  `20.118.29.115`, a US Azure address, Lazada returned **HTTP 200, the Thai storefront and 40
  listings on page one**, with no wall marker. A self-hosted runner is therefore not needed,
  and neither is a paid scraping API.
  - **What that measurement does not cover:** it is one request. A full sweep is 36 over
    several minutes, and Lazada's defences are partly behavioural and rate-based rather than
    purely IP reputation, so a datacenter IP that passes one call can still trip a wall
    partway through. The first complete scheduled run is the real proof.
  - `.github/workflows/phase0-reachability.yml` is kept rather than deleted. Re-run it before
    blaming the collector for anything that looks like a network problem.
- **Collection is automated. `.github/workflows/daily-collect.yml`, 01:00 UTC = 08:00 Asia/Bangkok,
  plus a manual Run workflow button.** Repo: `github.com/swuttipat/ecom-price-tracker`, private.
  It collects, rebuilds and commits `data/` back to `main` on its own.
  - **Confirmed working end to end on 2026-08-23**, run #2, 2m53s: 1,259 listings across the
    usual 16 buckets, zero nulls, rebuilt, committed as `data: daily collection 2026-08-23`
    and pushed without help. `collected_at` read 22:28 Bangkok rather than UTC, which is the
    `TZ` setting doing its job.
  - The runner collected **1,259 listings against the ~1,200 a local sweep usually returns**.
    Slightly deeper ranking coverage, not a schema difference. Worth watching, not acting on.
  - **The pre-automation history has holes: 08-19, 08-21 and 08-22.** Lazada serves no history,
    so those days are gone for good. The series is continuous from 08-23 onward.
  - **A wall against the runner is much less serious than a wall against Max's home IP, and the
    fix is different.** GitHub hands each run a different address from Azure's pool, so the next
    scheduled run starts from a clean one. A fixed home IP instead accumulates reputation damage,
    which is why it hit three walls in ten days. So: do not re-run a walled job, wait for
    tomorrow, and only treat three consecutive walls as a pattern worth cutting `pages` over.
    **`run-headed.bat` cannot fix a walled runner.** It builds a trusted browser profile on
    Max's laptop, against his own IP; the runner is a fresh container that inherits none of it.
  - **`run.bat dashboard` pulls before opening.** Once the runner started committing data, the
    local clone went stale on its own, and the dashboard reads local files. The full `run.bat`
    path deliberately does not pull, because after a local collection the newest data is on disk.
- **The public dashboard link is `https://swuttipat.github.io/ecom-price-tracker/`, added
  2026-08-24 at Max's request.** GitHub Pages, "Deploy from branch", serving `/docs`.
  - **The repo was public when this was set up, which was a mistake** - it was meant to be
    private from the first commit. GitHub Pages on a private repo still serves a public URL
    unless the account is on GitHub Enterprise, so switching to private afterwards would not
    have hidden anything already pushed; it only stops new leakage. Max was told this before
    Pages was enabled. If the repo is ever made private, the Pages URL keeps working exactly
    the same, because Pages visibility does not follow repo visibility on this plan.
  - **`docs/index.html` exists only because Pages cannot serve `dashboard/`.** It is a copy,
    kept in sync by the daily workflow's "Sync the dashboard into docs/" step. Its
    `../data/processed/app_data.js` reference resolves the same way `dashboard/index.html`'s
    does, both being one level under the repo root, so no path rewriting was needed.
  - **Anyone with the link can read every SKU, brand, seller and price in the dataset.** There
    is no login. Treat the link as public from here on: do not put anything in `data/` that
    would matter if it were public, and do not assume the private-repo intent still applies to
    dashboard content once Pages is live.
  - **`TZ: Asia/Bangkok` is set at job level and must stay.** The runner is UTC and the snapshot
    file is named by date, so without it a manual run before 07:00 Bangkok would misdate the day.
  - **The workflow retries exit 1 and never exit 2.** `common.EXIT_WALL` is 2, returned only for
    an anti-bot wall, because retrying a wall deepens the block. Transient failures get three
    attempts with backoff.
  - **`scripts\run.bat` and the workflow are two paths to the same work.** Both call
    `collect_lazada.py` then `pipeline.py`. Change one, change the other.
  - **GitHub disables scheduled workflows after 60 days of repository inactivity**, and the
    bot's own pushes with `GITHUB_TOKEN` do **not** reset that timer. A warning email arrives
    first; one manual Run workflow click re-arms it.
- **Give Max double-clickable files, not commands with arguments.** He hit three separate failures
  typing paths and args. Number them in run order when order matters, for example `1-...bat` then
  `2-...bat`.

## Analysis Traps, Learned The Hard Way

- **`item_id` prices are NOT comparable across days on their own.** Lazada's search returns
  whichever variant is cheapest, so a price can jump because a different pack size took over.
  On 2026-08-17, **12 of 39 apparent price moves (31%) were variant switches, not repricings.**
  `add_deltas()` tracks `prev_sku_id` and sets `variant_changed`; `label_status()` returns
  `variant_switch` ahead of the price comparison. **Never treat a `promo_price` delta as a
  repricing without checking `sku_id`.**
- **A platform that failed to collect is not a platform whose catalogue was delisted.** That bug
  once reported 701 items "gone" when the truth was 261 real delistings plus 440 uncollected.
  `build_changes()` computes "gone" only for platforms present on the latest date, and
  `data_quality.csv` carries `platforms_missing_today`.
- **Never confirm a chart renders by calling `chart.draw()`.** Every "painted=N" check in this
  project forced a paint first and therefore could not detect a chart that never renders on its
  own. The Cowork browser pane does not fire `requestAnimationFrame` while hidden, so canvases
  read `painted=0` there regardless of correctness. **Instead:** assert the chart object exists,
  the canvas is non-zero, the datasets and axis range are right, and `chart.update()` does not
  throw, then say plainly that visual rendering is unverified and ask Max to look.
- **Never let an auto-scaled axis magnify noise into signal.** A price moving 371.0 to 370.8 over
  8 days filled an entire plot with rounding wobble while every y-tick read the same "฿371".
  `lineChart()` now floors the y-axis span at 2% of the value (minimum 1 unit) and derives tick
  decimals from the resulting span.
- **The daily median price is a composition statistic, not a price signal.** About 20% of the
  ~1,200 listings turn over every day (08-16→17: 260 gone / 254 new; 08-17→18: 238 / 224) because
  Lazada reshuffles which listings reach the pages swept. The daily median therefore oscillates
  (499, 490, 490, 459, 490, 455, 499, 450, 459) with almost no repricing under it. On those same
  days the matched cohort was 932-982 listings **flat** out of ~1,000. **Measure price movement on
  a matched cohort, same `item_id` AND same `sku_id` on both dates, never on the daily median.**
- **That daily churn is ranking reshuffle, not delisting.** ~240-260 listings leave the swept pages
  each day and a similar number arrive. `build_changes()` counts "new" against the whole history,
  which is why 08-18 showed 224 listings absent the day before but only 47 never seen at all.
- **List prices are inflated.** Median discount 38.5%; 35% of promoted listings claim more than
  50% off. `retail_price` is seller-declared, so `promo_price` is the real number and any
  discount is a marketing claim.
- **"Unbranded" is about 37.6% of the shelf**, the largest single group, because Lazada does not
  require a brand field. `config/brands.csv` pulls known names out of product titles; add aliases
  there as they come up.

## Platform Notes

- **Lazada data quirks:**
  - `adFlag` is the **string** `'0'`, which is truthy in Python. `isSponsored` is a real boolean.
    Both go through `common.truthy()`. Getting this wrong flagged all 1,201 listings as sponsored
    on the first run.
  - `itemSoldCntShow` is Thai-formatted (`"936 ชิ้น"`) and absent for about 45% of listings.
    `ratingScore` is absent for about 40%. **Absence means Lazada shows no badge, not a scraping
    failure.**
  - `itemUrl` is protocol-relative (`//www.lazada.co.th/...`).
  - The Windows console is cp1252 and dies on Thai text. `common.py` reconfigures stdout to UTF-8
    at import.
- **Two rules earned on the platforms that were dropped, worth keeping for any new one.**
  **Never build a signed search request yourself** - capture the site's own XHR responses off the
  wire, because the headers are generated by its JavaScript. And **when a page works by hand but
  not from the collector, the difference is usually the tab, not the site**: a tab with no
  history, referer or scroll gets challenged where a warmed one sails through. That second insight
  is the first thing to check on any new platform.

## Engineering Rules

- **Navigation races are the recurring failure mode in this codebase.** Three separate crashes came
  from calling `page.evaluate` or `page.goto` while a site redirected underneath. Poll cookies on
  the context rather than the DOM where possible, wrap every page call in the wait loops, and retry
  `Execution context was destroyed` once after settling before treating it as a failure.
- **`page.evaluate` has no timeout of its own.** An in-page `fetch()` with no AbortController hung
  a whole run indefinitely. The JS aborts at `request_timeout_seconds`.
- **Never let a blocked run look like a successful one.** The collector stops at the first wall,
  `run.bat` ends with an unmissable banner, `data_quality.csv` carries `days_since_collection`, and
  the dashboard shows a red "these numbers are N days old" banner whenever the latest collection is
  not today.
- `data/raw/_payloads/` holds gzipped raw JSON per request. **`run.bat rebuild` re-parses a day
  offline**, which is how several parsing bugs were fixed without re-scraping. Always prefer it to
  a fresh sweep.
- Dashboard charts render lazily on first tab activation. Chart.js measures a canvas inside a
  `display:none` tab as 0x0 and never recovers, with or without a later `resize()`.
- Chart.js: pass plain rgba via `withAlpha`, never `color-mix()`, which throws
  `this._fn is not a function` from its colour animator. Set `Chart.defaults.animation.duration`
  and `.easing` as properties; assigning the whole object drops `easing`.
- The browser caches `index.html`. No-cache meta tags are in place and `run.bat` opens the
  dashboard with a `?t=<random>` buster. Batch gotcha: `pushd "..." && set "D=%CD%"` captures the
  directory from *before* the pushd, because `%CD%` expands when the line is parsed. Keep `pushd`,
  `set` and `popd` on separate lines.
- `run.bat test` runs 15 checks on the day-over-day logic against a synthetic second day, and never
  touches `data/`. Run it after changing `pipeline.py`.

## Working Notes

- **Baseline, first collection 2026-08-10:** 1,201 unique SKUs, 247 brands, 708 sellers, 7 shelves,
  36 requests. Median selling price ฿499 against a ฿790 median list price, 70.6% on promotion.
- **First week's trend, Lazada:** the market-wide median ran ฿499 → ฿450 → ฿459 (08-10 to 08-18),
  but **that series is mostly composition churn, not repricing**. See the matched-cohort trap
  above. Promo penetration held 64-71%, listing count around 1,200.
- **A Lazada platform campaign ran 2026-08-15 to 08-17 inclusive.** Established 2026-08-20 from the
  raw snapshots. 225 listings cut price on 08-15, held flat through 08-17, and 233 rose on 08-18.
  Of the 08-15 droppers still listed on 08-18, **92.1% rose again, and 98.3% of those went back to
  their exact 08-14 price, to the baht.** It spans 147 sellers and 80 brands, which no single
  seller could coordinate. So the 08-18 **"247 price_rise" spike is a real campaign close, not a
  collection artifact**, and the week's trend data is trustworthy. Cohort discount depth ran
  49.8% → 53.2% → 46.7% across the window. Watch for the next mid-month window around 09-15.
- **Third anti-bot wall, 2026-08-20**, after 08-11 and 08-18, roughly one every 4-5 days from
  Max's home IP. Plain HTTP *and* the headless browser were blocked on the first request and
  nothing was written. Max cleared it the same evening with `run-headed.bat`, and the 08-20
  sweep then completed normally: 1,210 listings, 16 buckets, 37 requests. **Only 08-19 is
  permanently missing.** Treat the recurrence as a standing condition of collecting from the
  home IP, not as an incident. Whether the GitHub runner's IP suffers the same wear is unknown
  and worth watching over the first fortnight of scheduled runs.
- `market_daily.csv` (one row per date per platform) feeds the Daily trend tab. A date with no
  collection renders as a **gap** (`spanGaps: false`) so a missed run can never look like a price
  move. Platform colours are pinned per platform (`PLATFORM_SLOT`), never by list position.
- Chart rendering was confirmed working by Max on 2026-08-18.
