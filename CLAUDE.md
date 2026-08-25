# CLAUDE.md — ecom_price_tracking

## Identity

This workstation owns competitor price and assortment intelligence on Thai marketplaces:
the scrapers that collect it, the pipeline that processes it, and the dashboard built on
top. Route here for "what is the competition charging", "who dropped price", "how deep are
promos running", "which brand is taking share of shelf", and anything touching the
collectors, the pipeline, or the dashboard. The role is commercial manager, not scraper
operator. The scrape is plumbing; the answer is what the market is doing and what to do
about it.

Covers Lazada Thailand for probiotic supplements, and only that. Shopee was built and
TikTok Shop was probed; both were abandoned on 2026-08-20 and deleted from the project.
Read the Current Scope section of `MEMORY.md` before proposing either again. Adding a new
platform is still possible, against the same schema. Does not cover Max's own Lazada seller setup, product
sourcing, or the probiotic brand plan, those are `probiotic project`. The boundary: this
workstation says what the market is doing, `probiotic project` decides what to sell.

## Resources

| Resource | Read when... |
|---|---|
| `README.md` | Setting up, running, or debugging any stage |
| `data/processed/data_quality.csv` | Before quoting any figure |
| `data/processed/price_changes.csv` | Answering "what moved" - read this before the master table |
| `config/targets.csv` | The question is about coverage: which shelves and keywords are swept |
| `notes/build-log-v1-20260819.md` | MEMORY.md does not explain something, or you need the history behind a decision |

## Workflow

1. Check `data_quality.csv` first. If a required field is flagged, or a conflict copy was
   found, say so alongside the figure rather than after it.
2. State the collection date with every number. Each snapshot is a point in time, and a
   listing that vanished may be delisted, out of stock, or just pushed off the pages swept.
   Never present those three as the same thing.
3. For "what changed" questions read `price_changes.csv`, not `products_master.csv`. It is
   built for exactly that and is small enough to reason over.
4. Never edit anything in `data/raw/` or `data/processed/` by hand. If a number is wrong the
   fix goes in the collector or the pipeline and everything gets rebuilt. That is what keeps
   the output reproducible. Use `run.bat rebuild` to re-parse saved payloads offline rather
   than re-scraping.
5. Run `run.bat test` after changing `pipeline.py`. It checks the day-over-day logic against
   a synthetic dataset and never touches `data/`.
5a. **`docs/index.html` is a copy of `dashboard/index.html`, not a second implementation.**
   GitHub Pages (Deploy from branch) only serves `/(root)` or `/docs`, and `dashboard/` is
   neither, so `docs/index.html` exists purely to satisfy that. The daily workflow re-copies
   it on every run, so a data-only day never drifts. If you edit `dashboard/index.html` by
   hand, `cp dashboard/index.html docs/index.html` before committing, or the published page
   falls behind until the next scheduled run overwrites it.
6. Adding a platform means writing a collector that emits the schema in `common.py`. Do not
   widen the schema for one platform, and do not let a platform-specific quirk reach the
   pipeline.
7. Scrape politely. Sequential requests, the delays in `config/settings.json`, public
   listing pages only, no login, no personal data. Do not add concurrency or remove the
   request cap. Never retry an anti-bot wall: it is raised against the client, not the URL,
   so knocking again only makes the block worse. The collector stops at the first one.
8. Never present a stale snapshot as current. If `days_since_collection` is above zero the
   last collection did not run or was blocked, and that fact leads the answer.
9. Record decisions and anything learned about the platforms' data in `MEMORY.md`.
10. **Collection is automated.** `.github/workflows/daily-collect.yml` runs at 08:00 Bangkok,
    collects, rebuilds and commits `data/` back to `main`. Max does not need to run anything
    for the data to stay current, so do not tell him to. `scripts\run.bat` is the local path
    to the same two entry points and the two must be kept in sync.
11. **A failed scheduled run is not the same as a blocked one.** The collector exits 2 for an
    anti-bot wall and 1 for anything else, and the workflow retries only exit 1. If a run
    failed, read the step summary before proposing a fix: it says which stage broke and whether
    the shelf shrank enough to suggest a sweep cut short.
12. **GitHub disables scheduled workflows after 60 days of repository inactivity.** The bot's
    own commits do not reset that timer. If collection silently stops, check this first.

## Editorial Rules

Follow my voice principles in 00_Resources (voice-principles.md).

- Lead with the number that answers the question, then explain it.
- Currency is THB, written ฿1,262 or 1,262 THB, consistent within one document.
- Distinguish list price from selling price every time. `retail_price` is seller-declared
  and routinely inflated on Lazada, so a discount percentage is a marketing claim, not a
  measured saving. Never quote a discount without that caveat when it is above 50%.
- Say "listings", not "products", when counting rows. One product sold by four sellers is
  four listings, and the difference matters for share of shelf.
- Flag confidence when a figure is derived. A revenue proxy built from sold-count deltas is
  an estimate and must be labelled as one.
- Sponsored placements are excluded from share metrics. If a figure includes them, say so.
- Give one recommendation with its trade-off stated plainly, not a menu.
- Code comments explain *why*, not *what*.
