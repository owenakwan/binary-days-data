"""FDA decision dates (PDUFA) straight from company SEC filings -> pdufa.json.

The spreadsheet's FDA dates came from third-party calendars and some were wrong (MLYS was listed for Oct 23; its own
filings say December 22, 2026). Companies state the date in 8-Ks, 10-Qs and 6-Ks: "assigned a PDUFA target action
date of December 22, 2026". This reads those filings for every drug company, keeps the newest date per drug (so an
extension replaces the old date) and drops decisions that already happened. build_site_data.py and the emails use it
to correct the sheet and to add FDA dates the sheet doesn't have.
"""
import datetime as dt
import json
import os
import re
import time
import urllib.parse

import requests

from build_guidance import BIO_SIC, CT, MON_RE, MONTHS, UA, doc_text, log, program_names

OUT_DIR = os.environ.get("OUT_DIR", "out")
OUT = os.path.join(OUT_DIR, "pdufa.json")
CACHE = os.path.join(OUT_DIR, "pdufa_cache.json")
LOOKBACK_DAYS = 330        # a standard review is ~10 months, and 10-Qs repeat the date every quarter
BUDGET_S = 300             # time for reading new filings per run; the rest is picked up next run
CACHE_V = 1
QUERIES = ["PDUFA", "target action date"]
FORMS = "8-K,10-Q,10-K,6-K"

KEY_RE = re.compile(r"pdufa|target action date|goal date|action date", re.I)
DATE_RE = re.compile(rf"\b{MON_RE}\s+(\d{{1,2}}),?\s+(20\d\d)\b", re.I)
DECIDED_RE = re.compile(r"\bfda (has )?approved\b|\bapproved by the (u\.s\. )?fda\b|received (u\.s\. )?(fda )?approval|\bfda approval of\b|complete response letter", re.I)
IFFY_RE = re.compile(r"\b(if|whether|could|would|may|potential(ly)?|upon)\b", re.I)


def sheet_tickers():
    """Tickers on the catalyst sheet (read first, so they're checked on the first run)."""
    try:
        with open(os.path.join(OUT_DIR, "catalysts.json"), encoding="utf-8") as f:
            c = json.load(f)
        return {t for r in c.get("catalysts") or [] for t in re.findall(r"[A-Z]{1,5}", str(r.get("ticker") or "").upper())}
    except (OSError, ValueError):
        return set()


def search(q, start, end, page):
    params = {"q": f'"{q}"', "dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat(), "forms": FORMS, "from": page * 100}
    r = requests.get("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params), headers=UA, timeout=30)
    r.raise_for_status()
    return r.json().get("hits", {})


def extract(text, filed):
    """[(date, sentence)] for every FDA action date stated in a filing, plus sentences saying a decision came in."""
    dates, decided = [], []
    for s in re.split(r"(?<=[.;])\s+(?=[A-Z•])|\s•\s", text):
        s = s.strip()
        if len(s) < 20 or len(s) > 900:
            continue
        if DECIDED_RE.search(s) and not IFFY_RE.search(s):
            decided.append(s[:300])
        keys = [m.end() for m in KEY_RE.finditer(s)]
        if not keys:
            continue
        best = None
        for m in DATE_RE.finditer(s):
            try:
                d = dt.date(int(m.group(3)), MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
            except ValueError:
                continue
            if d < filed:
                continue   # a past date: when it was submitted, accepted, or "as of"
            # the date that sits closest after "PDUFA ... date of" (or just before it: "a December 22 PDUFA date")
            gap = min((m.start() - k if m.start() >= k else (k - m.end()) * 3) for k in keys)
            if gap <= 160 and (best is None or gap < best[0]):
                best = (gap, d)
        if best:
            dates.append((best[1].isoformat(), s[:400]))
    return dates, decided


def main():
    today = dt.datetime.now(CT).date()
    try:
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("_v") != CACHE_V:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    cache["_v"] = CACHE_V
    sheet = sheet_tickers()

    # month-sized slices, because one search returns at most 1,000 hits
    hits = {}
    end = today
    while end > today - dt.timedelta(days=LOOKBACK_DAYS):
        start = max(end - dt.timedelta(days=30), today - dt.timedelta(days=LOOKBACK_DAYS))
        for q in QUERIES:
            for page in range(10):
                try:
                    h = search(q, start, end, page)
                except Exception as e:  # noqa: BLE001
                    log("search failed:", q, start, e)
                    break
                rows = h.get("hits") or []
                for x in rows:
                    src = x.get("_source") or {}
                    sics = set(src.get("sics") or [])
                    if sics and not (sics & BIO_SIC):
                        continue
                    names = src.get("display_names") or []
                    m = re.search(r"\(([A-Z]{1,5})(?:[,)])", names[0]) if names else None
                    if m:
                        hits[x["_id"]] = {"ticker": m.group(1), "company": re.sub(r"\s+\(.*$", "", names[0]).strip(),
                                          "form": src.get("form") or "", "filed": src.get("file_date"), "cik": (src.get("ciks") or [""])[0]}
                if len(rows) < 100:
                    break
                time.sleep(0.12)
        end = start - dt.timedelta(days=1)
    log(f"{len(hits)} filings mention an FDA action date in the last {LOOKBACK_DAYS} days")

    # sheet companies first, then newest filings, so the dates that matter are right on the first run
    order = sorted(hits.items(), key=lambda kv: (kv[1]["ticker"] not in sheet, -(dt.date.fromisoformat(kv[1]["filed"]).toordinal() if kv[1]["filed"] else 0)))
    t0, read = time.time(), 0
    for _id, h in order:
        if _id in cache:
            continue
        if time.time() - t0 > BUDGET_S:
            break
        acc, fname = _id.split(":", 1)
        url = f"https://www.sec.gov/Archives/edgar/data/{int(h['cik'] or 0)}/{acc.replace('-', '')}/{fname}"
        try:
            ds, dec = extract(doc_text(url), dt.date.fromisoformat(h["filed"]))
            cache[_id] = {**h, "url": url, "dates": ds, "decided": dec if h["form"].startswith(("8-K", "6-K")) else []}
        except Exception as e:  # noqa: BLE001
            log("read failed:", url, e)
            cache[_id] = {**h, "url": url, "dates": [], "decided": [], "error": True}
        read += 1
        time.sleep(0.12)
    cutoff = (today - dt.timedelta(days=LOOKBACK_DAYS + 30)).isoformat()
    cache = {k: v for k, v in cache.items() if k == "_v" or (v.get("filed") or "") >= cutoff}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))

    # one entry per (company, drug): the newest filing's date wins, so a 3-month extension replaces the old date
    by_t = {}
    for k, v in cache.items():
        if k != "_v":
            by_t.setdefault(v["ticker"], []).append(v)
    items = []
    for t, fs in by_t.items():
        fs.sort(key=lambda v: v.get("filed") or "", reverse=True)
        stated = []   # (filed, date, names, sentence, filing)
        for v in fs:
            for d, s in v.get("dates") or []:
                stated.append((v["filed"], d, program_names(s), s, v))
        kept = []
        for filed, d, names, s, v in stated:
            if d < (today - dt.timedelta(days=7)).isoformat():
                continue
            # a newer filing gives this drug a different date and doesn't repeat this one -> it moved
            newer = [x for x in stated if x[0] > filed and (bool(names & x[2]) if names and x[2] else not names and not x[2])]
            if newer and not any(x[1] == d for x in newer):
                continue
            if any(x["date"] == d for x in kept):
                continue
            # a decision already came in: an 8-K after the filing says approved / complete response letter for this drug
            done = [dv for dv in fs if dv["filed"] >= filed and dv["filed"] >= (dt.date.fromisoformat(d) - dt.timedelta(days=75)).isoformat()
                    and any(not names or any(n in x.lower() for n in names) for x in dv.get("decided") or [])]
            kept.append({"ticker": t, "company": v["company"], "date": d, "text": s, "drugs": sorted(names)[:4], "filed": filed,
                         "form": v["form"], "link": v["url"], "decided": bool(done),
                         "decided_text": (done[0]["decided"][0] if done else ""), "decided_link": (done[0]["url"] if done else "")})
        items += kept
    items.sort(key=lambda x: (x["date"], x["ticker"]))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": items}, f, separators=(",", ":"))
    ahead = [i for i in items if i["date"] >= today.isoformat() and not i["decided"]]
    print(f"Wrote {OUT}: {len(items)} FDA dates ({len(ahead)} still ahead); read {read} new filings, {len(cache) - 1} cached")
    for i in ahead[:60]:
        print(f"   {i['date']}  {i['ticker']:6} {', '.join(i['drugs'])[:30]:30} {i['text'][:100]}")


if __name__ == "__main__":
    main()
