#!/usr/bin/env python3
"""Stages 2 and 3 - store and process.

Reads every dated snapshot in data/raw/, merges them into a single history, works out
what moved since the previous collection, and writes the tables the dashboard reads.

    data/raw/YYYY-MM-DD-<platform>.csv
        -> products_master.csv     full history, one row per SKU per day
        -> latest.csv              newest snapshot per SKU
        -> price_changes.csv       what moved since the previous collection date
        -> brand_daily.csv         brand rollup with share of shelf
        -> seller_daily.csv        seller rollup
        -> subcategory_daily.csv   subcategory rollup with promo penetration
        -> data_quality.csv        read this before quoting any number
        -> app_data.js             window.ECOM_DATA for dashboard/index.html

Usage
    py pipeline.py                  rebuild everything from data/raw/
    py pipeline.py --data-dir DIR   use a different data folder (used by the tests)
    py pipeline.py --quiet          less console output
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common as C  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

NUMERIC = ["retail_price", "promo_price", "discount_pct", "units_sold", "rating", "review_count"]
REQUIRED_FIELDS = ["product_name", "retail_price", "promo_price", "date", "seller_name",
                   "brand_name", "category", "subcategory", "review_count"]
# Lazada only renders these once a listing has traction. Blank means "no badge shown",
# not "the scraper missed it", so they are reported without a warning flag.
OPTIONAL_FIELDS = {
    "units_sold": "no sold badge on the listing - Lazada hides it below a threshold",
    "rating": "no rating yet - listing has no reviews",
}
HISTORY_DAYS = 90


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def load_raw(raw_dir: str, verbose: bool = True) -> pd.DataFrame:
    paths = sorted(p for p in glob.glob(os.path.join(raw_dir, "*.csv")) if not os.path.basename(p).startswith("_"))
    if not paths:
        C.die(f"No snapshots in {raw_dir}. Run 'py collect_lazada.py' first.")

    frames = []
    for path in paths:
        rows = C.read_csv_rows(path)          # raises on a truncated OneDrive placeholder
        if not rows:
            print(f"  ! {os.path.basename(path)} has a header but no rows - skipped")
            continue
        frames.append(pd.DataFrame(rows))
        if verbose:
            print(f"  {os.path.basename(path):<32} {len(rows):>5} rows")

    df = pd.concat(frames, ignore_index=True)
    for col in C.SCHEMA:
        if col not in df.columns:
            df[col] = ""
    for col in NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = df["date"].astype(str).str.strip()
    df["item_key"] = df["platform"].astype(str) + ":" + df["item_id"].astype(str)
    return df


def scope_platforms(df: pd.DataFrame, verbose: bool = True) -> tuple:
    """Keep only the platforms named in settings.dashboard_platforms.

    A platform with a partial history distorts everything that compares days: its SKUs
    inflate market totals on the days it ran and read as delistings on the days it did not.
    Raw snapshots are untouched, so re-including a platform is a one-word config change.
    """
    allowed = C.load_settings().get("dashboard_platforms") or []
    if not allowed:
        return df, []
    allowed = [str(p).strip().lower() for p in allowed]
    present = set(df["platform"].astype(str).str.lower().unique())
    dropped = sorted(present - set(allowed))
    if not dropped:
        return df, []
    before = len(df)
    df = df[df["platform"].astype(str).str.lower().isin(allowed)].reset_index(drop=True)
    if verbose:
        print(f"  scope: keeping {', '.join(allowed)}; excluded {', '.join(dropped)} "
              f"({before - len(df)} rows). Raw files are untouched.")
    if df.empty:
        C.die("dashboard_platforms in config/settings.json excluded every row. "
              "Widen it or set it to [] to include everything.")
    return df, dropped


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """One row per SKU per platform per day. A re-run on the same day supersedes the
    earlier one rather than double-counting it."""
    before = len(df)
    df = (df.sort_values(["date", "platform", "item_id", "collected_at"])
            .drop_duplicates(subset=["date", "platform", "item_id"], keep="last")
            .reset_index(drop=True))
    return df, before - len(df)


# ---------------------------------------------------------------------------
# Derived tables
# ---------------------------------------------------------------------------
def add_deltas(df: pd.DataFrame) -> pd.DataFrame:
    """Day-over-day movement per SKU, computed against that SKU's own previous
    observation (not necessarily yesterday, if a run was missed)."""
    df = df.sort_values(["item_key", "date"]).copy()
    grouped = df.groupby("item_key", sort=False)
    df["prev_date"] = grouped["date"].shift(1)
    df["prev_promo"] = grouped["promo_price"].shift(1)
    df["prev_retail"] = grouped["retail_price"].shift(1)
    df["prev_discount_pct"] = grouped["discount_pct"].shift(1)
    df["prev_units_sold"] = grouped["units_sold"].shift(1)
    # Lazada's search surfaces whichever VARIANT is cheapest, so the price attached to an
    # item_id can jump because a different pack size took over, not because anything was
    # repriced. Observed 2026-08-17: item 16160468309 went 55 -> 240 THB purely because
    # sku_id changed from ...371 to ...370. Tracking sku_id keeps those out of price moves.
    df["prev_sku_id"] = grouped["sku_id"].shift(1)

    df["promo_change"] = df["promo_price"] - df["prev_promo"]
    df["promo_change_pct"] = np.where(
        df["prev_promo"].fillna(0) > 0,
        (df["promo_change"] / df["prev_promo"] * 100).round(1),
        np.nan,
    )
    df["discount_pct_change"] = (df["discount_pct"] - df["prev_discount_pct"]).round(1)
    # Platforms sometimes reset or re-window their sold counter, which shows up as a
    # negative delta. Keep it visible here; clip it only where it feeds a revenue figure.
    df["units_sold_delta"] = df["units_sold"] - df["prev_units_sold"]

    both_known = df["prev_sku_id"].notna() & (df["prev_sku_id"].astype(str).str.strip() != "") \
        & (df["sku_id"].astype(str).str.strip() != "")
    df["variant_changed"] = np.where(
        both_known & (df["prev_sku_id"].astype(str) != df["sku_id"].astype(str)), "yes", "no")
    return df


def label_status(row) -> str:
    if pd.isna(row["prev_date"]):
        return "new"
    # A different variant is now the one on show. The number moved, but nothing was
    # repriced, so calling it a drop or a rise would be false. This must be tested before
    # the price comparison below.
    if row.get("variant_changed") == "yes" and pd.notna(row["promo_change"]) \
            and abs(row["promo_change"]) >= 0.5:
        return "variant_switch"
    if pd.notna(row["promo_change"]) and abs(row["promo_change"]) >= 0.5:
        return "price_drop" if row["promo_change"] < 0 else "price_rise"
    prev_d, now_d = row["prev_discount_pct"] or 0, row["discount_pct"] or 0
    if prev_d == 0 and now_d > 0:
        return "promo_started"
    if prev_d > 0 and now_d == 0:
        return "promo_ended"
    return "unchanged"


def promo_event(row) -> str:
    prev_d = 0 if pd.isna(row["prev_discount_pct"]) else row["prev_discount_pct"]
    now_d = 0 if pd.isna(row["discount_pct"]) else row["discount_pct"]
    if pd.isna(row["prev_date"]):
        return "n/a"
    if prev_d == 0 and now_d > 0:
        return "started"
    if prev_d > 0 and now_d == 0:
        return "ended"
    if now_d > prev_d + 0.5:
        return "deepened"
    if now_d < prev_d - 0.5:
        return "shallower"
    return "none"


def build_changes(df: pd.DataFrame, latest_date: str) -> pd.DataFrame:
    """Movement into the latest date, plus SKUs that vanished out of it."""
    cols = ["date", "prev_date", "platform", "item_id", "product_name", "brand_name",
            "seller_name", "subcategory", "prev_retail", "retail_price",
            "prev_promo", "promo_price", "promo_change", "promo_change_pct",
            "prev_discount_pct", "discount_pct", "discount_pct_change",
            "units_sold_delta", "status", "promo_event",
            "variant_changed", "prev_sku_id", "sku_id", "url"]

    current = df[df["date"] == latest_date].copy()
    if current.empty:
        return pd.DataFrame(columns=cols)

    current["status"] = current.apply(label_status, axis=1)
    current["promo_event"] = current.apply(promo_event, axis=1)

    # SKUs present on the previous date but absent today: delisted, out of stock, or
    # pushed off the pages we crawl. Worth seeing either way.
    #
    # Only for platforms that actually collected today. A platform that failed to collect
    # would otherwise have its entire catalogue read as delisted overnight - that bug once
    # turned 261 real disappearances into a headline of 701.
    dates = sorted(df["date"].unique())
    gone = pd.DataFrame(columns=cols)
    if len(dates) > 1:
        prev_date = dates[-2]
        platforms_today = set(current["platform"].unique())
        prev_rows = df[(df["date"] == prev_date) & (df["platform"].isin(platforms_today))]
        missing_keys = set(prev_rows["item_key"]) - set(current["item_key"])
        if missing_keys:
            gone = prev_rows[prev_rows["item_key"].isin(missing_keys)].copy()
            gone["prev_date"] = gone["date"]
            gone["date"] = latest_date
            gone["prev_promo"] = gone["promo_price"]
            gone["prev_retail"] = gone["retail_price"]
            gone["prev_discount_pct"] = gone["discount_pct"]
            for col in ["promo_price", "retail_price", "discount_pct", "promo_change",
                        "promo_change_pct", "discount_pct_change", "units_sold_delta"]:
                gone[col] = np.nan
            gone["status"] = "gone"
            gone["promo_event"] = "n/a"

    out = pd.concat([current, gone], ignore_index=True)
    out = out[out["status"] != "unchanged"]
    for col in cols:
        if col not in out.columns:
            out[col] = np.nan
    order = {"price_drop": 0, "promo_started": 1, "price_rise": 2, "promo_ended": 3,
             "new": 4, "gone": 5, "variant_switch": 6}
    out["_o"] = out["status"].map(order).fillna(9)
    out = out.sort_values(["_o", "promo_change_pct"], na_position="last")
    return out[cols].round(2)


def _revenue_proxy(group: pd.DataFrame) -> float:
    """promo price x units sold since the last run. Clipped at zero because a platform
    counter reset is not a negative sale."""
    delta = group["units_sold_delta"].clip(lower=0).fillna(0)
    return float((group["promo_price"].fillna(0) * delta).sum())


def rollup(df: pd.DataFrame, key: str) -> pd.DataFrame:
    organic = df[df["is_sponsored"] != "yes"]
    totals = organic.groupby("date")["item_key"].nunique().rename("date_sku_total")

    rows = []
    for (day, value), group in organic.groupby(["date", key], sort=False):
        if not str(value).strip():
            continue
        rows.append({
            "date": day,
            key: value,
            "sku_count": int(group["item_key"].nunique()),
            "median_promo_price": round(float(group["promo_price"].median(skipna=True) or 0), 2),
            "min_promo_price": round(float(group["promo_price"].min(skipna=True) or 0), 2),
            "avg_discount_pct": round(float(group["discount_pct"].mean(skipna=True) or 0), 1),
            "promo_sku_count": int((group["discount_pct"].fillna(0) > 0).sum()),
            "avg_rating": round(float(group["rating"].mean(skipna=True) or 0), 2),
            "total_reviews": int(group["review_count"].fillna(0).sum()),
            "units_sold_total": int(group["units_sold"].fillna(0).sum()),
            "units_sold_delta": int(group["units_sold_delta"].fillna(0).sum()),
            "revenue_proxy": round(_revenue_proxy(group), 2),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.merge(totals, on="date", how="left")
    out["share_of_shelf_pct"] = (out["sku_count"] / out["date_sku_total"] * 100).round(1)
    out["promo_penetration_pct"] = (out["promo_sku_count"] / out["sku_count"] * 100).round(1)
    out = out.drop(columns=["date_sku_total"])
    return out.sort_values(["date", "sku_count"], ascending=[True, False])


def days_old(iso_date: str) -> int:
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except ValueError:
        return 0


def lapsed_platforms(df: pd.DataFrame, latest_date: str) -> list:
    """Platforms that collected on the previous date but not the latest one."""
    dates = sorted(df["date"].unique())
    if len(dates) < 2:
        return []
    before = set(df[df["date"] == dates[-2]]["platform"].unique())
    now = set(df[df["date"] == latest_date]["platform"].unique())
    return sorted(before - now)


def build_market_daily(df: pd.DataFrame) -> pd.DataFrame:
    """One row per date per platform - the series the trend charts are drawn from.

    Sponsored listings are excluded so a heavy ad day does not read as a market move.
    """
    organic = df[df["is_sponsored"] != "yes"]
    rows = []
    for (day, platform), grp in organic.groupby(["date", "platform"], sort=True):
        promo = grp["promo_price"].dropna()
        discounted = grp["discount_pct"].fillna(0) > 0
        rows.append({
            "date": day,
            "platform": platform,
            "skus": int(grp["item_key"].nunique()),
            "median_promo_price": round(float(promo.median()), 2) if len(promo) else None,
            "median_retail_price": round(float(grp["retail_price"].median(skipna=True)), 2)
                                   if grp["retail_price"].notna().any() else None,
            "p25_promo_price": round(float(promo.quantile(0.25)), 2) if len(promo) else None,
            "p75_promo_price": round(float(promo.quantile(0.75)), 2) if len(promo) else None,
            "avg_discount_pct": round(float(grp["discount_pct"].mean(skipna=True) or 0), 1),
            "promo_penetration_pct": round(100.0 * discounted.mean(), 1) if len(grp) else 0.0,
            "brands": int(grp["brand_name"].nunique()),
            "sellers": int(grp["seller_name"].nunique()),
        })
    return pd.DataFrame(rows)


def build_quality(df: pd.DataFrame, raw_rows: int, dropped: int, latest: pd.DataFrame,
                  excluded_platforms: list | None = None) -> pd.DataFrame:
    dates = sorted(df["date"].unique())
    rows = [
        {"metric": "raw_rows_read", "value": raw_rows, "note": "before de-duplication"},
        {"metric": "duplicate_rows_dropped", "value": dropped, "note": "same SKU twice on the same date"},
        {"metric": "rows_in_master", "value": len(df), "note": ""},
        {"metric": "unique_skus", "value": int(df["item_key"].nunique()), "note": "across all dates"},
        {"metric": "collection_dates", "value": len(dates), "note": ", ".join(dates[-5:])},
        {"metric": "latest_date", "value": dates[-1] if dates else "", "note": ""},
        {"metric": "days_since_collection", "value": days_old(dates[-1]) if dates else "",
         "note": "0 means collected today"
                 + ("  <-- STALE, the last collection did not run or was blocked"
                    if dates and days_old(dates[-1]) > 0 else "")},
        {"metric": "skus_in_latest", "value": len(latest), "note": ""},
        {"metric": "sponsored_in_latest", "value": int((latest["is_sponsored"] == "yes").sum()),
         "note": "ad placements, excluded from share-of-shelf"},
        {"metric": "platforms_in_latest", "value": ", ".join(sorted(latest["platform"].unique())),
         "note": ""},
    ]
    if excluded_platforms:
        rows.append({
            "metric": "platforms_excluded_by_config", "value": ", ".join(excluded_platforms),
            "note": "left out on purpose via dashboard_platforms in config/settings.json; "
                    "raw snapshots are still on disk",
        })
    lapsed = lapsed_platforms(df, dates[-1] if dates else "")
    if lapsed:
        rows.append({
            "metric": "platforms_missing_today", "value": ", ".join(lapsed),
            "note": "collected yesterday but not today - their listings are held out of the "
                    "change log rather than counted as delisted  <-- CHECK",
        })
    def count_missing(field):
        if field in NUMERIC:
            return int(latest[field].isna().sum())
        return int((latest[field].astype(str).str.strip() == "").sum())

    for field in REQUIRED_FIELDS:
        missing = count_missing(field)
        rows.append({
            "metric": f"missing_{field}",
            "value": missing,
            "note": f"{C.pct(missing, len(latest))}% of the latest snapshot"
                    + ("  <-- CHECK" if missing / max(len(latest), 1) > 0.1 else ""),
        })
    for field, why in OPTIONAL_FIELDS.items():
        missing = count_missing(field)
        rows.append({
            "metric": f"no_{field}_shown",
            "value": missing,
            "note": f"{C.pct(missing, len(latest))}% of listings - {why}",
        })
    unparsed = int(((latest["units_sold_raw"].astype(str).str.strip() != "") & latest["units_sold"].isna()).sum())
    rows.append({"metric": "unparsed_sold_strings", "value": unparsed,
                 "note": "platform showed a sold count the parser could not read"})
    if len(dates) < 2:
        rows.append({"metric": "comparison_available", "value": "no",
                     "note": "only one collection date so far - price movement starts on the second run"})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Dashboard payload
# ---------------------------------------------------------------------------
def build_app_data(df, latest, changes, brands, sellers, subcats, quality, latest_date, prev_date,
                   market_daily=None):
    recent_dates = sorted(df["date"].unique())[-HISTORY_DAYS:]
    hist_df = df[df["date"].isin(recent_dates)]

    def num(value, digits=2):
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return None
        return round(float(value), digits)

    skus = []
    for _, r in latest.iterrows():
        skus.append({
            "id": r["item_id"],
            "plat": r["platform"],
            "name": r["product_name"],
            "brand": r["brand_name"],
            "seller": r["seller_name"],
            "subcat": r["subcategory"],
            "retail": num(r["retail_price"]),
            "promo": num(r["promo_price"]),
            "disc": num(r["discount_pct"], 1),
            "sold": num(r["units_sold"], 0),
            "soldD": num(r["units_sold_delta"], 0),
            "rating": num(r["rating"]),
            "reviews": num(r["review_count"], 0),
            "spon": r["is_sponsored"] == "yes",
            "stock": r["in_stock"] == "yes",
            "url": r["url"],
        })

    # Price history, only where there is more than one observation to draw.
    series = {}
    for item_key, group in hist_df.groupby("item_key"):
        if len(group) < 2:
            continue
        item_id = group.iloc[0]["item_id"]
        series[item_id] = [[d, num(p), num(rt)] for d, p, rt in
                           zip(group["date"], group["promo_price"], group["retail_price"])]

    promo_prices = latest["promo_price"].dropna()
    if len(promo_prices) > 4:
        lo, hi = float(promo_prices.quantile(0.02)), float(promo_prices.quantile(0.98))
        counts, edges = np.histogram(promo_prices.clip(lo, hi), bins=12)
        hist = {"edges": [round(float(e)) for e in edges], "counts": [int(c) for c in counts]}
    else:
        hist = {"edges": [], "counts": []}

    organic = latest[latest["is_sponsored"] != "yes"]
    by_platform = [
        {
            "platform": name,
            "skus": int(len(grp)),
            "median_promo": num(grp["promo_price"].median()),
            "avg_discount": num(grp["discount_pct"].mean(), 1),
            "brands": int(grp["brand_name"].nunique()),
            "sellers": int(grp["seller_name"].nunique()),
        }
        for name, grp in latest.groupby("platform")
    ]
    kpis = {
        "n_skus": int(len(latest)),
        "by_platform": by_platform,
        "n_organic": int(len(organic)),
        "n_brands": int(organic["brand_name"].nunique()),
        "n_sellers": int(organic["seller_name"].nunique()),
        "median_promo": num(organic["promo_price"].median()),
        "median_retail": num(organic["retail_price"].median()),
        "avg_discount": num(organic["discount_pct"].mean(), 1),
        "promo_penetration": num((organic["discount_pct"].fillna(0) > 0).mean() * 100, 1),
        "avg_rating": num(organic["rating"].mean()),
        "total_reviews": int(organic["review_count"].fillna(0).sum()),
    }

    def frame_records(frame, limit=None):
        if frame is None or frame.empty:
            return []
        out = frame.replace({np.nan: None})
        if limit:
            out = out.head(limit)
        return json.loads(out.to_json(orient="records"))

    latest_brands = brands[brands["date"] == latest_date] if not brands.empty else brands
    latest_sellers = sellers[sellers["date"] == latest_date] if not sellers.empty else sellers
    latest_subcats = subcats[subcats["date"] == latest_date] if not subcats.empty else subcats

    payload = {
        "meta": {
            "generated_at": C.now_iso(),
            "currency": C.CURRENCY,
            "platforms": sorted(df["platform"].unique().tolist()),
            "dates": sorted(df["date"].unique().tolist()),
            "latest_date": latest_date,
            "prev_date": prev_date,
            "has_comparison": bool(prev_date),
            # A silently failed collection leaves the pipeline rebuilding yesterday's data.
            # The dashboard says so rather than looking freshly updated.
            "days_old": days_old(latest_date),
            "today": date.today().isoformat(),
            "lapsed_platforms": lapsed_platforms(df, latest_date),
            "excluded_platforms": [r["value"] for r in json.loads(quality.to_json(orient="records"))
                                   if r["metric"] == "platforms_excluded_by_config"],
        },
        "kpis": kpis,
        # True totals per status. The `changes` list below is capped for page weight, so
        # counting it would undercount: on 2026-08-14 the cap turned 261 delistings into
        # 144 on screen.
        "changeCounts": {k: int(v) for k, v in changes["status"].value_counts().items()}
                        if not changes.empty else {},
        "changesShown": int(min(len(changes), 400)),
        "changesTotal": int(len(changes)),
        # item_ids whose displayed variant changed at some point in the charted window. A
        # jump on their price line is a different pack size, not a repricing.
        "variantSwitched": sorted(set(
            hist_df.loc[hist_df["variant_changed"] == "yes", "item_id"].astype(str)
        )) if "variant_changed" in hist_df.columns else [],
        "hist": hist,
        "daily": frame_records(market_daily) if market_daily is not None else [],
        "skus": skus,
        "series": series,
        "changes": frame_records(changes, limit=400),
        "brands": frame_records(latest_brands.sort_values("sku_count", ascending=False)),
        "sellers": frame_records(latest_sellers.sort_values("sku_count", ascending=False), limit=60),
        "subcats": frame_records(latest_subcats.sort_values("sku_count", ascending=False)),
        "brandTrend": frame_records(brands[brands["brand_name"].isin(
            latest_brands.sort_values("sku_count", ascending=False)["brand_name"].head(8)
        )]) if not brands.empty else [],
        "quality": frame_records(quality),
    }
    return "window.ECOM_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Merge raw snapshots and build the dashboard data.")
    parser.add_argument("--data-dir", help="alternative data folder (default: ../data)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir) if args.data_dir else C.DATA_DIR
    raw_dir = os.path.join(data_dir, "raw")
    out_dir = os.path.join(data_dir, "processed")
    verbose = not args.quiet

    C.assert_no_conflict_copies(data_dir)

    if verbose:
        print("Reading snapshots...")
    df = load_raw(raw_dir, verbose)
    raw_rows = len(df)
    df, excluded_platforms = scope_platforms(df, verbose)
    df, dropped = dedupe(df)
    df = add_deltas(df)

    dates = sorted(df["date"].unique())
    latest_date = dates[-1]
    prev_date = dates[-2] if len(dates) > 1 else ""
    latest = df[df["date"] == latest_date].copy()

    changes = build_changes(df, latest_date)
    brands = rollup(df, "brand_name")
    sellers = rollup(df, "seller_name")
    subcats = rollup(df, "subcategory")
    market_daily = build_market_daily(df)
    quality = build_quality(df, raw_rows, dropped, latest, excluded_platforms)

    def write(name, frame):
        path = os.path.join(out_dir, name)
        C.atomic_write(path, frame.to_csv(index=False, lineterminator="\n"), encoding="utf-8-sig")
        return path

    master_cols = C.SCHEMA + ["prev_date", "prev_promo", "prev_retail", "promo_change",
                              "promo_change_pct", "discount_pct_change", "units_sold_delta",
                              "prev_sku_id", "variant_changed"]
    write("products_master.csv", df[[c for c in master_cols if c in df.columns]].round(2))
    write("latest.csv", latest[[c for c in master_cols if c in latest.columns]].round(2))
    write("price_changes.csv", changes)
    write("brand_daily.csv", brands)
    write("seller_daily.csv", sellers)
    write("subcategory_daily.csv", subcats)
    write("market_daily.csv", market_daily)
    write("data_quality.csv", quality)

    app_js = build_app_data(df, latest, changes, brands, sellers, subcats, quality,
                            latest_date, prev_date, market_daily)
    C.atomic_write(os.path.join(out_dir, "app_data.js"), app_js, encoding="utf-8")

    if verbose:
        print(f"\nProcessed {len(df)} rows across {len(dates)} date(s). Latest: {latest_date}")
        print(f"  {len(latest)} SKUs  |  {latest['brand_name'].nunique()} brands  "
              f"|  {latest['seller_name'].nunique()} sellers")
        if prev_date:
            counts = changes["status"].value_counts().to_dict()
            print(f"  vs {prev_date}: " + ", ".join(f"{v} {k}" for k, v in counts.items()) if counts
                  else f"  vs {prev_date}: nothing moved")
        else:
            print("  First collection - price movement appears from the second run onward.")
        print("\nData quality:")
        for _, r in quality.iterrows():
            if str(r["metric"]).startswith("missing_") and r["value"] == 0:
                continue
            print(f"  {r['metric']:<28} {str(r['value']):<8} {r['note']}")
        print(f"\nWrote 8 files -> {os.path.normpath(out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
