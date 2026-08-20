#!/usr/bin/env python3
"""Stage 1 - collect Lazada Thailand listings into a dated raw snapshot.

Lazada's category and search pages are rendered from a JSON payload that the same URL
returns directly when you append `?ajax=true`. That means no browser and no automation
fingerprint: one ordinary HTTPS GET per page of 40 items.

    https://www.lazada.co.th/shop-digestion-and-absorption/?ajax=true&q=probiotic&page=1

What gets read out of each item (verified against the live payload on 2026-08-10):

    name  brandName  sellerName  sellerId  price  originalPrice  discount
    ratingScore  review  itemSoldCntShow  itemId  skuId  categories
    location  inStock  isSponsored  itemUrl

Writes data/raw/YYYY-MM-DD-lazada.csv - one row per unique SKU, append-only by day.

Usage
    py collect_lazada.py                one full run from config/targets.csv
    py collect_lazada.py --check        one request, print a sample, write nothing
    py collect_lazada.py --pages 2      override the page count for every bucket
    py collect_lazada.py --bucket SLUG  only buckets whose slug matches
    py collect_lazada.py --lang-probe   compare Thai vs default product names
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common as C  # noqa: E402
from browser_fetch import AutoFetcher  # noqa: E402

PLATFORM = "lazada"
BASE = "https://www.lazada.co.th"


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def build_url(slug: str, query: str, page: int, ajax: bool, force_thai: bool) -> str:
    """Mirror the parameters Lazada's own JavaScript sends.

    Captured from the live page on 2026-08-11:
        /shop-digestion-and-absorption/?ajax=true&isFirstRequest=true&page=1&q=probiotic
    Anything extra makes the request look less like the site's own. An earlier `spm`
    tracking parameter was invented here and has been dropped.
    """
    path = "/catalog/" if slug.strip().lower() in ("", "catalog") else f"/{slug.strip('/')}/"
    params = {}
    if ajax:
        params["ajax"] = "true"
        if page == 1:
            params["isFirstRequest"] = "true"
    params["page"] = page
    params["q"] = query
    if ajax and force_thai:
        # Lazada translates results for a visitor it reads as non-Thai. Harmless in the
        # browser path, where the th-TH context already settles it.
        params["lang"] = "th"
    return BASE + path + "?" + urllib.parse.urlencode(params, encoding="utf-8")


def safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9ก-๙_-]+", "_", C.clean_text(text))[:40] or "x"


# ---------------------------------------------------------------------------
# Payload -> rows
# ---------------------------------------------------------------------------
def extract_items(payload: dict) -> tuple:
    """Returns (items, info). Empty items with a note means the page was blocked or
    genuinely had no results - the caller decides which."""
    if not isinstance(payload, dict):
        return [], {"note": "payload was not a JSON object"}
    mods = payload.get("mods") or {}
    items = mods.get("listItems") or []
    main = payload.get("mainInfo") or {}
    info = {
        "total_results": C.parse_int(main.get("totalResults")),
        "page_size": C.parse_int(main.get("pageSize")) or 40,
        "page": C.parse_int(main.get("page")) or 1,
        "note": "ok" if items else (main.get("errorMsg") or "no listItems in payload"),
    }
    return items, info


def normalise(item: dict, target: dict, run_date: str, collected_at: str, brands: dict) -> dict | None:
    item_id = C.clean_text(item.get("itemId"))
    if not item_id:
        return None

    name = C.clean_text(item.get("name"))
    brand_raw = C.clean_text(item.get("brandName"))
    brand = C.normalise_brand(brand_raw, name, brands)

    promo = C.parse_price(item.get("price"))
    retail = C.parse_price(item.get("originalPrice")) or promo
    # A listing with no promo repeats the same figure in both fields. Guard against the
    # occasional inverted pair rather than emitting a negative discount.
    if promo and retail and promo > retail:
        retail = promo
    discount_pct = round(100.0 * (retail - promo) / retail, 1) if (retail and promo and retail > 0) else 0.0

    sold_raw = C.clean_text(item.get("itemSoldCntShow"))
    categories = item.get("categories") or []

    return {
        "date": run_date,
        "platform": PLATFORM,
        "item_id": item_id,
        "sku_id": C.clean_text(item.get("skuId")),
        "product_name": name,
        "brand_name": brand,
        "brand_raw": brand_raw,
        "seller_name": C.clean_text(item.get("sellerName")),
        "seller_id": C.clean_text(item.get("sellerId")),
        "category": C.clean_text(target.get("category")),
        "subcategory": C.clean_text(target.get("subcategory")),
        "platform_category_ids": "|".join(str(c) for c in categories),
        "retail_price": retail if retail else "",
        "promo_price": promo if promo else "",
        "discount_pct": discount_pct,
        "currency": C.CURRENCY,
        "units_sold": C.parse_sold(sold_raw) if sold_raw else "",
        "units_sold_raw": sold_raw,
        "rating": C.parse_rating(item.get("ratingScore")) or "",
        "review_count": C.parse_int(item.get("review")) or 0,
        "in_stock": "yes" if C.truthy(item.get("inStock")) else "no",
        "is_sponsored": "yes" if (C.truthy(item.get("isSponsored")) or C.truthy(item.get("adFlag"))) else "no",
        "location": C.clean_text(item.get("location")),
        "url": C.absolute_url(item.get("itemUrl"), BASE),
        "query_bucket": f"{target.get('slug')}|{target.get('query')}",
        "collected_at": collected_at,
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def collect(args) -> int:
    settings = C.load_settings()
    force_thai = settings.get("product_name_language") == "force_thai"
    save_payloads = bool(settings.get("save_payloads")) and not args.no_payload

    targets = C.load_targets(PLATFORM)
    if args.bucket:
        needle = args.bucket.lower()
        targets = [t for t in targets if needle in t["slug"].lower() or needle in t["query"].lower()]
    if not targets:
        C.die("No enabled Lazada buckets matched. Check config/targets.csv.")

    run_date = args.date or C.today_iso()
    collected_at = C.now_iso()
    brands = C.load_brand_table()
    fetcher = AutoFetcher(settings, force_browser=args.browser, headless=not args.headed)

    by_item: dict = {}
    bucket_log = []
    print(f"Lazada collection for {run_date} - {len(targets)} bucket(s), max {fetcher.cap} requests")
    if fetcher.mode == "browser":
        print("  (browser mode forced)")

    try:
        _sweep(targets, args, fetcher, by_item, bucket_log, run_date, collected_at,
               brands, force_thai, save_payloads)
    finally:
        fetcher.close()

    rows = list(by_item.values())
    if not rows:
        if fetcher.blocked:
            C.die(
                "BLOCKED BY LAZADA'S ANTI-BOT WALL (Alibaba x5sec), even through the browser.\n\n"
                "  Nothing was written, so the last good snapshot is untouched and the\n"
                "  dashboard still shows it, with a staleness banner.\n\n"
                "  Things to try, in order:\n"
                "    1. Double-click  scripts\\run-headed.bat\n"
                "       A visible browser opens. If a slider or image challenge\n"
                "       appears, solve it once by hand and collection carries on\n"
                "       by itself. The profile is persistent, so it is remembered.\n"
                "    2. Raise delay_seconds_min / delay_seconds_max in config/settings.json\n"
                "       and cut 'pages' in config/targets.csv, then leave it a few hours.\n"
                "    3. If it keeps happening, the next step is a paid scraping API."
            )
        C.die("Nothing collected. Every bucket returned no rows. "
              "Run 'py collect_lazada.py --check' to see the raw response.")

    rows.sort(key=lambda r: (r["subcategory"], -(r["units_sold"] or 0) if isinstance(r["units_sold"], int) else 0))
    try:
        C.assert_rows_sane(rows, PLATFORM)
    except RuntimeError as exc:
        C.die(str(exc))
    out_path = os.path.join(C.RAW_DIR, f"{run_date}-{PLATFORM}.csv")
    C.atomic_write_csv(out_path, C.SCHEMA, rows)

    sponsored = sum(1 for r in rows if r["is_sponsored"] == "yes")
    promoed = sum(1 for r in rows if (r["discount_pct"] or 0) > 0)
    print(f"\nWrote {len(rows)} unique SKUs -> {os.path.normpath(out_path)}")
    print(f"  {fetcher.count} requests via {fetcher.mode}  |  {sponsored} sponsored  |  {promoed} on promotion")
    _field_report(rows)
    return 0


def _sweep(targets, args, fetcher, by_item, bucket_log, run_date, collected_at,
           brands, force_thai, save_payloads) -> None:
    for target in targets:
        slug, query = target["slug"], target["query"]
        pages = args.pages or target["pages"]
        label = f"{target['subcategory']} / {query}"
        got, new = 0, 0
        print(f"\n  {label}  ({slug}, {pages} page(s))")

        for page in range(1, pages + 1):
            url = build_url(slug, query, page, ajax=True, force_thai=force_thai)
            referer = build_url(slug, query, page, ajax=False, force_thai=force_thai)
            payload, note = fetcher.get_json(url, referer=referer)

            if note == C.BLOCKED_SENTINEL:
                print(f"    page {page}: BLOCKED by Lazada's anti-bot wall")
                bucket_log.append({"bucket": label, "page": page, "items": 0, "note": note})
                break

            if payload is None:
                print(f"    page {page}: FAILED - {note}")
                bucket_log.append({"bucket": label, "page": page, "items": 0, "note": note})
                break

            items, info = extract_items(payload)
            if save_payloads:
                stamp = f"{run_date}-lazada-{safe_filename(slug)}-{safe_filename(query)}-p{page}.json.gz"
                os.makedirs(C.PAYLOAD_DIR, exist_ok=True)
                with gzip.open(os.path.join(C.PAYLOAD_DIR, stamp), "wt", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)

            page_new = 0
            for raw_item in items:
                row = normalise(raw_item, target, run_date, collected_at, brands)
                if not row:
                    continue
                got += 1
                existing = by_item.get(row["item_id"])
                if existing is None:
                    by_item[row["item_id"]] = row
                    page_new += 1
                    new += 1
                elif row["query_bucket"] not in existing["query_bucket"]:
                    # Same SKU surfaced by another bucket. Keep the first (most specific)
                    # subcategory, but record every bucket that found it.
                    existing["query_bucket"] += "," + row["query_bucket"]

            print(f"    page {page}: {len(items)} items ({page_new} new)"
                  + (f", {info['total_results']} total on Lazada" if page == 1 and info["total_results"] else ""))
            bucket_log.append({"bucket": label, "page": page, "items": len(items), "note": info["note"]})

            if len(items) < info["page_size"]:
                print("    (last page for this bucket)")
                break

        print(f"    -> {got} rows seen, {new} new unique SKUs")

        if fetcher.blocked:
            # The wall is raised against the client, not the URL. Once even the browser
            # is walled, every remaining bucket returns the same captcha page.
            print("\n  Stopping early: the anti-bot wall is up for every available path.")
            break


# Fields Lazada only shows once a listing has traction. An empty value here is the
# platform declining to display a badge, not a scraping failure, so they are reported
# separately from the fields that must always be present.
_OPTIONAL_FIELDS = {
    "units_sold": "no sold badge shown (below Lazada's display threshold)",
    "rating": "no rating yet",
}


def rebuild_from_payloads(args) -> int:
    """Re-parse a day's saved JSON without touching the network.

    This is why save_payloads exists: when a parser bug turns up, the fix should not
    cost another 36 requests to Lazada.
    """
    run_date = args.date or C.today_iso()
    pattern = os.path.join(C.PAYLOAD_DIR, f"{run_date}-{PLATFORM}-*.json.gz")
    paths = sorted(__import__("glob").glob(pattern))
    if not paths:
        C.die(f"No saved payloads for {run_date} in {C.PAYLOAD_DIR}.")

    targets = {(t["slug"], t["query"]): t for t in C.load_targets(PLATFORM)}
    brands = C.load_brand_table()
    collected_at = C.now_iso()
    by_item: dict = {}

    print(f"Re-parsing {len(paths)} saved payload(s) for {run_date} - no network calls")
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
        # filename: DATE-lazada-<slug>-<query>-pN.json.gz
        stem = os.path.basename(path)[len(run_date) + len(PLATFORM) + 2:-len(".json.gz")]
        target = next((t for (slug, query), t in targets.items()
                       if stem.startswith(safe_filename(slug) + "-" + safe_filename(query) + "-p")), None)
        if target is None:
            print(f"  ! {os.path.basename(path)}: no matching bucket in targets.csv - skipped")
            continue
        items, _ = extract_items(payload)
        for raw_item in items:
            row = normalise(raw_item, target, run_date, collected_at, brands)
            if not row:
                continue
            existing = by_item.get(row["item_id"])
            if existing is None:
                by_item[row["item_id"]] = row
            elif row["query_bucket"] not in existing["query_bucket"]:
                existing["query_bucket"] += "," + row["query_bucket"]

    rows = list(by_item.values())
    if not rows:
        C.die("Payloads parsed to zero rows.")
    rows.sort(key=lambda r: (r["subcategory"], -(r["units_sold"] or 0) if isinstance(r["units_sold"], int) else 0))
    try:
        C.assert_rows_sane(rows, PLATFORM)
    except RuntimeError as exc:
        C.die(str(exc))
    out_path = os.path.join(C.RAW_DIR, f"{run_date}-{PLATFORM}.csv")
    C.atomic_write_csv(out_path, C.SCHEMA, rows)

    sponsored = sum(1 for r in rows if r["is_sponsored"] == "yes")
    promoed = sum(1 for r in rows if (r["discount_pct"] or 0) > 0)
    print(f"\nRebuilt {len(rows)} unique SKUs -> {os.path.normpath(out_path)}")
    print(f"  {sponsored} sponsored  |  {promoed} on promotion")
    _field_report(rows)
    return 0


def _field_report(rows: list) -> None:
    """The 11 fields Max asked for, and how completely they came back."""
    required = ["product_name", "retail_price", "promo_price", "date", "seller_name",
                "brand_name", "category", "subcategory", "review_count"]
    total = len(rows)
    print("\n  Field completeness (must always be present):")
    for field in required:
        filled = sum(1 for r in rows if str(r.get(field, "")).strip() != "")
        flag = " " if filled / total >= 0.9 else "!"
        print(f"   {flag} {field:<16} {C.pct(filled, total):>5.1f}%  ({filled}/{total})")

    print("\n  Shown only for listings with traction:")
    for field, why in _OPTIONAL_FIELDS.items():
        filled = sum(1 for r in rows if str(r.get(field, "")).strip() != "")
        print(f"     {field:<16} {C.pct(filled, total):>5.1f}%  ({filled}/{total})  - rest: {why}")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def check(args) -> int:
    settings = C.load_settings()
    force_thai = settings.get("product_name_language") == "force_thai"
    targets = C.load_targets(PLATFORM)
    if not targets:
        C.die("No enabled Lazada buckets in config/targets.csv.")
    target = targets[0]
    url = build_url(target["slug"], target["query"], 1, ajax=True, force_thai=force_thai)
    print(f"GET {url}\n")

    fetcher = AutoFetcher(settings, force_browser=args.browser, headless=not args.headed)
    try:
        payload, note = fetcher.get_json(
            url, referer=build_url(target["slug"], target["query"], 1, False, force_thai))
    finally:
        fetcher.close()
    if payload is None:
        C.die(f"Request failed: {note}")
    print(f"(fetched via {fetcher.mode})")

    items, info = extract_items(payload)
    print(f"items on page: {len(items)}   total on Lazada: {info['total_results']}   note: {info['note']}")
    if not items:
        return 1

    brands = C.load_brand_table()
    row = normalise(items[0], target, C.today_iso(), C.now_iso(), brands)
    print("\nSample row:")
    for key in C.SCHEMA:
        value = row.get(key, "")
        if isinstance(value, str) and len(value) > 90:
            value = value[:87] + "..."
        print(f"  {key:<22} {value}")
    print("\nNothing was written.")
    return 0


def lang_probe(args) -> int:
    """Lazada translates results for visitors it reads as non-Thai. This shows whether
    the Thai locale hints in settings.json are actually changing what comes back."""
    settings = C.load_settings()
    target = C.load_targets(PLATFORM)[0]
    fetcher = AutoFetcher(settings, force_browser=args.browser, headless=not args.headed)
    for force_thai in (False, True):
        url = build_url(target["slug"], target["query"], 1, True, force_thai)
        payload, note = fetcher.get_json(url, referer=build_url(target["slug"], target["query"], 1, False, force_thai))
        label = "force_thai" if force_thai else "as_returned"
        if payload is None:
            print(f"{label:<12} FAILED - {note}")
            continue
        items, _ = extract_items(payload)
        names = [C.clean_text(i.get("name"))[:70] for i in items[:3]]
        thai_chars = sum(1 for i in items if re.search(r"[ก-๙]", C.clean_text(i.get("name"))))
        print(f"\n{label:<12} {len(items)} items, {thai_chars} with Thai characters in the name")
        for n in names:
            print(f"   - {n}")
    fetcher.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Lazada Thailand probiotic listings.")
    parser.add_argument("--check", action="store_true", help="one request, print a sample row, write nothing")
    parser.add_argument("--lang-probe", action="store_true", help="compare Thai vs default product names")
    parser.add_argument("--pages", type=int, help="override pages per bucket")
    parser.add_argument("--bucket", help="only buckets whose slug or query contains this")
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--no-payload", action="store_true", help="skip saving the gzipped raw JSON")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-parse a day's saved payloads offline instead of scraping again")
    parser.add_argument("--browser", action="store_true",
                        help="skip the HTTP attempt and go straight to the browser")
    parser.add_argument("--headed", action="store_true",
                        help="show the browser window (use to solve a captcha by hand once)")
    args = parser.parse_args()

    if args.check:
        return check(args)
    if args.lang_probe:
        return lang_probe(args)
    if args.rebuild:
        return rebuild_from_payloads(args)
    return collect(args)


if __name__ == "__main__":
    sys.exit(main())
