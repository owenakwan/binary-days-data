"""
Guided catalysts for every company, found automatically in SEC filings.

Biotechs say when data is coming in their quarterly results and press releases, filed with the SEC as 8-K exhibits
and 10-Qs: "topline data expected in Q4 2026" (IRON), "topline results expected in October 2026" (RAPP), "PDUFA target
action date of March 3, 2027". This searches the SEC's free full-text search for that kind of wording over the last
~7 months, reads each filing once (remembered in out/guidance_cache.json), pulls out every guided event with its time
window, and drops the ones a later filing shows already happened ("met its primary endpoint", "announces topline").

Writes out/guidance.json. Runs every data update; new filings are cheap because old ones are cached.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse
from zoneinfo import ZoneInfo

import requests

OUT_DIR = os.environ.get("OUT_DIR", "out")
OUT = os.path.join(OUT_DIR, "guidance.json")
CACHE = os.path.join(OUT_DIR, "guidance_cache.json")
CT = ZoneInfo("America/Chicago")
UA = {"User-Agent": f"binary-days data builder {os.environ.get('SEC_CONTACT', '').encode('ascii', 'ignore').decode().strip() or 'contact via github.com/owenakwan'}"}
LOOKBACK_DAYS = 210
BUDGET_S = 420            # time for reading new filings per run; the rest is picked up next run
PHRASES = [
    "topline data expected", "topline results expected", "top-line data expected", "top-line results expected",
    "expects to report topline", "expect to report topline", "topline data anticipated", "topline results anticipated",
    "topline data in the fourth quarter", "topline data in the first quarter", "readout expected", "data readout",
    "initial data expected", "interim data expected", "data expected in the fourth quarter", "results expected in the fourth quarter",
    "PDUFA target action date", "PDUFA goal date", "target action date of", "on track to report",
    "results are expected in the fourth quarter", "data are expected in the fourth quarter", "are expected in the fourth quarter",
    "topline data results are expected", "data is expected in", "expected to read out", "expects to announce topline", "anticipates reporting topline",
]
# Results wording, so a readout that happened is noticed even when the release has no guidance phrases in it
RESULT_PHRASES = ["primary endpoint", "co-primary endpoints", "announces positive topline", "announces topline", "topline results from",
                  "did not meet", "statistically significant", "complete response letter", "FDA approval of"]
BIO_SIC = {"2833", "2834", "2835", "2836", "8731", "8733", "3841", "3845", "8071", "2830"}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
MON_RE = r"(january|february|march|april|may|june|july|august|september|october|november|december)"
ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}

EVENT_RE = re.compile(r"top-?line|data|results|readout|read out|pdufa|action date|approval decision|fda decision", re.I)
GUIDE_RE = re.compile(r"expect|anticipat|on track|plan(s|ned)? to|guid|projected|scheduled", re.I)
BOILER_RE = re.compile(r"forward-looking|risks and uncertainties|could cause actual results|undue reliance", re.I)
RESULT_RE = re.compile(r"announce[sd]? (positive |negative )?top-?line|reported (positive )?top-?line|positive top-?line (data|results)|top-?line (data|results) from (the|its|our)|"
                       r"\bmet (\w+ ){0,3}(co-)?primary (efficacy )?end ?points?|achieved (\w+ ){0,3}(co-)?primary (efficacy )?end ?points?|"
                       r"did not (meet|achieve)|failed to (meet|achieve)|missed (\w+ ){0,2}primary|statistically significant (improvement|reduction)|"
                       r"\bfda (has )?approved\b|received (fda )?approval|complete response letter", re.I)


def log(*a): print(*a, file=sys.stderr)


def month_end(y, m):
    return (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))


def window(text, filed):
    """(start, end, label) for the first time phrase in text, or None. Relative phrases use the filing date."""
    t = text.lower()
    cands = []
    for m in re.finditer(rf"\b{MON_RE}\s+(\d{{1,2}}),?\s+(20\d\d)\b", t):
        d = dt.date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))
        cands.append((m.start(), d, d, f"{m.group(1).title()} {int(m.group(2))}, {m.group(3)}"))
    for m in re.finditer(rf"\b(early|mid|late|end of)[- ]?{MON_RE}\s+(20\d\d)\b|\b{MON_RE}\s+(20\d\d)\b", t):
        if m.group(2):
            y, mo, part = int(m.group(3)), MONTHS.index(m.group(2)) + 1, m.group(1)
        else:
            y, mo, part = int(m.group(5)), MONTHS.index(m.group(4)) + 1, ""
        s, e = dt.date(y, mo, 1), month_end(y, mo)
        if part == "early":
            e = dt.date(y, mo, 12)
        elif part == "mid":
            s, e = dt.date(y, mo, 8), dt.date(y, mo, 24)
        elif part in ("late", "end of"):
            s = dt.date(y, mo, 18)
        cands.append((m.start(), s, e, (part + " " if part else "") + f"{MONTHS[mo - 1].title()} {y}"))
    for m in re.finditer(r"\b(?:q([1-4])|([1-4])q)\s*'?(20\d\d|\d\d)\b|\b(first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter(?: of)?(?: the year)?\s+(20\d\d)\b", t):
        q = int(m.group(1) or m.group(2) or ORD[m.group(4)])
        y = int(m.group(3) or m.group(5))
        y = y + 2000 if y < 100 else y
        cands.append((m.start(), dt.date(y, 3 * q - 2, 1), month_end(y, 3 * q), f"Q{q} {y}"))
    for m in re.finditer(r"\b(?:(first|second) half|([12])h|h([12]))(?: of)?\s+(20\d\d)\b", t):
        h = 1 if (m.group(1) == "first" or m.group(2) == "1" or m.group(3) == "1") else 2
        y = int(m.group(4))
        cands.append((m.start(), dt.date(y, 1 if h == 1 else 7, 1), dt.date(y, 6, 30) if h == 1 else dt.date(y, 12, 31), f"{'1H' if h == 1 else '2H'} {y}"))
    for m in re.finditer(r"\b(early|mid|late|end of|year-end|by year-end|by the end of)[- ]?(20\d\d)\b", t):
        y, p = int(m.group(2)), m.group(1)
        s, e = {"early": (dt.date(y, 1, 1), dt.date(y, 4, 30)), "mid": (dt.date(y, 5, 1), dt.date(y, 8, 31)),
                "late": (dt.date(y, 9, 1), dt.date(y, 12, 31))}.get(p, (dt.date(y, 10, 1), dt.date(y, 12, 31)))
        cands.append((m.start(), s, e, f"{p.replace('by the end of', 'end of')} {y}"))
    for m in re.finditer(r"\b(by (the )?(end of the year|year[- ]end)|later this year|this month|in the coming weeks|before the end of the year)\b", t):
        y = filed.year
        p = m.group(1)
        if "month" in p:
            s, e = filed, month_end(filed.year, filed.month)
        elif "weeks" in p:
            s, e = filed, filed + dt.timedelta(days=42)
        else:
            s, e = filed, dt.date(y, 12, 31)
        cands.append((m.start(), s, e, p))
    if not cands:
        return None
    pos, s, e, label = min(cands, key=lambda c: c[0])
    return s, e, label


def doc_text(url):
    r = requests.get(url, headers=UA, timeout=30, stream=True)
    r.raise_for_status()
    raw = r.raw.read(2_500_000, decode_content=True).decode("utf-8", "ignore")   # big 10-Qs: the first 2.5 MB is plenty
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", t.replace("•", " • ").replace("●", " • ").replace("\xa0", " "))


def extract(text, filed):
    """Guided events and result statements in one filing."""
    sents = re.split(r"(?<=[.;])\s+(?=[A-Z•])|\s•\s|\s·\s|\so\s(?=[A-Z])", text)
    out, results = [], []
    for i, s in enumerate(sents):
        s = s.strip()
        if len(s) < 25 or len(s) > 700 or BOILER_RE.search(s):
            continue
        if RESULT_RE.search(s) and not re.search(r"\b(if|whether|could|would|may|potential(ly)?)\b", s, re.I):
            results.append(s[:300])
        if not (EVENT_RE.search(s) and GUIDE_RE.search(s)):
            continue
        w = window(s, filed)
        if not w:
            continue
        start, end, label = w
        if (end - start).days > 200 or end < filed - dt.timedelta(days=20):
            continue   # too vague ("in 2027") or already past when written
        if end <= filed + dt.timedelta(days=3):
            continue   # about the filing day itself (e.g. "today announced topline results"), not a future event
        if start == end and not re.search(r"pdufa|action date|meeting|conference|webcast|presentation", s, re.I):
            continue   # exact dates are usually "as of December 31" or a cash-runway date, unless it's a PDUFA / meeting
        low = s.lower()
        kind = ("FDA decision" if re.search(r"pdufa|action date|approval decision|fda decision", low)
                else "Topline data" if re.search(r"top-?line", low) else "Initial data" if re.search(r"initial|interim|preliminary", low) else "Data")
        ctx = sents[i - 1].strip()[-160:] if i and len(s) < 90 else ""
        out.append({"kind": kind, "start": start.isoformat(), "end": end.isoformat(), "when": label, "text": s[:420], "ctx": ctx})
    return out, results[:12]


NOT_NAMES = {"phase", "topline", "top-line", "data", "results", "expected", "quarter", "fourth", "third", "second", "first", "trial", "study",
             "company", "fda", "the", "this", "with", "from", "pivotal", "initial", "interim", "report", "reports", "anticipated", "patients",
             "program", "clinical", "readout", "half", "year", "track", "plans", "planned", "expects", "expect", "following", "completed",
             "enrollment", "including", "additional", "submission", "approval", "decision", "target", "action", "date", "pdufa", "bla", "nda",
             "crl", "sec", "cash", "nasdaq", "nyse", "inc", "ceo", "cfo", "usd", "eps"}


def program_names(text):
    """Drug and trial names in a sentence: APOLLO, RAP-219, DURAVYU, bitopertin, KSI-101 (lowercased)."""
    out = set()
    for w in re.findall(r"\b([A-Z]{3,}[A-Z0-9-]*|[A-Za-z]+-\d+[A-Za-z0-9]*|[a-z]{5,}(?:mab|nib|tide|gene|stat|tinib|ciclib|parib|vir|ine|ide|ant|ast)\b|[a-z]+(?:mab|tinib))\b", text):
        lw = w.lower()
        if lw not in NOT_NAMES and len(lw) >= 4:
            out.add(lw)
    return out


def efts(q, start, end, page, forms="8-K,10-Q,10-K"):
    params = {"q": f'"{q}"', "dateRange": "custom", "startdt": start.isoformat(), "enddt": end.isoformat(), "forms": forms, "from": page * 100}
    r = requests.get("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params), headers=UA, timeout=30)
    r.raise_for_status()
    return r.json().get("hits", {})


def main():
    today = dt.datetime.now(CT).date()
    try:
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("_v") != 4:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    cache["_v"] = 4
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    hits = {}
    jobs = [(q, start, "8-K,10-Q,10-K") for q in PHRASES] + [(q, today - dt.timedelta(days=120), "8-K") for q in RESULT_PHRASES]
    for q, since, forms in jobs:
        for page in range(5):
            try:
                h = efts(q, since, today, page, forms)
            except Exception as e:  # noqa: BLE001
                log("search failed:", q, e)
                break
            rows = h.get("hits") or []
            for x in rows:
                src = x.get("_source") or {}
                sics = set(src.get("sics") or [])
                if sics and not (sics & BIO_SIC):
                    continue
                names = src.get("display_names") or []
                m = re.search(r"\(([A-Z]{1,5})(?:[,)])", names[0]) if names else None
                if not m:
                    continue
                hits[x["_id"]] = {"ticker": m.group(1), "company": re.sub(r"\s+\(.*$", "", names[0]).strip(), "form": src.get("form") or "",
                                  "filed": src.get("file_date"), "cik": (src.get("ciks") or [""])[0]}
            if len(rows) < 100:
                break
            time.sleep(0.15)
        time.sleep(0.15)
    log(f"{len(hits)} matching filings in the last {LOOKBACK_DAYS} days")
    t0, read = time.time(), 0
    for _id, h in sorted(hits.items(), key=lambda kv: kv[1]["filed"] or "", reverse=True):
        if _id in cache:
            continue
        if time.time() - t0 > BUDGET_S:
            break
        acc, fname = _id.split(":", 1)
        url = f"https://www.sec.gov/Archives/edgar/data/{int(h['cik'] or 0)}/{acc.replace('-', '')}/{fname}"
        try:
            filed = dt.date.fromisoformat(h["filed"])
            ev, res = extract(doc_text(url), filed)
            if not h["form"].startswith("8-K"):
                res = []   # only 8-Ks count as "it read out"; 10-Qs re-describe old results
            cache[_id] = {**h, "url": url, "events": ev, "results": res}
        except Exception as e:  # noqa: BLE001
            log("read failed:", url, e)
            cache[_id] = {**h, "url": url, "events": [], "results": [], "error": True}
        read += 1
        time.sleep(0.12)
    cutoff = (today - dt.timedelta(days=LOOKBACK_DAYS + 30)).isoformat()
    cache = {k: v for k, v in cache.items() if k == "_v" or (v.get("filed") or "") >= cutoff}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))

    # results announced (a later filing says it read out) -> the guided event before it is done
    done = {}
    for k, v in cache.items():
        if k != "_v" and v.get("results"):
            done.setdefault(v["ticker"], []).append((v["filed"], " ".join(v["results"]).lower()))
    items, seen = [], set()
    for v in sorted((v for k, v in cache.items() if k != "_v"), key=lambda v: v.get("filed") or "", reverse=True):   # newest guidance first
        for e in v.get("events") or []:
            end = dt.date.fromisoformat(e["end"])
            if end < today - dt.timedelta(days=3):
                continue
            # read out already: a later 8-K reports results for the same drug / trial (APOLLO, RAP-219, bitopertin…),
            # filed inside or shortly before the window. Companies with several programs keep the other ones.
            names = program_names(e["text"] + " " + e.get("ctx", ""))
            window_from = (dt.date.fromisoformat(e["start"]) - dt.timedelta(days=45)).isoformat()
            if any(d > v["filed"] and d >= window_from and (not names or any(n in txt for n in names)) for d, txt in done.get(v["ticker"], [])):
                continue
            key = (v["ticker"], e["kind"], e["when"])
            if key in seen:
                continue
            seen.add(key)
            items.append({"ticker": v["ticker"], "company": v["company"], "kind": e["kind"], "when": e["when"], "start": e["start"], "end": e["end"],
                          "text": e["text"], "ctx": e.get("ctx", ""), "filed": v["filed"], "form": v["form"], "link": v["url"]})
    items.sort(key=lambda x: (x["end"], x["start"], x["ticker"]))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": items}, f, separators=(",", ":"))
    open_now = [i for i in items if i["start"] <= today.isoformat() <= i["end"]]
    print(f"Wrote {OUT}: {len(items)} guided events ({len(open_now)} with the window open now); read {read} new filings, {len(cache)} cached")
    for i in open_now[:40]:
        print(f"   {i['ticker']:6} {i['kind']:13} {i['when']:18} {i['text'][:110]}")


if __name__ == "__main__":
    main()
