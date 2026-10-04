#!/usr/bin/env python3
"""
Builds the website's data files (run by .github/workflows/update-site-data.yml):

  site/data/catalysts.json  - your spreadsheet's open catalysts, plus PDUFA / FDA advisory
                              committee dates discovered in news headlines (marked unverified)
  site/data/earnings.json   - every US earnings report for the next 5 weeks (Finnhub), with each
                              company's recent report times so the site can show its usual time

Nothing secret is written. The Finnhub key stays in GitHub Secrets.

Usage:  python build_site_data.py
Env:    FINNHUB_KEY (earnings + advisory committees), SEC_CONTACT (SEC User-Agent contact), XLSX_PATH, OUT_DIR
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from openpyxl import load_workbook

CT = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
XLSX = os.environ.get("XLSX_PATH") or os.path.join(HERE, "biotech_catalysts_master.xlsx")
OUT = os.environ.get("OUT_DIR") or os.path.join(HERE, "out")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "").strip()
SEC_UA = f"binary-days data builder {os.environ.get('SEC_CONTACT', '').strip() or 'contact via github.com/owenakwan'}"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
WEEKS_AHEAD = 5
HISTORY_DAYS = 400

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": BROWSER_UA})


def log(msg):
    print(msg, flush=True)


# ----------------------------------------------------------------------------- helpers
def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)):
        try:
            return (dt.datetime(1899, 12, 30) + dt.timedelta(days=float(v))).date()
        except (OverflowError, ValueError):
            return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def iso(d):
    return d.isoformat() if d else None


def finnhub(path, **params):
    if not FINNHUB_KEY:
        return None
    params["token"] = FINNHUB_KEY
    for attempt in range(3):
        try:
            r = SESSION.get(f"https://finnhub.io/api/v1/{path}", params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(3 + attempt * 3)
                continue
            if r.status_code in (401, 403):
                log(f"[warn] Finnhub {path}: HTTP {r.status_code} (not on the free plan or bad key)")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            log(f"[warn] Finnhub {path}: {e}")
            time.sleep(2)
    return None


def sec_tickers():
    """{TICKER: company title} and a list of (normalized name, ticker) for headline matching."""
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA}, timeout=30)
        r.raise_for_status()
        rows = list(r.json().values())
    except Exception as e:  # noqa: BLE001
        log(f"[warn] SEC ticker list unavailable: {e}")
        return {}, []
    names, by_name = {}, []
    for v in rows:
        t, title = v["ticker"].upper(), v["title"].strip()
        if title.isupper():
            title = title.title()
        names.setdefault(t, title)
        n = norm_company(title)
        if len(n) >= 6:
            by_name.append((n, t))
    by_name.sort(key=lambda x: -len(x[0]))  # longest names first so "Summit Therapeutics" beats "Summit"
    return names, by_name


SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|ag|sa|nv|se|holdings?|group|the|n\.v|s\.a)\b\.?", re.I)


def norm_company(s):
    s = SUFFIX.sub(" ", s.lower())
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", s)).strip()


# ----------------------------------------------------------------------------- spreadsheet
def load_spreadsheet():
    if not os.path.exists(XLSX):
        log("[warn] spreadsheet not found; catalysts list will only contain discovered items")
        return []
    wb = load_workbook(XLSX, data_only=True, read_only=True)
    ws = wb["Catalysts"] if "Catalysts" in wb.sheetnames else wb.worksheets[0]
    header, out = None, []
    for row in ws.iter_rows(values_only=True):
        if header is None:
            cells = [str(c).strip() if c is not None else "" for c in row]
            if "Ticker" in cells:
                header = cells
            continue
        rec = {header[i]: row[i] for i in range(min(len(header), len(row))) if header[i]}
        tk = str(rec.get("Ticker") or "").strip().upper()
        if not tk or str(rec.get("Status") or "").strip().lower() == "resolved":
            continue
        raw = rec.get("Date")
        out.append({
            "ticker": tk,
            "company": str(rec.get("Company") or "").strip(),
            "search": str(rec.get("Search Name") or rec.get("Company") or tk).strip(),
            "catalyst": str(rec.get("Catalyst") or "").strip(),
            "date_text": to_date(raw).strftime("%b %d, %Y") if isinstance(raw, (dt.date, dt.datetime)) else str(raw or "").strip(),
            "sort": iso(to_date(rec.get("Sort Date")) or to_date(raw)),
            "end": iso(to_date(rec.get("End Date"))),
            "type": str(rec.get("Type") or "").strip(),
            "status": str(rec.get("Status") or "").strip(),
            "notes": str(rec.get("Notes") or "").strip(),
        })
    wb.close()
    return out


# ----------------------------------------------------------------------------- discovery from news
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
FULL_DATE = re.compile(rf"\b({MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I)
EXCH_TICKER = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE|NYSE American|NYSE MKT|Cboe|OTC|OTCQX|OTCQB)[A-Za-z ]*:\s*([A-Z]{1,5})\)")
QUERIES = [
    ('"PDUFA" "target action date"', "PDUFA"),
    ('"PDUFA date"', "PDUFA"),
    ('"PDUFA goal date"', "PDUFA"),
    ('FDA accepts application "PDUFA"', "PDUFA"),
    ('FDA "advisory committee" meeting scheduled', "AdComm"),
]


def parse_when(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        return None


def google_news(query):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        r = SESSION.get(url, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        log(f"[warn] Google News {query!r}: {e}")
        return []
    items = []
    for it in root.iter("item"):
        title = html.unescape(it.findtext("title") or "")
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        items.append({"title": title, "text": title + " " + desc, "link": it.findtext("link") or "",
                      "source": it.findtext("source") or "", "published": parse_when(it.findtext("pubDate"))})
    return items


def parse_full_date(m):
    try:
        return dt.datetime.strptime(f"{m.group(1)[:3].title()} {m.group(2)} {m.group(3)}", "%b %d %Y").date()
    except ValueError:
        return None


def discover(today, names, by_name, known):
    found = {}
    for q, kind in QUERIES:
        for it in google_news(q):
            text = it["text"]
            # take the first future date that appears near the keyword
            kw = re.search(r"PDUFA|target action date|goal date|advisory committee", text, re.I)
            if not kw:
                continue
            cands = [(abs(m.start() - kw.start()), parse_full_date(m)) for m in FULL_DATE.finditer(text)]
            cands = sorted((dist, d) for dist, d in cands if d and today <= d <= today + dt.timedelta(days=400))
            if not cands or cands[0][0] > 160:
                continue
            date = cands[0][1]
            m = EXCH_TICKER.search(text)
            ticker = m.group(1) if m else None
            if not ticker:
                head = norm_company(it["title"])
                for n, t in by_name:
                    if head.startswith(n + " ") or head == n:
                        ticker = t
                        break
            if not ticker:
                continue
            if (ticker, date.isoformat()) in known:
                continue
            key = (ticker, date.isoformat())
            if key in found:
                continue
            title = re.sub(r"\s+-\s+[^-]{2,60}$", "", it["title"])
            found[key] = {
                "ticker": ticker, "company": names.get(ticker, ""), "date": date.isoformat(), "type": kind,
                "headline": title, "link": it["link"], "source": it["source"],
                "published": it["published"].date().isoformat() if it["published"] else None,
            }
        time.sleep(0.8)
    log(f"Discovered {len(found)} dated events from news")
    return sorted(found.values(), key=lambda r: (r["date"], r["ticker"]))


def adcomm_calendar(today, known):
    data = finnhub("fda-advisory-committee-calendar") or []
    out = []
    for r in data if isinstance(data, list) else []:
        d = to_date((r.get("fromDate") or "")[:10])
        if not d or d < today:
            continue
        out.append({"ticker": "", "company": "", "date": d.isoformat(), "type": "AdComm",
                    "headline": (r.get("eventDescription") or "FDA advisory committee meeting").strip()[:240],
                    "link": r.get("url") or "", "source": "FDA (via Finnhub)", "published": None})
    log(f"FDA advisory committee meetings ahead: {len(out)}")
    return out


# ----------------------------------------------------------------------------- earnings
def monday(d):
    return d - dt.timedelta(days=d.weekday())


def earnings(today, names):
    if not FINNHUB_KEY:
        log("[warn] FINNHUB_KEY missing: earnings calendar skipped")
        return None
    ok_sym = re.compile(r"^[A-Z]{1,5}$")
    start, end = monday(today), monday(today) + dt.timedelta(days=7 * WEEKS_AHEAD - 1)
    rows, seen = [], set()
    d = start
    while d <= end:  # one call per week keeps each response small
        chunk_end = min(d + dt.timedelta(days=6), end)
        res = finnhub("calendar/earnings", **{"from": d.isoformat(), "to": chunk_end.isoformat()}) or {}
        for e in res.get("earningsCalendar", []):
            s = (e.get("symbol") or "").upper()
            if not ok_sym.match(s) or (s, e.get("date")) in seen:
                continue
            seen.add((s, e.get("date")))
            rows.append([s, e.get("date"), e.get("hour") or "", e.get("epsEstimate"), e.get("revenueEstimate"),
                         e.get("quarter"), e.get("year")])
        d = chunk_end + dt.timedelta(days=1)
        time.sleep(1.1)
    log(f"Upcoming earnings rows: {len(rows)}")

    upcoming = {r[0] for r in rows}
    # Report-time history builds up over time: keep what earlier runs saved, add reports whose
    # dates have now passed, and ask Finnhub for history only once a day (it's ~30 calls).
    prev = {}
    try:
        with open(os.path.join(OUT, "earnings.json"), encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        pass
    hist = {}
    def add(s, date, hour, act, est):
        if date:
            hist.setdefault(s, {})[date] = [date, hour or "", act, est]
    for s, items in (prev.get("history") or {}).items():
        for x in items:
            add(s, *x[:4])
    for r in prev.get("rows") or []:
        if r[1] and r[1] < today.isoformat() and r[2]:
            add(r[0], r[1], r[2], None, r[3])
    if prev.get("history_fetched") != today.isoformat():
        d = today - dt.timedelta(days=HISTORY_DAYS)
        while d < today:
            chunk_end = min(d + dt.timedelta(days=13), today - dt.timedelta(days=1))
            res = finnhub("calendar/earnings", **{"from": d.isoformat(), "to": chunk_end.isoformat()}) or {}
            for e in res.get("earningsCalendar", []):
                s = (e.get("symbol") or "").upper()
                if s in upcoming:
                    add(s, e.get("date"), e.get("hour"), e.get("epsActual"), e.get("epsEstimate"))
            d = chunk_end + dt.timedelta(days=1)
            time.sleep(1.1)
        fetched = today.isoformat()
    else:
        fetched = prev.get("history_fetched")
    oldest = (today - dt.timedelta(days=450)).isoformat()  # keep about five quarters per company
    history = {s: [x for x in sorted(v.values(), key=lambda x: x[0], reverse=True) if x[0] >= oldest][:6] for s, v in hist.items()}
    history = {s: v for s, v in history.items() if v}
    log(f"Report-time history on file for {len(history)} of {len(upcoming)} companies")

    rows.sort(key=lambda r: (r[1] or "", -(r[4] or 0)))
    return {
        "sample": False,
        "updated": dt.datetime.now(CT).isoformat(timespec="minutes"),
        "from": start.isoformat(), "to": end.isoformat(),
        "fields": ["symbol", "date", "hour", "epsEstimate", "revenueEstimate", "quarter", "year"],
        "rows": rows,
        "history_fields": ["date", "hour", "epsActual", "epsEstimate"],
        "history": history,
        "history_fetched": fetched,
        "names": {s: names[s] for s in upcoming if s in names},
    }


# ----------------------------------------------------------------------------- main
def write(name, obj):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    log(f"Wrote {path} ({os.path.getsize(path):,} bytes)")


def main():
    today = dt.datetime.now(CT).date()
    names, by_name = sec_tickers()

    cats = load_spreadsheet()
    if not cats:  # spreadsheet unreachable this run: keep the last good list instead of blanking the board
        try:
            with open(os.path.join(OUT, "catalysts.json"), encoding="utf-8") as f:
                cats = json.load(f).get("catalysts") or []
            log(f"Using the previous catalyst list ({len(cats)} rows)")
        except (OSError, ValueError):
            cats = []
    known = {(c["ticker"], c["sort"]) for c in cats if c["sort"]}
    disc = discover(today, names, by_name, known) + adcomm_calendar(today, known)
    disc.sort(key=lambda r: (r["date"], r["ticker"]))
    write("catalysts.json", {
        "sample": False,
        "updated": dt.datetime.now(CT).isoformat(timespec="minutes"),
        "catalysts": cats,
        "discovered": disc,
    })

    earn = earnings(today, names)
    if earn is not None and earn["rows"]:
        write("earnings.json", earn)
    else:
        log("Earnings file left unchanged (no data this run)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"[error] {type(e).__name__}: {e}")
        sys.exit(1)
