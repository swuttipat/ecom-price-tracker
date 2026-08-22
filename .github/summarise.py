"""Write the daily run summary to stdout, for $GITHUB_STEP_SUMMARY.

Runs with `if: always()`, so it must also produce something useful when collection failed.
In that case the processed tables still hold the PREVIOUS day and days_since_collection
says so, which is exactly the signal worth surfacing.

Deliberately stdlib only. If the dependency install is what broke, this still runs.
"""
import csv
import os
import pathlib
import sys

PROCESSED = pathlib.Path("data/processed")

# A sweep cut short by a wall still writes whatever it collected before stopping, and a
# short day would otherwise look like a market event: hundreds of listings reported "gone"
# that were only never fetched. Flag it loudly rather than trusting the number.
PARTIAL_SWEEP_RATIO = 0.75


def read_rows(name):
    path = PROCESSED / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def quality():
    return {r["metric"]: r["value"] for r in read_rows("data_quality.csv")}


def out(line=""):
    print(line)


q = quality()
if not q:
    out("## Daily collection")
    out()
    out("`data/processed/data_quality.csv` is missing. The pipeline did not run.")
    sys.exit(0)

latest = q.get("latest_date", "unknown")
stale = int(q.get("days_since_collection") or 0)
skus = int(q.get("skus_in_latest") or 0)

status = "collected" if stale == 0 else f"STALE by {stale} day(s)"
out(f"## Daily collection: {latest} ({status})")
out()

if stale > 0:
    out(f"> **No new snapshot today.** The newest data is from {latest}, {stale} day(s) ago.")
    out("> The dashboard shows its staleness banner. The previous snapshot was left untouched.")
    out()

# ---------------------------------------------------------------- market movement
market = [r for r in read_rows("market_daily.csv") if r.get("date")]
if market:
    today, prev = market[-1], (market[-2] if len(market) > 1 else None)
    out("| Metric | " + today["date"] + (f" | {prev['date']} |" if prev else " |"))
    out("|---|---|" + ("---|" if prev else ""))

    def row(label, key, suffix=""):
        a = today.get(key, "")
        b = f" {prev.get(key, '')}{suffix} |" if prev else ""
        out(f"| {label} | {a}{suffix} |{b}")

    row("Listings", "skus")
    row("Median selling price", "median_promo_price", " THB")
    row("On promotion", "promo_penetration_pct", "%")
    row("Brands", "brands")
    row("Sellers", "sellers")
    out()

    if prev:
        now_n, was_n = int(float(today["skus"])), int(float(prev["skus"]))
        if was_n and now_n < was_n * PARTIAL_SWEEP_RATIO:
            drop = 100 - (now_n / was_n * 100)
            out(f"> **Warning: the shelf shrank {drop:.0f}% overnight** ({was_n} to {now_n}).")
            out("> That is usually a sweep cut short, not a market event. Check whether the")
            out("> collector stopped early before reading anything into the change log.")
            out()

# ---------------------------------------------------------------- what moved
changes = read_rows("price_changes.csv")
if changes:
    counts = {}
    for r in changes:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    out("**Changes vs the previous snapshot:** "
        + ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in ordered))
    out()
    out("_A price change on one item_id is not always a repricing: Lazada surfaces whichever")
    out("variant is cheapest, so check `variant_changed` before calling it a market move._")
    out()

# ---------------------------------------------------------------- data quality
flagged = [(m, v) for m, v in q.items()
           if m.startswith("missing_") and (v or "0") not in ("0", "")]
if flagged:
    out("**Fields missing from the latest snapshot:** "
        + ", ".join(f"{m.removeprefix('missing_')} ({v})" for m, v in flagged))
    out()

dupes = q.get("duplicate_rows_dropped", "0")
if dupes not in ("0", ""):
    out(f"**{dupes} duplicate rows dropped.**")
    out()

out(f"_{skus} listings, {q.get('collection_dates', '?')} collection dates, "
    f"{q.get('unique_skus', '?')} unique SKUs across the history._")

run = os.environ.get("GITHUB_RUN_NUMBER")
if run:
    out(f"_Run #{run}. Full detail in `data/processed/data_quality.csv`._")
