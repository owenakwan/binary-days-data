"""
IPO / new-listing lock-up expirations coming up, with the odds of a big ONE-DAY move based on what similar stocks did.

When a company goes public, insiders and early investors usually agree not to sell for 180 days (the "lock-up").
When it ends, a flood of sellable shares can hit a thin float and the stock can crash in a single session, often a day
or two before or after the date (XNDU -22% on Sep 22, SWMR -19%/-28% on Sep 14-15, HQ -32% on Sep 14).

  1. Listings: Finnhub's IPO calendar (traditional IPOs) plus the first trading day of every recently registered
     company in the SEC's ticker list, which also catches SPAC mergers and direct listings (XNDU, SWMR, HQ).
     First-trade dates never change, so they're scanned once and remembered (out/listing_dates.json).
  2. History: every past lock-up in ~2 years. For each: the move on the unlock day itself, and the worst and best
     single day from 2 trading days before to 2 after, plus the setup going in (gain since listing, volatility,
     20-day trend, RSI). Remembered in out/lockup_history.json.
  3. Odds: for each upcoming lock-up, the 40 most similar past cases -> how often they had a 10%+ or 20%+ one-day
     drop around the date, or a 10%+ one-day jump.

Writes out/lockups.json once a day. Dates are estimates (listing date + 180 days); some lock-ups are 90 or 365 days,
or end early on a price trigger. History is a guide, not a forecast.
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
LISTINGS = os.path.join(OUT_DIR, "listing_dates.json")
CT = ZoneInfo("America/Chicago")
KEY = os.environ.get("FINNHUB_KEY", "").strip()
SEC_UA = {"User-Agent": f"binary-days data builder {os.environ.get('SEC_CONTACT', '').encode('ascii', 'ignore').decode().strip() or 'contact via github.com/owenakwan'}"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
LOCKUP_DAYS = 180
AROUND = 2                 # trading days before / after the unlock that count as "around the date"
HISTORY_DAYS = 730         # how far back to study past lock-ups
K = 40                     # similar past cases per upcoming lock-up
NEW_CIK = 1_850_000        # SEC ids at or above this belong to companies registered in roughly the last 5 years
SPAC = re.compile(r"acquisition|merger corp|blank check|\bspac\b|capital corp\b", re.I)
HIST_VERSION = 2


def log(*a): print(*a, file=sys.stderr)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


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
    """Priced, non-SPAC IPOs between two dates (Finnhub), a month at a time."""
    rows, a = [], start
    while a <= end:
        b = min(end, a + dt.timedelta(days=30))
        rows += (finnhub("calendar/ipo", **{"from": a.isoformat(), "to": b.isoformat()}) or {}).get("ipoCalendar") or []
        a = b + dt.timedelta(days=1)
        time.sleep(1.1)
    out = {}
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or sym in out or (r.get("status") or "").lower() not in ("priced", ""):
            continue
        if SPAC.search(r.get("name") or "") or (sym.endswith(("U", "R", "W")) and ipo_price(r.get("price")) == 10):
            continue
        try:
            d = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        out[sym] = {"symbol": sym, "name": r.get("name") or "", "exchange": r.get("exchange") or "", "listed": d, "kind": "IPO",
                    "ipo_price": ipo_price(r.get("price")), "ipo_shares": r.get("numberOfShares")}
    return out


def ohlc(sym, start, end=None):
    """[(date, open, low, close)] from Yahoo, oldest first."""
    params = {"interval": "1d", "period1": int(dt.datetime.combine(start, dt.time()).timestamp())}
    params["period2"] = int(dt.datetime.combine(end or dt.date.today() + dt.timedelta(days=1), dt.time()).timestamp())
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params=params, headers=UA, timeout=20)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        return [(dt.datetime.utcfromtimestamp(t).date(), o, lo, c) for t, o, lo, c in zip(res["timestamp"], q["open"], q["low"], q["close"]) if c and lo]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------- listing dates (SPAC mergers, direct listings, IPOs)
def scan_listings(today, budget_s):
    """First trading day for recently registered companies (SEC ids >= NEW_CIK). A few hundred per run until done."""
    store = load(LISTINGS, {"dates": {}, "names": {}, "checked": {}})
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=30)
        rows = list(r.json().values())
    except Exception as e:  # noqa: BLE001
        log("SEC ticker list unavailable:", e)
        return store
    want = []
    for v in rows:
        t = str(v["ticker"]).upper()
        # skip warrants/units/rights and 5-letter OTC foreign tickers (…Y ADRs, …F ordinaries)
        if v["cik_str"] < NEW_CIK or "-" in t or (len(t) == 5 and t[-1] in "WURYF"):
            continue
        store["names"][t] = v["title"]
        # new names: check now; ones with no listing date yet (not trading): re-check weekly
        last = store["checked"].get(t)
        if last is None or (store["dates"].get(t) is None and last < (today - dt.timedelta(days=7)).isoformat()):
            want.append(t)
    t0, n = time.time(), 0
    for t in want:
        if time.time() - t0 > budget_s:
            break
        try:
            r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}", params={"range": "5d", "interval": "1d"}, headers=UA, timeout=15)
            meta = r.json()["chart"]["result"][0]["meta"]
            ft = meta.get("firstTradeDate")
            store["dates"][t] = dt.datetime.utcfromtimestamp(ft).date().isoformat() if ft else None
        except Exception:  # noqa: BLE001
            store["dates"][t] = None
        store["checked"][t] = today.isoformat()
        n += 1
        time.sleep(0.12)
    save(LISTINGS, store)
    known = sum(1 for v in store["dates"].values() if v)
    print(f"Listing dates: {known} known, {n} checked this run, {max(0, len(want) - n)} still to check")
    return store


def listings(store, ipo_map, start, end):
    """Every listing between two dates: IPOs from Finnhub plus SPAC mergers / direct listings from the scan."""
    out = {s: v for s, v in ipo_map.items() if start <= v["listed"] <= end}
    for t, d in store["dates"].items():
        if not d or t in out or (len(t) == 5 and t[-1] in "WURYF"):
            continue
        try:
            day = dt.date.fromisoformat(d)
        except ValueError:
            continue
        name = store["names"].get(t, "")
        if start <= day <= end and not SPAC.search(name):
            out[t] = {"symbol": t, "name": name, "exchange": "", "listed": day, "kind": "SPAC merger / direct listing", "ipo_price": None, "ipo_shares": None}
    return out


# ---------------------------------------------------------------- study
def features(rows, base_px):
    """Setup at the end of `rows`: gain since listing, 20-day volatility and trend, RSI(14)."""
    c = [x[3] for x in rows]
    if len(c) < 22:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 20, len(c))]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252)
    gains = [max(c[i] - c[i - 1], 0) for i in range(len(c) - 14, len(c))]
    losses = [max(c[i - 1] - c[i], 0) for i in range(len(c) - 14, len(c))]
    rsi = 100 if not sum(losses) else 100 - 100 / (1 + sum(gains) / sum(losses))
    return {"price": round(c[-1], 4), "gain": round(c[-1] / base_px - 1, 4) if base_px else None, "vol20": round(vol, 4),
            "mom20": round(c[-1] / c[-21] - 1, 4), "rsi": round(rsi, 1)}


def study(item):
    """The unlock-day move, and the worst / best single day from 2 trading days before to 2 after."""
    unlock = item["listed"] + dt.timedelta(days=LOCKUP_DAYS)
    rows = ohlc(item["symbol"], item["listed"], unlock + dt.timedelta(days=10))
    t = next((i for i, r in enumerate(rows) if r[0] >= unlock), None)
    if t is None or t - AROUND - 22 < 0 or t + AROUND >= len(rows):
        return None
    base_px = item.get("ipo_price") or rows[0][3]
    f = features(rows[: t - AROUND], base_px)
    if not f:
        return None
    day = lambda i: rows[i][3] / rows[i - 1][3] - 1  # noqa: E731
    span = range(t - AROUND, t + AROUND + 1)
    return dict(f, symbol=item["symbol"], unlock=unlock.isoformat(), kind=item["kind"], day0=round(day(t), 4),
                worst=round(min(day(i) for i in span), 4), best=round(max(day(i) for i in span), 4),
                worst_low=round(min(rows[i][2] / rows[i - 1][3] - 1 for i in span), 4))


def rates(cases):
    n = len(cases)
    if not n:
        return None
    return {"n": n, "drop10": round(sum(h["worst"] <= -0.10 for h in cases) / n, 3), "drop20": round(sum(h["worst"] <= -0.20 for h in cases) / n, 3),
            "rise10": round(sum(h["best"] >= 0.10 for h in cases) / n, 3), "day0_drop10": round(sum(h["day0"] <= -0.10 for h in cases) / n, 3),
            "day0_median": round(sorted(h["day0"] for h in cases)[n // 2], 4)}


def vec(f, use_gain):
    v = [f["vol20"], f["mom20"]]
    if use_gain:
        v.insert(0, math.log1p(max(f["gain"], -0.95)))
    return v


def odds(f, history):
    use_gain = f.get("gain") is not None
    rows = [h for h in history if not use_gain or h.get("gain") is not None]
    if len(rows) < 20:
        return None
    vs = [vec(h, use_gain) for h in rows]
    sd = [max(1e-6, math.sqrt(sum((v[i] - sum(x[i] for x in vs) / len(vs)) ** 2 for v in vs) / len(vs))) for i in range(len(vs[0]))]
    me = vec(f, use_gain)
    near = sorted(rows, key=lambda h: sum(((a - b) / s) ** 2 for a, b, s in zip(vec(h, use_gain), me, sd)))[:K]
    out = rates(near)
    out["examples"] = [f"{h['symbol']} {h['worst'] * 100:+.0f}%" for h in sorted(near[:10], key=lambda h: h["worst"])[:4]]
    return out


def main():
    today = dt.datetime.now(CT).date()
    force = "--force" in sys.argv
    # the listing scan runs every time until it has caught up (a few minutes a run), then it's nearly free
    store = scan_listings(today, 150)
    if not KEY:
        print("No FINNHUB_KEY; skipping lock-ups.")
        return
    prev = load(OUT, {})
    if not force and prev.get("day") == today.isoformat() and prev.get("v") == HIST_VERSION:
        print("Lock-ups already built today; keeping them.")
        return

    # ---- history: every past lock-up, studied once and remembered
    hist = load(HIST, {})
    if hist.get("v") != HIST_VERSION:
        hist = {"v": HIST_VERSION, "done": {}}
    done = hist["done"]
    past_start, past_end = today - dt.timedelta(days=HISTORY_DAYS + LOCKUP_DAYS), today - dt.timedelta(days=LOCKUP_DAYS + 8)
    past = listings(store, ipos(past_start, past_end), past_start, past_end)
    new = [p for p in past.values() if p["symbol"] + "|" + p["listed"].isoformat() not in done]
    t0 = time.time()
    for p in new:
        if time.time() - t0 > 480:   # a big first run is spread over several days
            break
        done[p["symbol"] + "|" + p["listed"].isoformat()] = study(p)
        time.sleep(0.25)
    save(HIST, {"v": HIST_VERSION, "updated": today.isoformat(), "done": done})
    history = [h for h in done.values() if h]
    base = rates(history)
    print(f"Lock-up history: {len(history)} past cases ({sum(1 for h in history if h['kind'] != 'IPO')} SPAC/direct listings), "
          f"{len(new)} new checked this run. Base rates: {base}")

    # ---- upcoming lock-ups with today's setup and the odds from similar past cases
    up_start, up_end = today - dt.timedelta(days=LOCKUP_DAYS + 3), today - dt.timedelta(days=LOCKUP_DAYS - 45)
    out = []
    for p in listings(store, ipos(up_start, up_end), up_start, up_end).values():
        unlock = p["listed"] + dt.timedelta(days=LOCKUP_DAYS)
        rows = ohlc(p["symbol"], p["listed"])
        if not rows:
            continue   # not trading (delisted, or a ticker that never listed)
        base_px = p.get("ipo_price") or rows[0][3]
        row = {"symbol": p["symbol"], "name": p["name"], "exchange": p["exchange"], "kind": p["kind"], "listed": p["listed"].isoformat(),
               "ipo_date": p["listed"].isoformat(), "ipo_price": p.get("ipo_price"), "first_close": round(rows[0][3], 4), "ipo_shares": p.get("ipo_shares"),
               "unlock": unlock.isoformat(), "days_left": (unlock - today).days, "estimated": True}
        f = features(rows, base_px)
        if f:
            row["now"] = f
            row["odds"] = odds(f, history)
        out.append(row)
        time.sleep(0.25)
    out.sort(key=lambda x: (x["unlock"], x["symbol"]))
    save(OUT, {"v": HIST_VERSION, "day": today.isoformat(), "updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "lockup_days": LOCKUP_DAYS,
               "measure": f"one-day moves from {AROUND} trading days before to {AROUND} after the unlock", "base": base, "items": out})
    print(f"Wrote {OUT}: {len(out)} upcoming lock-ups")
    for x in out[:40]:
        o = x.get("odds") or {}
        print(f"   {x['unlock']} {x['symbol']:6} {x['kind'][:5]} gain {((x.get('now') or {}).get('gain') or 0) * 100:+.0f}%  "
              f"1-day drop10 {o.get('drop10')} drop20 {o.get('drop20')} rise10 {o.get('rise10')}")


if __name__ == "__main__":
    main()
