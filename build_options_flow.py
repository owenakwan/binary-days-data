"""Unusual options -> options_flow.json for stocks with 2x ETFs and catalyst names ($3+), using the same rules as the 1pm
email. Free option chains show totals and the last trade, not each trade's buyer or seller, so "bought" is a guess."""
import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

from build_tools_common import MIN_PRICE, api, load, log, now_ct, save, stamp, upcoming_catalysts

BUDGET_S = 240
MAX_AGE_MIN = 55
THREADS = 2


def chain(t, date=""):
    return api("/api/options", s=t, **({"date": date} if date else {}))


def flow(t):
    first = chain(t)
    if not first or first.get("error") or not first.get("expirations"):
        return t, None
    exps = first["expirations"][:2]
    chains = [first] + [chain(t, e) for e in exps[1:]]
    px = first.get("price") or 0
    if px < MIN_PRICE:
        return t, {"skip": True}
    cv = pv = cp = pp = 0.0
    big = []
    for c in chains:
        ex = dt.datetime.fromtimestamp(c["date"], dt.timezone.utc).date().isoformat() if isinstance(c.get("date"), (int, float)) else ""
        for side, rows in (("C", c.get("calls") or []), ("P", c.get("puts") or [])):
            for o in rows:
                v, last = o.get("vol") or 0, o.get("last") or 0
                prem = v * last * 100
                if side == "C":
                    cv += v
                    cp += prem
                else:
                    pv += v
                    pp += prem
                oi = o.get("oi") or 0
                if v >= 500 and prem >= 100_000 and v > max(oi, 1) * 1.2:
                    bid, ask = o.get("bid") or 0, o.get("ask") or 0
                    fill = "bought" if ask and last >= ask * 0.99 else "sold" if bid and last <= bid * 1.01 else ""
                    big.append({"t": t, "side": side, "strike": o["strike"], "exp": ex, "vol": int(v), "oi": int(oi), "prem": round(prem), "fill": fill,
                                "otm": (o["strike"] > px) if side == "C" else (o["strike"] < px)})
    if cv + pv == 0:
        return t, {"skip": True}
    ratio, pc = (cp + 1) / (pp + 1), (pv / cv if cv else 9.9)
    lean = ("bullish" if ratio >= 1.8 and pc <= 0.7 else "bearish" if ratio <= 0.55 and pc >= 1.2 else
            "slightly bullish" if ratio >= 1.25 and pc <= 1.0 else "slightly bearish" if ratio <= 0.8 and pc >= 0.8 else
            "mixed" if (ratio >= 1.25 and pc > 1.0) or (ratio <= 0.8 and pc < 0.8) else "neutral")
    big.sort(key=lambda b: -b["prem"])
    return t, {"t": t, "price": px, "cv": int(cv), "pv": int(pv), "cp": round(cp), "pp": round(pp), "pc": round(pc, 2), "lean": lean, "big": big[:5], "at": time.time()}


def main():
    prev = load("options_flow.json")
    now = now_ct()
    open_now = now.weekday() < 5 and (8, 30) <= (now.hour, now.minute) <= (15, 30)
    if prev.get("items") and (not open_now or time.time() - (prev.get("at") or 0) < MAX_AGE_MIN * 60):
        log("options flow: market closed or list is fresh enough")
        return
    lev = load("leveraged.json").get("items") or {}
    old = {x["t"]: x for x in prev.get("items") or []}
    order = sorted(set(lev) | set(upcoming_catalysts()), key=lambda t: (old.get(t) or {}).get("at", 0))   # oldest first, so every stock gets its turn
    t0, done, failed = time.time(), 0, 0
    with ThreadPoolExecutor(THREADS) as ex:
        for i in range(0, len(order), THREADS * 4):
            if time.time() - t0 > BUDGET_S:
                break
            for t, f in ex.map(flow, order[i:i + THREADS * 4]):
                if f is None:
                    failed += 1
                elif f.get("skip"):
                    old.pop(t, None)
                    done += 1
                else:
                    old[t] = f
                    done += 1
    cutoff = time.time() - 36 * 3600
    items = [x for x in old.values() if x.get("at", 0) >= cutoff]
    items.sort(key=lambda x: -(x["cp"] + x["pp"]))
    save("options_flow.json", {"updated": stamp(), "at": time.time(), "scanned": done, "failed": failed, "total": len(order), "items": items})
    log(f"Options flow: {done} of {len(order)} stocks scanned, {failed} lookups failed")


if __name__ == "__main__":
    main()
