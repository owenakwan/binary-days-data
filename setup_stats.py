"""How much a binary catalyst can move the stock: market cap, the options-implied move to the first expiry after the
event, 30-day volatility, the typical daily move and the biggest one-day moves of the past year.

Cached in moves.json and refreshed every few hours (options move slowly; a run only has a couple of minutes for this).
"""
import datetime as dt
import json
import math
import os
import time

import requests

OUT_DIR = os.environ.get("OUT_DIR", "out")
CACHE = os.path.join(OUT_DIR, "moves.json")
API = "https://binary-days-api.binary-days-api.workers.dev"
MAX_AGE_H = 3
BUDGET_S = 150
UA = {"User-Agent": "Mozilla/5.0 (binary-days data builder)"}


def chart_stats(t):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}", params={"range": "1y", "interval": "1d"}, headers=UA, timeout=15)
    res = r.json()["chart"]["result"][0]
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
    if len(closes) < 40:
        return {}
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    last30 = [math.log(1 + x) for x in rets[-30:]]
    mean = sum(last30) / len(last30)
    hv = math.sqrt(sum((x - mean) ** 2 for x in last30) / (len(last30) - 1)) * math.sqrt(252) * 100
    return {"price": round(res["meta"].get("regularMarketPrice") or closes[-1], 2), "hv30": round(hv),
            "avg_move": round(sum(abs(x) for x in rets[-60:]) / len(rets[-60:]) * 100, 1),
            "max_up": round(max(rets) * 100), "max_down": round(min(rets) * 100)}


def implied(t, by, price):
    """ATM straddle / price for the first expiry on or after `by`."""
    o = requests.get(f"{API}/api/options", params={"s": t}, timeout=20).json()
    ex = next((e for e in o.get("expirations") or [] if e >= dt.datetime(by.year, by.month, by.day, tzinfo=dt.timezone.utc).timestamp()), None)
    if not ex:
        return None, None
    if ex != o.get("date"):
        o = requests.get(f"{API}/api/options", params={"s": t, "date": ex}, timeout=20).json()
    calls, puts = o.get("calls") or [], o.get("puts") or []
    px = o.get("price") or price
    ks = sorted({c["strike"] for c in calls} & {p["strike"] for p in puts}, key=lambda k: abs(k - px))
    if not ks or not px:
        return None, None
    v = lambda x: (x["bid"] + x["ask"]) / 2 if x.get("bid") and x.get("ask") else x.get("last") or 0   # noqa: E731
    c = next(x for x in calls if x["strike"] == ks[0])
    p = next(x for x in puts if x["strike"] == ks[0])
    m = (v(c) + v(p)) / px * 100
    return (round(m) if 0 < m < 300 else None), dt.datetime.fromtimestamp(ex, dt.timezone.utc).date().isoformat()


def update(events, prof):
    """{ticker: stats} for the binary events, refreshing the oldest first within the time budget."""
    try:
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    want = {}
    for e in events:   # the event each stock's options move is measured to: its soonest binary
        if e["ticker"] not in want or e["end"] < want[e["ticker"]]:
            want[e["ticker"]] = e["end"]
    now = time.time()
    stale = sorted((t for t in want if now - cache.get(t, {}).get("at", 0) > MAX_AGE_H * 3600 or cache.get(t, {}).get("by") != want[t]),
                   key=lambda t: cache.get(t, {}).get("at", 0))
    t0, done = time.time(), 0
    for t in stale:
        if time.time() - t0 > BUDGET_S:
            break
        try:
            s = chart_stats(t)
            if s:
                s["im"], s["im_exp"] = implied(t, dt.date.fromisoformat(want[t]), s["price"])
            cache[t] = {**s, "by": want[t], "at": now}
            done += 1
        except Exception as ex:  # noqa: BLE001
            cache[t] = {**cache.get(t, {}), "by": want[t], "at": now - (MAX_AGE_H - 1) * 3600, "err": str(ex)[:80]}   # retry in an hour
        time.sleep(0.2)
    for t in want:
        if t in cache:
            cache[t]["cap_m"] = (prof.get(t) or {}).get("c")
    cache = {t: v for t, v in cache.items() if t in want}
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    print(f"Move stats: {len(want)} binary stocks, refreshed {done} this run, {sum(t in cache and 'price' in cache[t] for t in want)} with data")
    return cache


def money(m):
    if not m:
        return ""
    return f"${m / 1000:.1f}B" if m >= 1000 else f"${m:.0f}M"


def line(s):
    """'MC $1.9B · options ±21% by Nov 20 · volatility 65% · usual day ±3.1% · biggest day +38% / −45%'"""
    if not s or "price" not in s:
        return ""
    bits = [f"${s['price']:.2f}"]
    if s.get("cap_m"):
        bits.append(f"MC {money(s['cap_m'])}")
    if s.get("im"):
        d = dt.date.fromisoformat(s["im_exp"])
        bits.append(f"options ±{s['im']}% by {d:%b} {d.day}")
    bits.append(f"volatility {s['hv30']}%")
    bits.append(f"usual day ±{s['avg_move']}%")
    bits.append(f"biggest day +{s['max_up']}% / {s['max_down']}%".replace("-", "−"))
    return " · ".join(bits)
