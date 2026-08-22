"""Phase 0, probe 3: did this runner get served the Thai storefront?

Split out of the workflow rather than inlined, because a Python heredoc inside a YAML
block scalar inside a shell is three levels of quoting and one of them always loses.

Reads /tmp/body.json, written by probe 2. Writes single-word results to /tmp/r_* for the
verdict step to collect.
"""
import json
import pathlib
import re


def write(name: str, value) -> None:
    pathlib.Path(f"/tmp/{name}").write_text(str(value))


raw = pathlib.Path("/tmp/body.json").read_text(errors="replace")

try:
    payload = json.loads(raw)
except Exception as exc:                                       # noqa: BLE001
    print(f"Not JSON: {exc}")
    write("r_locale", "NOT_JSON")
    raise SystemExit(0)

items = (payload.get("mods", {}) or {}).get("listItems") or []
print(f"listItems: {len(items)}")

if not items:
    # No items with a 200 usually means a wall or an interstitial. Print enough of the
    # envelope to tell which, without dumping the whole body into the log.
    print("Top-level keys:", sorted(payload)[:12])
    print("ret:", payload.get("ret"))
    write("r_locale", "NO_ITEMS")
    write("r_items", 0)
    raise SystemExit(0)

write("r_items", len(items))

currencies = {i.get("currency") for i in items if i.get("currency")}
thai = sum(1 for i in items if re.search(r"[\u0e00-\u0e7f]", i.get("name") or ""))

print(f"currencies: {currencies or 'none reported'}")
print(f"names containing Thai characters: {thai}/{len(items)}")
for item in items[:3]:
    name = (item.get("name") or "")[:70]
    print(f"  - {name!r}  {item.get('currency')} {item.get('price')}")

ok_currency = (not currencies) or ("THB" in currencies)
# A quarter is a deliberately loose bar. Plenty of genuine Thai listings carry English
# brand names, so demanding a majority would produce false alarms.
ok_thai = thai >= max(1, len(items) // 4)

write("r_locale", "OK" if (ok_currency and ok_thai) else "WRONG_STOREFRONT")

if not ok_currency:
    print(f"::error::Currency is {currencies}, not THB. This runner is being served a non-Thai storefront.")
if not ok_thai:
    print("::warning::Product names are not Thai. Lazada is translating for a visitor it reads as non-Thai.")
