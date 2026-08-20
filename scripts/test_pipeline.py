#!/usr/bin/env python3
"""Offline check that the day-over-day logic is right, without waiting a day.

Builds a throwaway data folder in the temp directory: today's real snapshot, plus a
synthetic "yesterday" derived from it with known edits. Then runs the pipeline against
that folder and asserts price_changes.csv reports exactly those edits.

Nothing here touches data/. Run it after changing anything in pipeline.py.

    py test_pipeline.py
"""
from __future__ import annotations

import csv
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAILURES = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    sources = sorted(glob.glob(os.path.join(C.RAW_DIR, "*-lazada.csv")))
    if not sources:
        C.die("No real snapshot to build the test from. Run 'py collect_lazada.py' first.")
    source = sources[-1]
    today_rows = C.read_csv_rows(source)
    if len(today_rows) < 20:
        C.die(f"{source} has only {len(today_rows)} rows - too few to test with.")

    today = today_rows[0]["date"]
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    # Build "yesterday" from today's rows with edits we can predict exactly.
    dropped = today_rows[:2]          # present yesterday, absent today  -> "new" today
    kept = today_rows[2:]
    yesterday_rows = []
    expect_drop, expect_rise, expect_promo_started = None, None, None

    for index, row in enumerate(dict(r) for r in kept):
        row["date"] = yesterday
        promo = C.parse_price(row["promo_price"])
        retail = C.parse_price(row["retail_price"])
        if index == 0 and promo:
            row["promo_price"] = round(promo * 1.25, 2)      # today is a 20% DROP
            row["retail_price"] = max(retail or 0, row["promo_price"])
            expect_drop = row["item_id"]
        elif index == 1 and promo:
            row["promo_price"] = round(promo * 0.80, 2)      # today is a 25% RISE
            row["retail_price"] = max(retail or 0, row["promo_price"])
            expect_rise = row["item_id"]
        elif index == 2 and promo and retail:
            row["promo_price"] = retail                       # no promo yesterday
            row["discount_pct"] = 0
            expect_promo_started = row["item_id"]
        yesterday_rows.append(row)

    # Two SKUs that existed yesterday and are gone today.
    gone_ids = set()
    for row in (dict(r) for r in dropped):
        row["date"] = yesterday
        yesterday_rows.append(row)
        gone_ids.add(row["item_id"])

    # Two brand-new SKUs today: fabricate them by cloning and re-keying today's rows.
    new_ids = set()
    extra_today = []
    for offset, row in enumerate(dict(r) for r in today_rows[:2]):
        row["item_id"] = f"TEST{offset}"
        row["product_name"] = f"[test] brand-new listing {offset}"
        extra_today.append(row)
        new_ids.add(row["item_id"])

    tmp = tempfile.mkdtemp(prefix="ecom_pipeline_test_")
    try:
        raw = os.path.join(tmp, "raw")
        os.makedirs(os.path.join(tmp, "processed"), exist_ok=True)
        os.makedirs(raw, exist_ok=True)
        # Today must NOT contain the two delisted SKUs, otherwise they are not gone.
        today_file_rows = kept + extra_today
        C.atomic_write_csv(os.path.join(raw, f"{yesterday}-lazada.csv"), C.SCHEMA, yesterday_rows)
        C.atomic_write_csv(os.path.join(raw, f"{today}-lazada.csv"), C.SCHEMA, today_file_rows)

        print(f"Temp dataset: {len(yesterday_rows)} rows on {yesterday}, "
              f"{len(today_file_rows)} on {today}")
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "pipeline.py"), "--data-dir", tmp, "--quiet"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            C.die("pipeline.py exited non-zero on the test dataset.")

        changes = C.read_csv_rows(os.path.join(tmp, "processed", "price_changes.csv"))
        by_id = {r["item_id"]: r for r in changes}
        status_of = {r["item_id"]: r["status"] for r in changes}

        print(f"\nprice_changes.csv: {len(changes)} rows")
        check("injected price drop is reported as price_drop",
              status_of.get(expect_drop) == "price_drop", f"{expect_drop} -> {status_of.get(expect_drop)}")
        check("the drop is about -20%",
              abs(float(by_id[expect_drop]["promo_change_pct"]) + 20) < 0.6 if expect_drop in by_id else False,
              by_id.get(expect_drop, {}).get("promo_change_pct"))
        check("injected price rise is reported as price_rise",
              status_of.get(expect_rise) == "price_rise", f"{expect_rise} -> {status_of.get(expect_rise)}")
        check("the rise is about +25%",
              abs(float(by_id[expect_rise]["promo_change_pct"]) - 25) < 0.6 if expect_rise in by_id else False,
              by_id.get(expect_rise, {}).get("promo_change_pct"))
        check("new promotion is reported as promo_started",
              status_of.get(expect_promo_started) in ("promo_started", "price_drop"),
              f"{expect_promo_started} -> {status_of.get(expect_promo_started)}")
        check("both brand-new SKUs are reported as new",
              all(status_of.get(i) == "new" for i in new_ids), sorted(new_ids))
        check("both delisted SKUs are reported as gone",
              all(status_of.get(i) == "gone" for i in gone_ids), sorted(gone_ids))
        check("unchanged SKUs are not in the change log",
              len(changes) < len(today_rows) / 2, f"{len(changes)} of {len(today_rows)}")

        master = C.read_csv_rows(os.path.join(tmp, "processed", "products_master.csv"))
        check("master holds both dates",
              len({r["date"] for r in master}) == 2, sorted({r["date"] for r in master}))
        latest = C.read_csv_rows(os.path.join(tmp, "processed", "latest.csv"))
        check("latest.csv holds only today",
              {r["date"] for r in latest} == {today})
        check("gone SKUs are absent from latest.csv",
              not (gone_ids & {r["item_id"] for r in latest}))

        brands = C.read_csv_rows(os.path.join(tmp, "processed", "brand_daily.csv"))
        check("brand rollup covers both dates",
              len({r["date"] for r in brands}) == 2)
        # Shares are rounded to 1dp for readability. With a few hundred single-SKU brands
        # each rounding 0.08% up to 0.1%, the column oversums by a couple of points.
        shares = [float(r["share_of_shelf_pct"]) for r in brands if r["date"] == today]
        check("share of shelf sums to ~100%", abs(sum(shares) - 100) < 4, f"{sum(shares):.1f}%")

        app = os.path.join(tmp, "processed", "app_data.js")
        check("app_data.js written and non-trivial", os.path.getsize(app) > 10_000,
              f"{os.path.getsize(app):,} bytes")
        with open(app, encoding="utf-8") as fh:
            head = fh.read(30)
        check("app_data.js declares window.ECOM_DATA", head.startswith("window.ECOM_DATA ="))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: " + "; ".join(FAILURES))
        return 1
    print("All checks passed. Test data was removed; data/ was never touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
