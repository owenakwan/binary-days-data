"""
IPO lock-up expirations coming up, with the odds of a big move based on what similar stocks did in the past.

When a company goes public, insiders and early investors usually agree not to sell for 180 days (the "lock-up").
When it ends, a lot of shares can hit the market at once and the stock often drops around that date, especially when
it trades far above the IPO price.

  1. Upcoming: IPOs priced 132-225 days ago (Finnhub free IPO calendar); lock-up end estimated as IPO date + 180 days.
  2. History: every non-SPAC IPO whose lock-up ended in the past ~2 years. For each, what the stock did from
     10 trading days before the unlock to 5 trading days after, plus what it looked like going in (gain since IPO,
     volatility, 20-day trend, RSI). Kept in out/lockup_history.json and extended each day.
  3. Odds: for each upcoming lock-up, the 40 most similar past cases -> how often they fell 10%+, fell 20%+, rose 10%+.

Writes out/lockups.json. Runs once a day. Dates are estimates: some lock-ups are 90 or 365 days or end early on a
price trigger (only the prospectus says). History is a guide, not a forecast.
"""
import datetime as dt
import json
import math
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests

OUT_DIR = os.environ.get("OUT_DIR", "out")
OUT = os.path.join(OUT_DIR, "lockups.json")
HIST = os.path.join(OUT_DIR, "lockup_history.json")
CT = ZoneInfo("America/Chicago")
KEY = os.environ.get("FINNHUB_KEY", "").strip()
LOCKUP_DAYS = 180
PRE, POST = 10, 5          # trading days before / after the unlock that make up the "lock-up window"
HISTORY_DAYS = 730         # how far back to study past lock-ups
K = 40                     # similar past cases per upcoming lock-up
SPAC = re.compile(r"acquisition|merger corp|blank check|\bspac\b|capital corp\b", re.I)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}


def log(*a): print(*a, file=sys.stderr)


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
            log("finnhub failed:", path, e)
            time.sleep(2)
    return None


def ipo_price(v):
    try:
        return float(str(v).split("-")[-1]) if v not in (None, "") else None
    except ValueError:
        return None


def ipos(start, end):
    """Priced, non-SPAC IPOs between two dates, asked for a month at a time (the API caps each answer)."""
    rows, a = [], start
    while a <= end:
        b = min(end, a + dt.timedelta(days=30))
        data = finnhub("calendar/ipo", **{"from": a.isoformat(), "to": b.isoformat()}) or {}
        rows += data.get("ipoCalendar") or []
        a = b + dt.timedelta(days=1)
        time.sleep(1.1)
    out, seen = [], set()
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or sym in seen or (r.get("status") or "").lower() not in ("priced", ""):
            continue
        # SPAC shells ($10 units, "Acquisition Corp") hold cash until a merger; their lock-ups don't move anything
        if SPAC.search(r.get("name") or "") or (sym.endswith(("U", "R", "W")) and ipo_price(r.get("price")) == 10):
            continue
        try:
            d = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        seen.add(sym)
        out.append({"symbol": sym, "name": r.get("name") or "", "exchange": r.get("exchange") or "", "ipo_date": d,
                    "ipo_price": ipo_price(r.get("price")), "ipo_shares": r.get("numberOfShares"), "ipo_value": r.get("totalSharesValue")})
    return out


def daily(sym, start, end):
    """[(date, close)] from Yahoo, oldest first."""
    p1 = int(dt.datetime.combine(start, dt.time()).timestamp())
    p2 = int(dt.datetime.combine(end, dt.time()).timestamp())
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"period1": p1, "period2": p2, "interval": "1d"},
                         headers=UA, timeout=20)
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        return [(dt.datetime.utcfromtimestamp(t).date(), c) for t, c in zip(res["timestamp"], closes) if c]
    except Exception:  # noqa: BLE001
        return []


def features(closes, ipo_px):
    """What the stock looks like at the end of `closes`: gain since IPO, 20-day volatility and trend, RSI(14)."""
    c = [x for _, x in closes]
    if len(c) < 22:
        return None
    last = c[-1]
    rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 20, len(c))]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252)
    gains = [max(c[i] - c[i - 1], 0) for i in range(len(c) - 14, len(c))]
    losses = [max(c[i - 1] - c[i], 0) for i in range(len(c) - 14, len(c))]
    rsi = 100 if not sum(losses) else 100 - 100 / (1 + sum(gains) / sum(losses))
    return {"price": round(last, 4), "gain": round(last / ipo_px - 1, 4) if ipo_px else None, "vol20": round(vol, 4),
            "mom20": round(last / c[-21] - 1, 4), "rsi": round(rsi, 1)}


def study(item, today):
    """What happened around one past lock-up: return from 10 trading days before to 5 after, plus the setup going in."""
    unlock = item["ipo_date"] + dt.timedelta(days=LOCKUP_DAYS)
    closes = daily(item["symbol"], item["ipo_date"], unlock + dt.timedelta(days=15))
    t = next((i for i, (d, _) in enumerate(closes) if d >= unlock), None)
    if t is None or t - PRE - 21 < 0 or t + POST >= len(closes):
        return None
    pre = closes[: t - PRE + 1]
    f = features(pre, item["ipo_price"])
    if not f:
        return None
    start, end = closes[t - PRE][1], closes[t + POST][1]
    low = min(x for _, x in closes[t - PRE: t + POST + 1])
    return dict(f, symbol=item["symbol"], unlock=unlock.isoformat(), ret=round(end / start - 1, 4), dd=round(low / start - 1, 4))


def vec(f, use_gain):
    v = [f["vol20"], f["mom20"]]
    if use_gain:
        v.insert(0, math.log1p(max(f["gain"], -0.95)))
    return v


def odds(f, history):
    """Of the K past lock-ups most like this one, how many fell 10%+ / 20%+ or rose 10%+ over the window."""
    use_gain = f.get("gain") is not None
    rows = [h for h in history if not use_gain or h.get("gain") is not None]
    if len(rows) < 20:
        return None
    vs = [vec(h, use_gain) for h in rows]
    sd = [max(1e-6, math.sqrt(sum((v[i] - sum(x[i] for x in vs) / len(vs)) ** 2 for v in vs) / len(vs))) for i in range(len(vs[0]))]
    me = vec(f, use_gain)
    near = sorted(rows, key=lambda h: sum(((a - b) / s) ** 2 for a, b, s in zip(vec(h, use_gain), me, sd)))[:K]
    n = len(near)
    rets = sorted(h["ret"] for h in near)
    return {"n": n, "drop10": round(sum(h["ret"] <= -0.10 for h in near) / n, 3), "drop20": round(sum(h["ret"] <= -0.20 for h in near) / n, 3),
            "rise10": round(sum(h["ret"] >= 0.10 for h in near) / n, 3), "median": round(rets[n // 2], 4),
            "examples": [f"{h['symbol']} {h['ret'] * 100:+.0f}%" for h in sorted(near[:8], key=lambda h: h["ret"])[:4]]}


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
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- history: past lock-ups, studied once each and remembered
    try:
        hist = json.load(open(HIST, encoding="utf-8"))
    except (OSError, ValueError):
        hist = {"done": {}}
    done = hist["done"]
    past = ipos(today - dt.timedelta(days=HISTORY_DAYS + LOCKUP_DAYS), today - dt.timedelta(days=LOCKUP_DAYS + 12))
    new = [p for p in past if p["symbol"] + "|" + p["ipo_date"].isoformat() not in done]
    t0 = time.time()
    for p in new:
        if time.time() - t0 > 600:   # spread a big first run over several days
            break
        done[p["symbol"] + "|" + p["ipo_date"].isoformat()] = study(p, today)
        time.sleep(0.3)
    hist = {"updated": today.isoformat(), "done": done}
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(hist, f, separators=(",", ":"))
    history = [h for h in done.values() if h]
    base = None
    if history:
        n = len(history)
        base = {"n": n, "drop10": round(sum(h["ret"] <= -0.10 for h in history) / n, 3), "drop20": round(sum(h["ret"] <= -0.20 for h in history) / n, 3),
                "rise10": round(sum(h["ret"] >= 0.10 for h in history) / n, 3), "median": round(sorted(h["ret"] for h in history)[n // 2], 4)}
    print(f"Lock-up history: {len(history)} past cases studied ({len(new)} new IPOs checked this run). Base rates: {base}")

    # ---- upcoming lock-ups, each with today's setup and the odds from similar past cases
    upcoming = ipos(today - dt.timedelta(days=LOCKUP_DAYS + 45), today - dt.timedelta(days=LOCKUP_DAYS - 48))
    out = []
    for p in upcoming:
        unlock = p["ipo_date"] + dt.timedelta(days=LOCKUP_DAYS)
        if not (today - dt.timedelta(days=3) <= unlock <= today + dt.timedelta(days=45)):
            continue
        row = {"symbol": p["symbol"], "name": p["name"], "exchange": p["exchange"], "ipo_date": p["ipo_date"].isoformat(), "ipo_price": p["ipo_price"],
               "ipo_shares": p["ipo_shares"], "ipo_value": p["ipo_value"], "unlock": unlock.isoformat(), "days_left": (unlock - today).days, "estimated": True}
        closes = daily(p["symbol"], p["ipo_date"], today + dt.timedelta(days=1))
        f = features(closes, p["ipo_price"]) if closes else None
        if f:
            row["now"] = f
            row["odds"] = odds(f, history)
        out.append(row)
        time.sleep(0.3)
    out.sort(key=lambda x: (x["unlock"], x["symbol"]))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"day": today.isoformat(), "updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "lockup_days": LOCKUP_DAYS,
                   "window": f"{PRE} trading days before to {POST} after the unlock", "base": base, "items": out}, f, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(out)} upcoming lock-ups")
    for x in out[:30]:
        o = x.get("odds") or {}
        print(f"   {x['unlock']} {x['symbol']:6} gain {((x.get('now') or {}).get('gain') or 0) * 100:+.0f}%  "
              f"drop10 {o.get('drop10')} drop20 {o.get('drop20')} rise10 {o.get('rise10')} n={o.get('n')}")


if __name__ == "__main__":
    main()
