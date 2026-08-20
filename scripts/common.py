#!/usr/bin/env python3
"""Shared plumbing for the e-commerce price tracker.

Everything that touches the filesystem goes through here, because this project lives
inside a OneDrive-synced folder. Three specific failures are guarded against:

  * a half-written file being picked up mid-sync  -> atomic_write()
  * a Files-On-Demand placeholder read as partial -> read_csv_rows() trailing-newline check
  * OneDrive silently forking a "(conflict copy)" -> assert_no_conflict_copies()

Also holds the parsers (price, sold count, rating), the brand normaliser, and the
polite HTTP fetcher every collector shares.
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
PAYLOAD_DIR = os.path.join(RAW_DIR, "_payloads")
PROCESSED_DIR = os.path.join(DATA_DIR, "processed")

CURRENCY = "THB"

# The Windows console defaults to cp1252, which cannot print Thai product names. Without
# this every run dies on a UnicodeEncodeError the moment it prints a title.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# The row contract. Every platform collector must emit exactly these columns so the
# pipeline never has to know which platform a row came from.
SCHEMA = [
    "date", "platform", "item_id", "sku_id", "product_name",
    "brand_name", "brand_raw", "seller_name", "seller_id",
    "category", "subcategory", "platform_category_ids",
    "retail_price", "promo_price", "discount_pct", "currency",
    "units_sold", "units_sold_raw", "rating", "review_count",
    "in_stock", "is_sponsored", "location", "url", "query_bucket", "collected_at",
]


# ---------------------------------------------------------------------------
# OneDrive-safe file IO
# ---------------------------------------------------------------------------
def atomic_write(path: str, text: str, encoding: str = "utf-8", retries: int = 6) -> str:
    """Write to a temp file in the same directory, then os.replace() it into place.

    os.replace is atomic on NTFS, so a reader (or OneDrive's uploader) sees either the
    old file or the new one, never a half-written one. The retry loop covers WinError 32,
    which is what you get when Excel or the sync client is holding the target open.
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")

    with open(tmp, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())

    last_error = None
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError as exc:          # WinError 32 - file locked
            last_error = exc
            time.sleep(0.6 * (attempt + 1))

    try:
        os.remove(tmp)
    except OSError:
        pass
    raise RuntimeError(
        f"Could not write {path} after {retries} attempts. "
        f"Close it in Excel, or wait for OneDrive to finish syncing, then re-run. ({last_error})"
    )


def atomic_write_csv(path: str, fieldnames: list, rows: list) -> str:
    """CSV written as utf-8-sig so Excel opens Thai text correctly on a Windows machine."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return atomic_write(path, buf.getvalue(), encoding="utf-8-sig")


def read_csv_rows(path: str) -> list:
    """Read a CSV, refusing to return a truncated one.

    Every file this project writes ends in a newline. A OneDrive placeholder that was
    only partially hydrated does not, so that single check catches the truncation
    failure mode documented in the workspace CLAUDE.md.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw.strip():
        raise RuntimeError(f"{path} is empty. If OneDrive shows a cloud icon, right-click it and pick 'Always keep on this device'.")
    if not raw.endswith(b"\n"):
        raise RuntimeError(
            f"{path} looks truncated (no trailing newline). This is usually OneDrive serving a "
            f"partially-downloaded file. Right-click the folder, choose 'Always keep on this device', then re-run."
        )
    text = raw.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


_CONFLICT_PATTERNS = ("conflict copy", "conflicted copy", "-desktop-", "_conflict")


def assert_no_conflict_copies(root: str = DATA_DIR) -> None:
    """Abort loudly rather than quietly folding a OneDrive fork into the history."""
    hits = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            low = name.lower()
            if any(p in low for p in _CONFLICT_PATTERNS):
                hits.append(os.path.join(dirpath, name))
    if hits:
        listing = "\n  ".join(hits)
        raise RuntimeError(
            "OneDrive conflict copies found in the data folder. Delete or rename them before "
            f"running again, otherwise the same day gets counted twice:\n  {listing}"
        )


def load_settings() -> dict:
    with open(os.path.join(CONFIG_DIR, "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load_targets(platform: str | None = None) -> list:
    rows = read_csv_rows(os.path.join(CONFIG_DIR, "targets.csv"))
    out = []
    for row in rows:
        if str(row.get("enabled", "")).strip().lower() not in ("yes", "y", "true", "1"):
            continue
        if platform and row.get("platform", "").strip().lower() != platform.lower():
            continue
        row["pages"] = int(str(row.get("pages") or 1).strip())
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
_PRICE_RE = re.compile(r"[\d.,]+")


def parse_price(value) -> float | None:
    """'1,262.00' / '฿1262' / 1262 -> 1262.0 ; '' / None / '0' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    match = _PRICE_RE.search(str(value))
    if not match:
        return None
    token = match.group(0).replace(",", "")
    # A trailing dot ("1262.") or multiple dots means the match ran into junk.
    parts = token.split(".")
    if len(parts) > 2:
        token = parts[0] + "." + parts[1]
    try:
        num = float(token.rstrip("."))
    except ValueError:
        return None
    return num if num > 0 else None


_SOLD_MULTIPLIERS = {
    "k": 1_000, "m": 1_000_000,
    "พัน": 1_000, "หมื่น": 10_000, "แสน": 100_000, "ล้าน": 1_000_000,
}
_SOLD_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(k|m|พัน|หมื่น|แสน|ล้าน)?",
    re.IGNORECASE,
)


def parse_sold(value) -> int | None:
    """Lazada shows sold counts in several shapes. Handles:

        '936 sold' -> 936        '1.2K sold' -> 1200      '10K+ sold' -> 10000
        'ขายแล้ว 936 ชิ้น' -> 936   'ขายได้ 1.2 พัน ชิ้น' -> 1200
        '' / None / 'No ratings yet' -> None
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _SOLD_RE.search(text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (match.group(2) or "").lower()
    return int(round(number * _SOLD_MULTIPLIERS.get(suffix, 1)))


def parse_rating(value) -> float | None:
    """'4.972081218274112' -> 4.97. Anything outside 0-5 is treated as missing."""
    num = None
    if value is None:
        return None
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if num <= 0 or num > 5:
        return None
    return round(num, 2)


def parse_int(value) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


_TRUE_TOKENS = {"true", "yes", "y", "1"}


def truthy(value) -> bool:
    """Lazada mixes real booleans with stringy ones: `isSponsored` is False but `adFlag`
    is the string '0', which Python would otherwise read as true."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    return str(value).strip().lower() in _TRUE_TOKENS


def clean_text(value) -> str:
    """Collapse whitespace and normalise Thai/Latin unicode so the same brand from two
    pages compares equal."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", " ", text).strip()


def absolute_url(url: str, base: str = "https://www.lazada.co.th") -> str:
    url = clean_text(url)
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base + url
    return url


# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------
_UNBRANDED = {"", "no brand", "nobrand", "none", "null", "n/a", "-", "ไม่ระบุ", "ไม่ระบุแบรนด์", "ไม่มีแบรนด์"}


def load_brand_table() -> dict:
    """alias (lowercased) -> {'canonical': str, 'is_mine': bool}"""
    path = os.path.join(CONFIG_DIR, "brands.csv")
    table = {}
    if not os.path.exists(path):
        return table
    for row in read_csv_rows(path):
        alias = clean_text(row.get("alias")).lower()
        canonical = clean_text(row.get("canonical"))
        if not alias or not canonical:
            continue
        table[alias] = {
            "canonical": canonical,
            "is_mine": str(row.get("is_mine", "")).strip().lower() in ("yes", "y", "true", "1"),
        }
    return table


def normalise_brand(raw, product_name: str, table: dict) -> str:
    """Map a platform brand string to a canonical name.

    Order: explicit alias -> passthrough of a real brand -> sniff a known brand out of
    the product title -> 'Unbranded'. Sniffing only ever matches brands already in
    brands.csv, so it cannot invent a name.
    """
    raw_clean = clean_text(raw)
    key = raw_clean.lower()

    if key in table:
        return table[key]["canonical"]
    if key and key not in _UNBRANDED:
        return raw_clean

    name_low = clean_text(product_name).lower()
    if name_low:
        # Longest alias first so "mega we care" wins over "mega".
        for alias in sorted(table, key=len, reverse=True):
            if len(alias) < 3:
                continue
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", name_low):
                return table[alias]["canonical"]
    return "Unbranded"


def brand_is_mine(canonical: str, table: dict) -> bool:
    target = clean_text(canonical).lower()
    return any(v["is_mine"] and v["canonical"].lower() == target for v in table.values())


# ---------------------------------------------------------------------------
# Polite HTTP
# ---------------------------------------------------------------------------
BLOCKED_SENTINEL = "BLOCKED_BY_ANTIBOT"

# Alibaba's wall has two shapes and both arrive as HTTP 200:
#   * an HTML captcha page          -> _____tmd_____/punish, x5secdata
#   * a small, VALID JSON envelope  -> {"ret":["FAIL_SYS_USER_VALIDATE","RGV587_ERROR::..."]}
# The second one is the dangerous one: it parses cleanly as JSON, so without this check it
# is mistaken for a real but empty response and every remaining bucket gets hammered.
_WALL_MARKERS = (
    "_____tmd_____/punish", "x5secdata", "captcha-verify",
    "FAIL_SYS_USER_VALIDATE", "RGV587_ERROR", "FAIL_SYS_ILLEGAL_ACCESS",
)


def is_antibot_wall(body: str) -> bool:
    if not body:
        return False
    head = body[:4000].lower()
    return any(marker.lower() in head for marker in _WALL_MARKERS)


class Fetcher:
    """Sequential, rate-limited, capped HTTP client.

    No concurrency by design. The cap is a safety net: a bad config edit can slow a run
    down but can never turn it into a hammering loop.
    """

    def __init__(self, settings: dict, verbose: bool = True):
        import requests  # imported here so --help works without the dependency

        self.settings = settings
        self.verbose = verbose
        self.count = 0
        self.blocked = False
        self.cap = int(settings.get("max_requests_per_run", 90))
        self.timeout = int(settings.get("request_timeout_seconds", 30))
        self.retries = int(settings.get("retries_per_request", 2))
        self._min = float(settings.get("delay_seconds_min", 2.0))
        self._max = float(settings.get("delay_seconds_max", 4.0))
        self._last_request_at = 0.0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": settings.get("user_agent", ""),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": settings.get("accept_language", "th-TH,th;q=0.9"),
            "Connection": "keep-alive",
        })

    def _wait(self):
        gap = random.uniform(self._min, self._max)
        elapsed = time.time() - self._last_request_at
        if self._last_request_at and elapsed < gap:
            time.sleep(gap - elapsed)

    def get_json(self, url: str, referer: str | None = None):
        """Returns (payload_dict_or_None, note). Never raises on a bad response.

        A note of BLOCKED_SENTINEL means an anti-bot wall, not a transport error. Those
        are never retried: the wall does not open on a second knock, and hammering it
        makes the block worse.
        """
        if self.count >= self.cap:
            return None, f"request cap reached ({self.cap})"

        headers = {"Referer": referer} if referer else {}
        last_note = "unknown error"

        for attempt in range(1, self.retries + 1):
            self._wait()
            self.count += 1
            self._last_request_at = time.time()
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            except Exception as exc:                      # noqa: BLE001 - network is untrusted
                last_note = f"{type(exc).__name__}: {exc}"
            else:
                body = resp.text if resp.content else ""
                if is_antibot_wall(body):
                    self.blocked = True
                    return None, BLOCKED_SENTINEL
                if resp.status_code == 200:
                    try:
                        return resp.json(), "ok"
                    except ValueError:
                        last_note = f"HTTP 200 but body was not JSON ({len(resp.content)} bytes)"
                else:
                    last_note = f"HTTP {resp.status_code}"

            if attempt < self.retries:
                backoff = 5 * attempt
                if self.verbose:
                    print(f"    ! {last_note} - retrying in {backoff}s")
                time.sleep(backoff)

        return None, last_note


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
# Fields that are meaningless to collect without. If a platform reshapes its payload the
# parser silently maps nothing, and rows arrive structurally valid but empty.
CRITICAL_FIELDS = ("product_name", "promo_price")
CRITICAL_MIN_FILLED = 0.5


def assert_rows_sane(rows: list, platform: str) -> None:
    """Refuse to write a snapshot that parsed to empty rows.

    A platform we no longer collect once moved its fields into a new JSON shape and the
    collector wrote 440 rows with 0% names and 0% prices. Nothing downstream noticed. A
    schema change must fail the run, not quietly poison the history.
    """
    if not rows:
        return
    for field in CRITICAL_FIELDS:
        filled = sum(1 for r in rows if str(r.get(field, "")).strip() != "")
        ratio = filled / len(rows)
        if ratio < CRITICAL_MIN_FILLED:
            raise RuntimeError(
                f"{platform}: '{field}' is empty on {100 * (1 - ratio):.0f}% of {len(rows)} rows.\n"
                f"  That is a parser problem, not a thin day - the platform has almost\n"
                f"  certainly changed its payload shape. Nothing was written, so the\n"
                f"  existing history is intact.\n\n"
                f"  Inspect a saved payload in data/raw/_payloads/ to see the new field\n"
                f"  names, fix the mapping, then re-parse offline with --rebuild."
            )


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def pct(numerator, denominator) -> float:
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def die(message: str) -> None:
    print(f"\nERROR: {message}\n", file=sys.stderr)
    sys.exit(1)
