#!/usr/bin/env python3
"""
Builds site/data/insiders.json: open-market insider BUYS (code P) and SELLS (code S)
from SEC Form 4 filings, for the website's Insiders page.

Sources (free, official, no key):
  - SEC daily form index  -> every Form 4 filed on each finished business day
  - SEC "latest filings" Atom feed -> today's Form 4s so far
Each filing's full text gives the trades (XML) and the company's industry (SIC code),
which is how biotech / pharma names get flagged.

State (which filings were already read) lives in insider_state.json next to the output,
so each run only downloads new filings. Run by .github/workflows/update-data.yml.

Usage:  python build_insiders.py
Env:    SEC_CONTACT (contact email for the SEC User-Agent, as the SEC requires), FINNHUB_KEY, OUT_DIR
"""
import datetime as dt
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests

CT = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("OUT_DIR") or os.path.join(HERE, "out")
OUT = os.path.join(OUT_DIR, "insiders.json")
STATE = os.path.join(OUT_DIR, "insider_state.json")
KEEP_DAYS = 14            # how much history the website shows
BACKFILL_BUSINESS_DAYS = 5
MIN_BUY = 1_000           # skip tiny trades
MIN_SELL = 10_000
MAX_FETCH = int(os.environ.get("MAX_FETCH", "4000"))   # safety cap per run
BIOTECH_SIC = {"2833", "2834", "2835", "2836", "8731"}
UA = {"User-Agent": f"binary-days insider data {os.environ.get('SEC_CONTACT', '').encode('ascii', 'ignore').decode().strip() or 'contact via github.com/owenakwan'}",
      "Accept-Encoding": "gzip, deflate"}
S = requests.Session()
S.headers.update(UA)
ACC_RE = re.compile(r"(\d{10}-\d{2}-\d{6})")


def log(m):
    print(m, flush=True)


def get(url, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + i * 3)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log(f"[warn] {url}: {e}")
            time.sleep(2 + i * 2)
    return None


# ----------------------------------------------------------------------------- discovery
def business_days_back(today, n):
    out, d = [], today - dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


def from_daily_index(day):
    q = (day.month - 1) // 3 + 1
    r = get(f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx")
    if r is None:
        return None  # not published yet (or a market holiday)
    found = {}
    for line in r.content.decode("latin-1").splitlines():
        if not (line.startswith("4 ") or line.startswith("4/A ")):
            continue
        m = re.search(r"(edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt)", line)
        if m:
            found.setdefault(m.group(3), m.group(1))
    return found


def from_atom(seen, pages=40):
    found = {}
    for p in range(pages):
        r = get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
                f"&owner=include&start={p * 100}&count=100&output=atom")
        if r is None:
            break
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            break
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall("a:entry", ns)
        if not entries:
            break
        new_on_page = 0
        for e in entries:
            link = e.find("a:link", ns)
            href = link.get("href") if link is not None else ""
            m = re.search(r"/data/(\d+)/\d+/(\d{10}-\d{2}-\d{6})-index", href)
            if not m:
                continue
            acc = m.group(2)
            if acc in seen or acc in found:
                continue
            found[acc] = f"edgar/data/{m.group(1)}/{acc}.txt"
            new_on_page += 1
        if new_on_page == 0 and p > 0:
            break  # caught up with filings read on an earlier run
        time.sleep(0.25)
    return found


# ----------------------------------------------------------------------------- parsing
def txt(node, path):
    el = node.find(path)
    return (el.text or "").strip() if el is not None and el.text else ""


def num(node, path):
    try:
        return float(txt(node, path).replace(",", ""))
    except ValueError:
        return None


def parse_filing(text, acc, path):
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text, re.S)
    if not m:
        return []
    try:
        doc = ET.fromstring(m.group(0))
    except ET.ParseError:
        return []
    ticker = txt(doc, "issuer/issuerTradingSymbol").upper().split()[0] if txt(doc, "issuer/issuerTradingSymbol") else ""
    if not ticker or ticker in ("NONE", "N/A", "NA"):
        return []
    company = txt(doc, "issuer/issuerName")
    owners, titles = [], []
    for o in doc.findall("reportingOwner"):
        owners.append(txt(o, "reportingOwnerId/rptOwnerName"))
        rel = o.find("reportingOwnerRelationship")
        if rel is not None:
            t = []
            if txt(rel, "isOfficer") in ("1", "true"):
                t.append(txt(rel, "officerTitle") or "Officer")
            if txt(rel, "isDirector") in ("1", "true"):
                t.append("Director")
            if txt(rel, "isTenPercentOwner") in ("1", "true"):
                t.append("10% owner")
            titles.append(", ".join(t) or txt(rel, "otherText") or "Other")
    plan = txt(doc, "aff10b5One") in ("1", "true")
    if not plan and re.search(r"10b5-1", text[m.start():m.end()], re.I):
        plan = True  # older filings mention the plan only in footnotes
    sic = re.search(r"ISSUER:.*?STANDARD INDUSTRIAL CLASSIFICATION:[^\[\n]*\[(\d{4})\]", text, re.S)
    biotech = bool(sic and sic.group(1) in BIOTECH_SIC)

    agg = {}
    for tr in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = txt(tr, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        sh = num(tr, "transactionAmounts/transactionShares/value") or 0
        px = num(tr, "transactionAmounts/transactionPricePerShare/value") or 0
        if sh <= 0 or px <= 0:
            continue
        a = agg.setdefault(code, {"shares": 0.0, "value": 0.0, "date": "", "after": None})
        a["shares"] += sh
        a["value"] += sh * px
        a["date"] = max(a["date"], txt(tr, "transactionDate/value")[:10])
        after = num(tr, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        if after is not None:
            a["after"] = after
    cik = path.split("/")[2]
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"
    out = []
    for code, a in agg.items():
        if a["value"] < (MIN_BUY if code == "P" else MIN_SELL):
            continue
        out.append({
            "acc": acc, "trade": a["date"], "ticker": ticker, "company": company,
            "insider": owners[0] if owners else "", "more": max(0, len(owners) - 1),
            "title": titles[0] if titles else "", "type": code,
            "shares": round(a["shares"]), "price": round(a["value"] / a["shares"], 4),
            "value": round(a["value"]), "after": round(a["after"]) if a["after"] is not None else None,
            "plan": plan, "bio": biotech, "url": url,
        })
    return out


def filed_date(text):
    m = re.search(r"FILED AS OF DATE:\s*(\d{8})", text)
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else None


# ----------------------------------------------------------------------------- clean-up
def merge_duplicates(rows):
    """Related entities (a fund and its general partner, say) often file the same trade separately.
    Keep one row per trade and count the extra filers in "more"."""
    out, by = [], {}
    for r in sorted(rows, key=lambda r: r["acc"]):
        k = (r["ticker"], r["type"], r["trade"], r["shares"], r["price"])
        if k in by:
            by[k]["more"] += 1 + r.get("more", 0)
            continue
        by[k] = r
        out.append(r)
    return out


def drop_bad_prices(rows):
    """Filers sometimes type a wrong price (e.g. $2,272,653 a share). Check unusually large trades
    against the real share price and drop ones that are off by more than 5x."""
    key = os.environ.get("FINNHUB_KEY", "").strip()
    quotes, keep = {}, []
    for r in rows:
        suspicious = r["price"] >= 500 or r["value"] >= 25_000_000
        if not suspicious:
            keep.append(r)
            continue
        if not key:
            if r["price"] < 5_000 and r["value"] < 2_000_000_000:
                keep.append(r)
            else:
                log(f"[drop] {r['ticker']} {r['type']} ${r['value']:,} (price ${r['price']:,} looks wrong; no Finnhub key to check)")
            continue
        if r["ticker"] not in quotes:
            try:
                q = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": r["ticker"], "token": key}, timeout=15).json()
                quotes[r["ticker"]] = float(q.get("c") or q.get("pc") or 0)
            except (requests.RequestException, ValueError):
                quotes[r["ticker"]] = 0.0
            time.sleep(1.1)
        real = quotes[r["ticker"]]
        if real and not (real / 5 <= r["price"] <= real * 5):
            log(f"[drop] {r['ticker']} {r['type']} filed price ${r['price']:,} vs market ${real:,.2f}")
            continue
        if not real and r["value"] >= 2_000_000_000:
            log(f"[drop] {r['ticker']} {r['type']} ${r['value']:,} (no market price to check)")
            continue
        keep.append(r)
    return keep


# ----------------------------------------------------------------------------- main
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    today = dt.datetime.now(CT).date()
    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    state = load(STATE, {"days_done": [], "seen": {}})
    seen = state.get("seen", {})          # accession -> filed date
    data = load(OUT, {})
    rows = [r for r in data.get("rows", []) if r.get("filed", "") >= cutoff] if not data.get("sample") else []

    todo = {a: p for a, p in state.get("pending", {}).items() if a not in seen}  # leftovers from a capped run
    for day in reversed(business_days_back(today, BACKFILL_BUSINESS_DAYS)):
        if day.isoformat() in state["days_done"]:
            continue
        found = from_daily_index(day)
        if found is None:
            log(f"{day}: daily index not published yet")
            continue
        new = {a: p for a, p in found.items() if a not in seen}
        log(f"{day}: {len(found)} Form 4 filings, {len(new)} not read yet")
        todo.update(new)
        state["days_done"].append(day.isoformat())
        time.sleep(0.3)
    live = from_atom(seen)
    log(f"Latest-filings feed: {len(live)} not read yet")
    for a, p in live.items():
        todo.setdefault(a, p)

    have = {(r["acc"], r["type"]) for r in rows}
    fetched = added = 0
    for acc, path in list(todo.items())[:MAX_FETCH]:
        r = get("https://www.sec.gov/Archives/" + path)
        fetched += 1
        if r is None:
            continue
        text = r.content.decode("utf-8", "replace")
        filed = filed_date(text) or today.isoformat()
        seen[acc] = filed
        for rec in parse_filing(text, acc, path):
            if (rec["acc"], rec["type"]) in have:
                continue
            rec["filed"] = filed
            rows.append(rec)
            have.add((rec["acc"], rec["type"]))
            added += 1
        if fetched % 250 == 0:
            log(f"  read {fetched}/{min(len(todo), MAX_FETCH)} filings, {added} buys/sells so far")
        time.sleep(0.11)  # SEC fair-access limit is 10 requests/second
    state["pending"] = dict(list(todo.items())[MAX_FETCH:])
    if state["pending"]:
        log(f"[note] {len(state['pending'])} filings left for the next run (MAX_FETCH cap)")

    rows = [r for r in rows if r.get("filed", "") >= cutoff]
    rows = merge_duplicates(rows)
    rows = drop_bad_prices(rows)
    rows.sort(key=lambda r: (r["filed"], r["value"]), reverse=True)
    state["seen"] = {a: d for a, d in seen.items() if d >= (today - dt.timedelta(days=KEEP_DAYS + 3)).isoformat()}
    state["days_done"] = sorted(d for d in set(state["days_done"]) if d >= cutoff)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"sample": False, "updated": dt.datetime.now(CT).isoformat(timespec="minutes"),
                   "days": KEEP_DAYS, "rows": rows}, f, separators=(",", ":"), ensure_ascii=False)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    buys = sum(1 for r in rows if r["type"] == "P")
    log(f"Read {fetched} filings, added {added}. Site now has {buys} buys and {len(rows) - buys} sells "
        f"({sum(1 for r in rows if r['bio'])} biotech/pharma) over {KEEP_DAYS} days.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"[error] {type(e).__name__}: {e}")
        sys.exit(1)
