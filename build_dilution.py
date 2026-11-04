"""Dilution tracker -> dilution.json: biotech and catalyst names ($3+) that filed to sell shares in the last 30 days, and
companies with under 12 months of cash. Forms come from SEC full-text search, cash and burn from SEC XBRL filings."""
import datetime as dt
import time
import urllib.parse

import requests

from build_guidance import BIO_SIC
from build_tools_common import MIN_PRICE, SEC_UA, chart, company_of, load, log, now_ct, save, stamp, ticker_of, upcoming_catalysts

DAYS = 30
FORMS = "S-1,S-3,S-3ASR,424B5,424B4"
BUDGET_S = 240
CACHE_DAYS = 7
CASH = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
INVEST = ("ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent")
KIND = {"S-1": "Registering new shares", "S-3": "Shelf: can sell later", "S-3ASR": "Shelf: can sell later", "424B5": "Selling shares now", "424B4": "Selling shares now"}


def search(start, end, page, q=""):
    params = {"dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat(), "forms": FORMS, "from": page * 100}
    if q:
        params["q"] = f'"{q}"'
    r = requests.get("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params), headers=SEC_UA, timeout=30)
    r.raise_for_status()
    return r.json().get("hits", {}).get("hits") or []


def filings(today, watch):
    start = today - dt.timedelta(days=DAYS)
    out = {}
    for page in range(10):
        rows = search(start, today, page)
        for x in rows:
            src = x.get("_source") or {}
            names = src.get("display_names") or [""]
            t = ticker_of(names[0])
            if not t or not (set(src.get("sics") or []) & BIO_SIC or t in watch):
                continue
            acc, _, fname = x["_id"].partition(":")
            cik = (src.get("ciks") or ["0"])[0]
            out[x["_id"]] = {"t": t, "name": company_of(names[0]), "form": src.get("form") or "", "date": src.get("file_date") or "", "cik": cik,
                             "link": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{fname}", "atm": False}
        if len(rows) < 100:
            break
        time.sleep(0.12)
    for page in range(5):
        rows = search(start, today, page, "at the market")
        for x in rows:
            if x["_id"] in out:
                out[x["_id"]]["atm"] = True
        if len(rows) < 100:
            break
        time.sleep(0.12)
    return list(out.values())


def latest(facts, tags):
    """The newest value in a 10-Q or 10-K across these XBRL tags: (end date, value)."""
    best = None
    for tag in tags:
        for x in ((facts.get(tag) or {}).get("units") or {}).get("USD") or []:
            if x.get("form") in ("10-Q", "10-K") and "val" in x and (best is None or x["end"] > best[0]):
                best = (x["end"], x["val"])
    return best


def runway(cik):
    r = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json", headers=SEC_UA, timeout=40)
    if r.status_code == 404:
        return {}   # foreign filers mostly don't report in this format
    r.raise_for_status()
    facts = (r.json().get("facts") or {}).get("us-gaap") or {}
    cash = latest(facts, CASH)
    if not cash:
        return {}
    end = cash[0]
    invest = latest(facts, INVEST)
    total = cash[1] + (invest[1] if invest and invest[0] == end else 0)
    # Cash flow is reported year to date, so use the longest period ending on the balance sheet date.
    flows = [x for x in ((facts.get("NetCashProvidedByUsedInOperatingActivities") or {}).get("units") or {}).get("USD") or []
             if x.get("end") == end and x.get("start") and x.get("form") in ("10-Q", "10-K")]
    out = {"cash": total, "end": end}
    if flows:
        f = min(flows, key=lambda x: x["start"])
        months = (dt.date.fromisoformat(end) - dt.date.fromisoformat(f["start"])).days / 30.44
        if 2 <= months <= 13 and f["val"] < 0:
            out["burn"] = -f["val"] / months
            out["runway"] = round(total / out["burn"], 1)
    return out


def main():
    today = now_ct().date()
    cat = upcoming_catalysts(120)
    rows = filings(today, set(cat))
    log(f"Dilution: {len(rows)} share-sale filings in the last {DAYS} days")
    tk = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=30).json()
    ciks = {v["ticker"]: str(v["cik_str"]) for v in tk.values()}
    cache = load("dilution_cache.json")
    fresh = time.time() - CACHE_DAYS * 86400
    names = list(dict.fromkeys(list(cat) + [r["t"] for r in rows]))
    t0 = time.time()
    for t in names:
        if t in ciks and cache.get(t, {}).get("at", 0) < fresh and time.time() - t0 < BUDGET_S:
            try:
                cache[t] = {**runway(ciks[t]), "at": time.time()}
            except Exception as e:  # noqa: BLE001
                log("no cash data:", t, e)
            time.sleep(0.12)
    save("dilution_cache.json", cache)
    px = {}
    for t in names:
        c = chart(t, "5d")
        p = (c or {}).get("meta", {}).get("regularMarketPrice")
        if p:
            px[t] = p
        time.sleep(0.1)
    stale = (today - dt.timedelta(days=200)).isoformat()

    def cash_of(t):
        c = cache.get(t) or {}
        if c.get("end", "") < stale:
            return {}
        return {"cash_m": round(c["cash"] / 1e6, 1) if c.get("cash") is not None else None,
                "burn_m": round(c["burn"] / 1e6, 1) if c.get("burn") else None, "runway": c.get("runway"), "as_of": c.get("end")}

    def ok(t):
        return px.get(t, 0) >= MIN_PRICE

    items = [{**{k: r[k] for k in ("t", "name", "form", "date", "link", "atm")}, "kind": KIND.get(r["form"], r["form"]), "price": round(px[r["t"]], 2), **cash_of(r["t"])}
             for r in rows if ok(r["t"])]
    items.sort(key=lambda x: (x["date"], x["t"]), reverse=True)
    seen = set()
    items = [x for x in items if (x["t"], x["kind"]) not in seen and not seen.add((x["t"], x["kind"]))]   # newest of each kind per stock
    low = [{"t": t, "price": round(px[t], 2), "event": cat[t][0] if t in cat else None, **cash_of(t)} for t in names if ok(t) and (cash_of(t).get("runway") or 99) < 12]
    low.sort(key=lambda x: x["runway"])
    save("dilution.json", {"updated": stamp(), "days": DAYS, "items": items[:150], "low_cash": low[:80]})
    log(f"Dilution: {len(items)} filings at $3+, {len(low)} companies under 12 months of cash")


if __name__ == "__main__":
    main()
