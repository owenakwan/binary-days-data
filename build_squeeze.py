"""Squeeze screener -> squeeze.json: heavily shorted stocks ($3+) with a catalyst or earnings report coming up, and how
their volume compares with normal. Short interest is Yahoo's, which is reported about twice a month, so it can lag."""
import time
from concurrent.futures import ThreadPoolExecutor

from build_tools_common import MIN_PRICE, THREADS, api, chart, load, log, now_ct, save, stamp, upcoming_catalysts

MAX_AGE_H = 3
MIN_SHORT = 15.0   # % of float


def check(t):
    p = api("/api/profile", s=t)
    st = p.get("stats") or {}
    q = p.get("quote") or {}
    if not q.get("price"):
        return t, None
    c = chart(t)
    vols = [b[3] for b in (c or {}).get("bars", [])]
    ratio = None
    if len(vols) >= 30:
        base = sum(vols[-30:-5]) / 25
        ratio = round(sum(vols[-5:]) / 5 / base, 2) if base else None
    short = st.get("shortPct")
    if short is not None and short <= 1.5:   # Yahoo gives it as a fraction
        short = round(short * 100, 1)
    return t, {"t": t, "name": p.get("name") or t, "price": q["price"], "pct": q.get("pct"), "cap": st.get("marketCap"),
               "short": short, "days": st.get("shortRatio"), "vol": ratio}


def main():
    prev = load("squeeze.json")
    if time.time() - (prev.get("at") or 0) < MAX_AGE_H * 3600:
        log("squeeze list is fresh enough")
        return
    cat = upcoming_catalysts()
    today = now_ct().date().isoformat()
    earn = {}
    for r in load("earnings.json").get("rows") or []:
        if today <= r[1] and r[0] not in earn:
            earn[r[0]] = r[1]
    lev = load("leveraged.json").get("items") or {}
    uni = sorted(set(cat) | {t for t in lev if t in earn})
    with ThreadPoolExecutor(THREADS) as ex:
        res = dict(ex.map(check, uni))
    items, failed = [], 0
    for t, v in res.items():
        if v is None:
            failed += 1
            continue
        if (v["price"] or 0) < MIN_PRICE or (v["short"] or 0) < MIN_SHORT:
            continue
        if t in cat:
            v["event"], v["what"] = cat[t]
        if t in earn:
            v["earn"] = earn[t]
        # more shorts, more days to cover and rising volume all add pressure
        v["score"] = round(v["short"] * (1 + min(v["vol"] or 1, 3) / 3) + min(v["days"] or 0, 15), 1)
        items.append(v)
    items.sort(key=lambda x: -x["score"])
    save("squeeze.json", {"updated": stamp(), "at": time.time(), "checked": len(uni), "failed": failed, "min_short": MIN_SHORT, "items": items[:80]})
    log(f"Squeeze: {len(items)} of {len(uni)} stocks qualify, {failed} lookups failed")


if __name__ == "__main__":
    main()
