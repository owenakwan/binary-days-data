"""
Economic calendar for the Binary Days site: this week's market-moving releases (Forex Factory's free weekly feed),
kept for two weeks so the page can show what already happened. Writes out/econ.json.
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import requests

OUT_DIR = os.environ.get("OUT_DIR", "out")
OUT = os.path.join(OUT_DIR, "econ.json")
CT = ZoneInfo("America/Chicago")
COUNTRIES = {"USD": "US", "EUR": "Euro area", "GBP": "UK", "JPY": "Japan", "CNY": "China", "CAD": "Canada"}


def main():
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers={"User-Agent": "Mozilla/5.0 binary-days"}, timeout=30)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:  # noqa: BLE001
        print("econ calendar unavailable:", e, file=sys.stderr)
        return
    try:
        with open(OUT, encoding="utf-8") as f:
            old = {e["id"]: e for e in json.load(f).get("events", [])}
    except (OSError, ValueError, KeyError):
        old = {}
    keep_from = (dt.datetime.now(CT) - dt.timedelta(days=14)).isoformat()
    events = {k: v for k, v in old.items() if v.get("time", "") >= keep_from}
    for e in rows:
        if e.get("country") not in COUNTRIES or e.get("impact") not in ("High", "Medium", "Low", "Holiday"):
            continue
        try:
            t = dt.datetime.fromisoformat(e["date"]).astimezone(CT)
        except (KeyError, ValueError):
            continue
        eid = f"{e['country']}|{e['title']}|{t:%Y-%m-%d}"
        events[eid] = {"id": eid, "time": t.isoformat(), "country": e["country"], "where": COUNTRIES[e["country"]], "title": e.get("title", ""),
                       "impact": e["impact"], "forecast": e.get("forecast") or "", "previous": e.get("previous") or ""}
    out = sorted(events.values(), key=lambda x: x["time"])
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "events": out}, f, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(out)} events ({sum(1 for x in out if x['country'] == 'USD' and x['impact'] == 'High')} high-impact US)")


if __name__ == "__main__":
    main()
