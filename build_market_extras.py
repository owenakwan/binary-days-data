#!/usr/bin/env python3
"""
Extra data for the website and emails, built on top of earnings.json and catalysts.json:

  profiles.json        name, industry, sector group and market cap for every company on the earnings
                       calendar or catalyst board (Finnhub profile2; cached, refreshed every 30 days,
                       up to PROFILES_PER_RUN new lookups per run to stay under the free rate limit)
  earnings_moves.json  for companies reporting in the next 10 days: how much the stock usually moves on
                       earnings (actual earnings-day reactions when we know past report dates, otherwise
                       the average of its 4 biggest daily moves in the last year) and 30-day volatility.
                       Rebuilt once a day.

Usage: python build_market_extras.py      Env: FINNHUB_KEY, OUT_DIR
"""
import datetime as dt
import json
import math
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

CT = ZoneInfo("America/Chicago")
OUT_DIR = os.environ.get("OUT_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip()
PROFILES_PER_RUN = int(os.environ.get("PROFILES_PER_RUN", "240"))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

# Finnhub industry -> the sector groups used by the earnings filters
SECTORS = [
    ("Biotech & pharma", ("Biotechnology", "Pharmaceuticals", "Life Sciences Tools & Services")),
    ("Health care", ("Health Care", "Healthcare", "Medical")),
    ("Tech", ("Technology", "Semiconductors", "Software", "Communications", "Electrical Equipment", "Media", "Telecommunication")),
    ("Financials", ("Banking", "Financial Services", "Insurance", "Real Estate", "Capital Markets")),
    ("Consumer", ("Retail", "Consumer products", "Hotels", "Restaurants", "Leisure", "Food", "Beverages", "Textiles", "Tobacco", "Automobiles", "Auto")),
    ("Energy & materials", ("Energy", "Oil", "Gas", "Utilities", "Metals", "Mining", "Chemicals", "Packaging", "Paper", "Building")),
    ("Industrials", ("Industrial", "Aerospace", "Airlines", "Logistics", "Transportation", "Machinery", "Construction", "Marine", "Road", "Trading Companies", "Commercial Services", "Professional Services")),
]


def log(m):
    print(m, flush=True)


def load(name, default):
    try:
        with open(os.path.join(OUT_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    log(f"Wrote {path} ({os.path.getsize(path):,} bytes)")


def sector_of(industry):
    ind = (industry or "").lower()
    for group, keys in SECTORS:
        if any(k.lower() in ind for k in keys):
            return group
    return "Other" if ind else ""


# ----------------------------------------------------------------------------- profiles
def update_profiles(symbols, today):
    prof = load("profiles.json", {})
    items = prof.get("items", {})
    stale = (today - dt.timedelta(days=30)).isoformat()
    todo = [s for s in symbols if s not in items or items[s].get("t", "") < stale]
    todo.sort(key=lambda s: (s in items, s))  # never-seen first
    done = 0
    if FINNHUB_KEY:
        for s in todo[:PROFILES_PER_RUN]:
            try:
                r = requests.get("https://finnhub.io/api/v1/stock/profile2", params={"symbol": s, "token": FINNHUB_KEY}, timeout=15)
                if r.status_code == 429:
                    time.sleep(10)
                    continue
                p = r.json() if r.ok else {}
            except (requests.RequestException, ValueError):
                p = {}
            items[s] = {
                "n": (p.get("name") or "").strip(),
                "i": p.get("finnhubIndustry") or "",
                "g": sector_of(p.get("finnhubIndustry")),
                "c": round(p["marketCapitalization"]) if p.get("marketCapitalization") else None,  # $ millions
                "t": today.isoformat(),
            }
            done += 1
            time.sleep(1.05)
    log(f"Profiles: {len(items)} on file, {done} fetched this run, {max(0, len(todo) - done)} still queued")
    save("profiles.json", {"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "fields": {"n": "name", "i": "industry", "g": "sector group", "c": "market cap $M"}, "items": items})
    return items


# ----------------------------------------------------------------------------- earnings moves
def daily_closes(sym):
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": "1y", "interval": "1d"}, headers=UA, timeout=15)
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        days = [dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat() for t in res["timestamp"]]
        return [(d, c) for d, c in zip(days, closes) if c]
    except Exception:  # noqa: BLE001
        return []


def move_stats(series, past_reports):
    if len(series) < 40:
        return None
    rets = [(series[i][0], series[i][1] / series[i - 1][1] - 1) for i in range(1, len(series))]
    last30 = [r for _, r in rets[-30:]]
    mean = sum(last30) / len(last30)
    vol30 = math.sqrt(sum((r - mean) ** 2 for r in last30) / (len(last30) - 1)) * math.sqrt(252) * 100
    by_day = {d: r for d, r in rets}
    days = [d for d, _ in rets]
    actual = []
    for rep in past_reports:  # [date, hour]: reaction = bigger of report day and next trading day
        d = rep[0]
        if d in by_day:
            i = days.index(d)
            nxt = by_day[days[i + 1]] if i + 1 < len(days) else 0
            actual.append(max(abs(by_day[d]), abs(nxt)) * 100)
    big4 = sorted((abs(r) * 100 for _, r in rets), reverse=True)[:4]
    if len(actual) >= 2:
        typical, basis = sum(actual) / len(actual), f"avg of last {len(actual)} earnings reactions"
    else:
        typical, basis = sum(big4) / len(big4), "avg of its 4 biggest daily moves in the past year"
    return {"move": round(typical, 1), "basis": basis, "vol30": round(vol30), "max": round(max(big4), 1),
            "price": round(series[-1][1], 2), "chg1m": round((series[-1][1] / series[max(0, len(series) - 22)][1] - 1) * 100, 1)}


def update_moves(today, profiles):
    prev = load("earnings_moves.json", {})
    if prev.get("date") == today.isoformat() and prev.get("items"):
        log("Earnings moves: already built today")
        return
    earn = load("earnings.json", {})
    end = (today + dt.timedelta(days=10)).isoformat()
    upcoming = {}
    for r in earn.get("rows", []):
        if today.isoformat() <= (r[1] or "") <= end and r[0] not in upcoming:
            upcoming[r[0]] = {"date": r[1], "hour": r[2]}
    history = earn.get("history", {})
    # skip tiny companies (under $150M) when we know the size; they move a lot for reasons nobody can trade
    syms = [s for s in upcoming if (profiles.get(s, {}).get("c") or 1e9) >= 150]
    log(f"Earnings moves: measuring {len(syms)} companies reporting by {end}")
    items = {}
    for i, s in enumerate(syms):
        st = move_stats(daily_closes(s), history.get(s, []))
        if st:
            items[s] = {**st, **upcoming[s]}
        if i % 100 == 99:
            log(f"  {i + 1}/{len(syms)}")
        time.sleep(0.12)
    top = sorted(items.items(), key=lambda kv: -kv[1]["move"])[:10]
    log("Most volatile reporters: " + ", ".join(f"{s} ±{v['move']}% ({v['date']})" for s, v in top))
    save("earnings_moves.json", {"date": today.isoformat(), "updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": items})


def main():
    today = dt.datetime.now(CT).date()
    earn = load("earnings.json", {})
    cats = load("catalysts.json", {})
    soon = (today + dt.timedelta(days=21)).isoformat()
    syms = {r[0] for r in earn.get("rows", []) if (r[1] or "") <= soon}
    syms |= {t for c in cats.get("catalysts", []) for t in str(c.get("ticker", "")).replace(" ", "").split(",") if t.isalpha()}
    profiles = update_profiles(sorted(syms), today)
    update_moves(today, profiles)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"[error] {type(e).__name__}: {e}")
        sys.exit(1)
