"""Catalyst track record -> track_record.json: how biotech stocks ($3+) moved on FDA approvals, rejections and trial results
over the last year. Events come from company 8-K / 6-K releases; only releases that lead with the news count. The move is
the bigger of the filing day and the next day, because a release can come out before the open or after the close."""
import datetime as dt
import re
import statistics
import time
import urllib.parse

import requests

from build_guidance import BIO_SIC, doc_text
from build_tools_common import MIN_PRICE, SEC_UA, chart, company_of, load, log, now_ct, save, stamp, ticker_of

LOOKBACK = 365
BUDGET_S = 240
CACHE_V = 1
QUERIES = {
    "approval": ["FDA approval of", "FDA approved", "FDA has approved"],
    "rejection": ["complete response letter"],
    "trial_win": ["met its primary endpoint", "met the primary endpoint", "achieved its primary endpoint", "achieved the primary endpoint"],
    "trial_miss": ["did not meet its primary endpoint", "did not meet the primary endpoint", "failed to meet its primary endpoint", "failed to meet the primary endpoint"],
}
HEAD = {
    "approval": re.compile(r"\b(FDA|Food and Drug Administration)\b.{0,80}\bapprov(ed|al|es)\b|\bapprov(ed|al|es)\b.{0,80}\b(FDA|Food and Drug Administration)\b", re.I),
    "rejection": re.compile(r"complete response letter", re.I),
    "trial_win": re.compile(r"\b(met|achieved|meets|achieves)\b.{0,40}\bprimary endpoint|positive\b.{0,60}\b(topline|top-line|results|data)\b", re.I),
    "trial_miss": re.compile(r"\b(did not meet|failed to meet|does not meet|missed)\b.{0,40}\bprimary endpoint", re.I),
}
NOT_NEWS = re.compile(r"\b(financial results|quarter|year[- ]end|corporate update|business update|investor presentation|conference)\b", re.I)
LABEL = {"approval": "FDA approval", "rejection": "FDA rejection (CRL)", "trial_win": "Trial win", "trial_miss": "Trial miss"}


def search(q, start, end, page):
    params = {"q": f'"{q}"', "dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat(), "forms": "8-K,6-K", "from": page * 100}
    r = requests.get("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params), headers=SEC_UA, timeout=30)
    r.raise_for_status()
    return r.json().get("hits", {}).get("hits") or []


def main():
    today = now_ct().date()
    cache = load("track_cache.json")
    if cache.get("_v") != CACHE_V:
        cache = {"_v": CACHE_V}
    start = today - dt.timedelta(days=LOOKBACK)
    hits = {}
    for cat, qs in QUERIES.items():
        for q in qs:
            for page in range(10):
                try:
                    rows = search(q, start, today, page)
                except Exception as e:  # noqa: BLE001
                    log("search failed:", q, e)
                    break
                for x in rows:
                    src = x.get("_source") or {}
                    names = src.get("display_names") or [""]
                    t = ticker_of(names[0])
                    if t and set(src.get("sics") or []) & BIO_SIC and re.search(r"\.html?$", x["_id"], re.I):
                        hits.setdefault(x["_id"], {"t": t, "name": company_of(names[0]), "date": src.get("file_date") or "", "cik": (src.get("ciks") or ["0"])[0], "cats": set()})["cats"].add(cat)
                if len(rows) < 100:
                    break
                time.sleep(0.12)
    log(f"Track record: {len(hits)} releases to look at")
    t0, charts = time.time(), {}
    order = sorted(hits.items(), key=lambda kv: kv[1]["date"], reverse=True)
    for _id, h in order:
        if _id in cache:
            continue
        if time.time() - t0 > BUDGET_S:
            log("time's up, the rest next run")
            break
        acc, fname = _id.split(":", 1)
        url = f"https://www.sec.gov/Archives/edgar/data/{int(h['cik'])}/{acc.replace('-', '')}/{fname}"
        try:
            text = doc_text(url)[:900]
        except Exception as e:  # noqa: BLE001
            log("couldn't read:", url, e)
            continue
        time.sleep(0.12)
        # the opening of the release has to be the news itself; a miss outranks a win when both phrases appear
        cat = next((c for c in ("trial_miss", "rejection", "approval", "trial_win") if c in h["cats"] and HEAD[c].search(text)), None)
        if not cat or NOT_NEWS.search(text[:260]):
            cache[_id] = None
            continue
        if h["t"] not in charts:
            charts[h["t"]] = chart(h["t"], "2y")
            time.sleep(0.1)
        c = charts[h["t"]]
        if c is None:
            continue   # Yahoo didn't answer: try again next run
        bars = c.get("bars") or []
        days = [b[0] for b in bars]
        i = next((k for k, d in enumerate(days) if d >= h["date"]), None)
        if i is None or i < 1:
            cache[_id] = None
            continue
        before = bars[i - 1][2]
        same = (bars[i][2] / before - 1) * 100
        nxt = (bars[i + 1][2] / bars[i][2] - 1) * 100 if i + 1 < len(bars) else None
        if nxt is None:
            continue   # too new: wait for the next day's close
        move = nxt if abs(nxt) > abs(same) else same
        cache[_id] = {"t": h["t"], "name": h["name"], "date": h["date"], "cat": cat, "move": round(move, 1), "before": round(before, 2), "link": url}
    save("track_cache.json", cache)
    events, seen = [], set()
    for _id, v in sorted(((k, v) for k, v in cache.items() if isinstance(v, dict)), key=lambda kv: kv[1]["date"], reverse=True):
        key = (v["t"], v["cat"], v["date"][:7])   # one per stock, kind and month: companies repeat the news in several filings
        if key in seen or v["before"] < MIN_PRICE or v["date"] < start.isoformat():
            continue
        seen.add(key)
        events.append(v)
    summary = {}
    for cat, label in LABEL.items():
        m = [e["move"] for e in events if e["cat"] == cat]
        if m:
            summary[cat] = {"label": label, "n": len(m), "avg": round(statistics.mean(m), 1), "median": round(statistics.median(m), 1),
                            "up": round(sum(x > 0 for x in m) / len(m) * 100), "best": max(m), "worst": min(m)}
    pending = sum(1 for k, _ in order if k not in cache)
    save("track_record.json", {"updated": stamp(), "from": start.isoformat(), "pending": pending, "summary": summary, "events": events[:200]})
    log(f"Track record: {len(events)} events; {pending} releases still to read")


if __name__ == "__main__":
    main()
