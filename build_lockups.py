"""
IPO lock-up expirations coming up (for the Binary Days emails and site).

When a company goes public, insiders and early investors usually agree not to sell for 180 days (the "lock-up").
When it ends, a lot of shares can hit the market at once and the stock often drops around that date, especially
when it trades far above the IPO price. This takes IPOs priced 150-200 days ago from Finnhub's free IPO calendar
and estimates each lock-up end as IPO date + 180 days.

Writes out/lockups.json. Runs once a day. Some lock-ups are 90 or 365 days, or end early on a price trigger;
those details are only in the prospectus, so every date here is marked as an estimate.
"""
import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

OUT_DIR = os.environ.get("OUT_DIR", "out")
OUT = os.path.join(OUT_DIR, "lockups.json")
CT = ZoneInfo("America/Chicago")
KEY = os.environ.get("FINNHUB_KEY", "").strip()
LOCKUP_DAYS = 180


def finnhub(path, **params):
    params["token"] = KEY
    for attempt in range(3):
        try:
            r = requests.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(4 + attempt * 4)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            print("finnhub failed:", path, e, file=sys.stderr)
            time.sleep(2)
    return None


def ipo_price(v):
    try:
        return float(str(v).split("-")[-1]) if v not in (None, "") else None
    except ValueError:
        return None


def main():
    today = dt.datetime.now(CT).date()
    if not KEY:
        print("No FINNHUB_KEY; skipping lock-ups.")
        return
    if "--force" not in sys.argv and os.path.exists(OUT):
        try:
            if json.load(open(OUT, encoding="utf-8")).get("day") == today.isoformat():
                print("Lock-ups already built today; keeping them.")
                return
        except Exception:  # noqa: BLE001
            pass
    # lock-ups ending from 3 days ago to 45 days ahead -> IPOs from 225 to 132 days ago; ask in two halves to stay under the result cap
    start, end = today - dt.timedelta(days=LOCKUP_DAYS + 45), today - dt.timedelta(days=LOCKUP_DAYS - 48)
    mid = start + (end - start) / 2
    rows = []
    for a, b in ((start, mid), (mid + dt.timedelta(days=1), end)):
        data = finnhub("calendar/ipo", **{"from": a.isoformat(), "to": b.isoformat()}) or {}
        rows += data.get("ipoCalendar") or []
        time.sleep(1.1)
    out, seen = [], set()
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or sym in seen or (r.get("status") or "").lower() not in ("priced", ""):
            continue
        try:
            ipo = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        unlock = ipo + dt.timedelta(days=LOCKUP_DAYS)
        if not (today - dt.timedelta(days=3) <= unlock <= today + dt.timedelta(days=45)):
            continue
        seen.add(sym)
        out.append({"symbol": sym, "name": r.get("name") or "", "exchange": r.get("exchange") or "", "ipo_date": ipo.isoformat(),
                    "ipo_price": ipo_price(r.get("price")), "ipo_shares": r.get("numberOfShares"), "ipo_value": r.get("totalSharesValue"),
                    "unlock": unlock.isoformat(), "estimated": True})
    out.sort(key=lambda x: (x["unlock"], x["symbol"]))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"day": today.isoformat(), "updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "lockup_days": LOCKUP_DAYS, "items": out}, f, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(out)} lock-ups ending between {today - dt.timedelta(days=3)} and {today + dt.timedelta(days=45)} (from {len(rows)} IPO rows)")
    for x in out[:40]:
        print(f"   {x['unlock']}  {x['symbol']:6} IPO {x['ipo_date']} at {x['ipo_price']}  {x['name'][:50]}")


if __name__ == "__main__":
    main()
