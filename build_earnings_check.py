"""Earnings dates for stocks with 2x leveraged ETFs, checked three ways -> earnings_check.json, leveraged.json and an
"Earnings check" note in the Obsidian vault.

  1. Finnhub's earnings calendar (earnings.json)
  2. Yahoo: its next date and whether Yahoo marks it confirmed (via the Binary Days API, /api/earnings)
  3. The company's own announcement: "Aehr Test Systems to Announce First Quarter Fiscal 2027 Financial Results on
     November 2, 2026" (headlines and summaries via /api/news)

Status:  announced - the company's release names the date (strongest)
         confirmed - Finnhub and Yahoo agree and Yahoo marks it confirmed
         conflict  - the company's release names a different date (its date is used)
         estimate  - none of the above; left out of the emails
The 2x earnings email reads this file, so the email and the vault always say the same thing.
"""
import datetime as dt
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

CT = ZoneInfo("America/Chicago")
OUT_DIR = os.environ.get("OUT_DIR", "out")
VAULT = os.environ.get("VAULT_DIR", "vault_out")
API = "https://binary-days-api.binary-days-api.workers.dev"
NASDAQ_DIR = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
DAYS = 10
MAX_AGE_H = 3
ALIAS = {"apple": "AAPL", "alphabet": "GOOGL", "google": "GOOGL", "nvidia": "NVDA", "amazon": "AMZN", "microsoft": "MSFT", "meta": "META",
         "tesla": "TSLA", "netflix": "NFLX", "broadcom": "AVGO", "palantir": "PLTR", "brkb": "BRK-B", "coinbase": "COIN", "oracle": "ORCL"}
NOT_CO = {"daily", "target", "long", "short", "china", "bitcoin", "ether", "copper", "gold", "silver", "india", "memory", "dram", "space", "quantum"}
PAT = re.compile(r"(?:\b(?:Long|Short|Inverse|Daily|Corgi|Bull|Bear)\s+)([A-Za-z][A-Za-z.&-]{0,14})\s+(?:Daily|Bull|Bear|2x|2X|ETF|Target)\b")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
# "...to Announce/Report/Release ... Results on November 2, 2026" / "...will report ... results on Thursday, October 29"
ANNOUNCE_RE = re.compile(r"\b(to|will)\s+(announce|report|release|host)\b.{0,120}?\b(results?|earnings)\b.{0,80}?\bon\s+(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"
                         r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?", re.I)
HOUR_RE = [(re.compile(r"after (the )?(u\.s\. )?(stock )?market('s)? clos|after the close|after market close", re.I), "amc"),
           (re.compile(r"before (the )?(u\.s\. )?(stock )?market('s)? open|before the open|prior to the (market )?open|pre-?market", re.I), "bmo")]


def leveraged_map():
    """{stock: {"long": [...], "short": [...]}} from the Nasdaq symbol directory (every US-listed security)."""
    r = requests.get(NASDAQ_DIR, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    rows = [x for x in (l.split("|") for l in r.text.splitlines()[1:]) if len(x) > 7 and x[7] == "N"]
    stocks = {x[1]: x[2] for x in rows if x[5] == "N"}
    first = {}
    for s, n in stocks.items():
        w = re.split(r"[\s,.]+", n)[0].lower()
        if len(w) >= 4:
            first.setdefault(w, set()).add(s)
    out = {}
    for x in rows:
        sym, name = x[1], x[2]
        if x[5] != "Y" or not re.search(r"\b2\s?x\b|200%", name, re.I) or "daily" not in name.lower():
            continue
        und = None
        for m in PAT.finditer(name):
            tok = m.group(1)
            if tok.upper() == tok and tok in stocks:
                und = tok
            elif tok.lower() in ALIAS:
                und = ALIAS[tok.lower()]
            elif tok.lower() not in NOT_CO and len(first.get(tok.lower(), ())) == 1:
                und = next(iter(first[tok.lower()]))
            if und:
                break
        if und:
            side = "short" if re.search(r"\b(short|bear|inverse)\b", name, re.I) else "long"
            out.setdefault(und.replace(".", "-"), {"long": [], "short": []})[side].append(sym)
    return out


def get(path, **params):
    try:
        return requests.get(API + path, params=params, timeout=40).json()
    except Exception:  # noqa: BLE001
        return {}


def announcement(t, name, near):
    """The company's own 'to announce results on <date>' headline, if there is one: (date, hour, title, link)."""
    items = (get("/api/news", s=t).get("items") or [])
    if not items:
        return "failed"   # the news lookup failed: don't let the calendars alone pass it
    word = (name or t).split()[0].lower()
    best = None
    for i in items:
        text = f"{i.get('title', '')}. {i.get('summary', '')}"
        if not (t in text or word in text.lower()):
            continue
        m = ANNOUNCE_RE.search(text)
        if not m:
            continue
        pub = (i.get("time") or "")[:10]
        try:
            y = int(m.group(6)) if m.group(6) else int(pub[:4] or near.year)   # groups: to/will, verb, results, month, day, year
            d = dt.date(y, MONTHS.index(m.group(4).lower()) + 1, int(m.group(5)))
        except ValueError:
            continue
        if pub and d.isoformat() < pub:
            continue   # an old release about a past quarter
        hour = next((h for rx, h in HOUR_RE if rx.search(text)), "")
        cand = (d.isoformat(), hour, i.get("title", "")[:200], i.get("link", ""), pub)
        if best is None or cand[4] > best[4]:   # newest announcement wins
            best = cand
    return best


def check(t, cal_row, today):
    e = get("/api/earnings", s=t)
    nx = e.get("next") or {}
    name = e.get("name") or t
    ann = announcement(t, name, today)
    failed = ann == "failed" or not e
    if failed:
        ann = None
    srcs = []
    fin_date = cal_row[1] if cal_row else None
    if fin_date:
        srcs.append({"name": "Finnhub calendar", "date": fin_date})
    y_conf = nx.get("confirmed") is True and nx.get("date") == fin_date
    if nx.get("date"):
        srcs.append({"name": "Yahoo" + (" (confirmed)" if nx.get("confirmed") else " (estimate)"), "date": nx["date"]})
    if ann:
        srcs.append({"name": "company announcement", "date": ann[0], "title": ann[2], "link": ann[3], "published": ann[4]})
    date, hour = fin_date or nx.get("date"), (cal_row[2] if cal_row else "") or nx.get("hour") or ""
    if ann and ann[0] >= today.isoformat():
        status = "announced" if ann[0] == date else "conflict"
        date, hour = ann[0], ann[1] or hour
    else:
        status = "confirmed" if y_conf and not failed else "estimate"
    note = (f"Calendars said {fin_date or nx.get('date')}; the company's release says {ann[0]}." if status == "conflict"
            else "A lookup failed; checking again next run." if failed else "")
    return t, {"date": date, "hour": hour, "status": status, "name": name, "sources": srcs, "note": note,
               "at": 0 if failed else time.time()}   # failed lookups are re-checked on the next run


def main():
    today = dt.datetime.now(CT).date()
    lev = leveraged_map()
    with open(os.path.join(OUT_DIR, "leveraged.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": lev}, f, separators=(",", ":"))
    try:
        with open(os.path.join(OUT_DIR, "earnings.json"), encoding="utf-8") as f:
            cal = json.load(f)
    except (OSError, ValueError):
        cal = {}
    end = (today + dt.timedelta(days=DAYS)).isoformat()
    rows = {r[0]: r for r in cal.get("rows") or [] if r[0] in lev and today.isoformat() <= r[1] <= end}
    try:
        with open(os.path.join(OUT_DIR, "earnings_check.json"), encoding="utf-8") as f:
            prev = json.load(f).get("items") or {}
    except (OSError, ValueError):
        prev = {}
    now = time.time()
    keep = {t: v for t, v in prev.items() if t in rows and now - v.get("at", 0) < MAX_AGE_H * 3600 and v.get("date") >= today.isoformat()}
    todo = [t for t in rows if t not in keep]
    todo.sort(key=lambda t: rows[t][1])   # soonest reports first
    with ThreadPoolExecutor(3) as ex:
        for t, v in ex.map(lambda t: check(t, rows[t], today), todo):   # keep every result (a time cut here once dropped ACHR / QBTS)
            keep[t] = v
    with open(os.path.join(OUT_DIR, "earnings_check.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": keep}, f, separators=(",", ":"), ensure_ascii=False)
    counts = {s: sum(v["status"] == s for v in keep.values()) for s in ("announced", "confirmed", "conflict", "estimate")}
    print(f"Earnings check: {len(keep)} stocks with 2x ETFs report by {end} {counts}; {len(todo)} checked this run")
    write_note(keep, lev, today)


ICON = {"announced": "✅✅", "confirmed": "✅", "conflict": "⚠️", "estimate": "❓"}
WORD = {"announced": "company announced", "confirmed": "calendars agree, Yahoo confirmed", "conflict": "company says a different date", "estimate": "not confirmed"}


def write_note(items, lev, today):
    if not os.path.isdir(VAULT):
        return
    hours = {"amc": "after close", "bmo": "before open", "dmh": "during market hours"}
    lines = ["# Earnings check (stocks with 2x ETFs)", "",
             f"Updated {dt.datetime.now(CT):%a %b %d, %I:%M %p} CT. Next {DAYS} days. "
             + " · ".join(f"{ICON[s]} {WORD[s]}" for s in ICON), "",
             "Only ✅✅ and ✅ go in the 6:30am earnings email. ⚠️ uses the company's date. ❓ is left out.", ""]
    day = None
    for t, v in sorted(items.items(), key=lambda kv: (kv[1]["date"], {"bmo": 0, "dmh": 1, "amc": 2}.get(kv[1]["hour"], 3), kv[0])):
        d = dt.date.fromisoformat(v["date"])
        if d != day:
            day = d
            lines += ["", f"## {d:%A, %B} {d.day}", ""]
        etfs = ", ".join(lev.get(t, {}).get("long", [])[:3])
        lines.append(f"- {ICON[v['status']]} [[{t}]] {v['name']} · {hours.get(v['hour'], 'time not given')} · 2x: {etfs}" + (f" — _{v['note']}_" if v["note"] else ""))
        for s in v["sources"]:
            lines.append(f"    - {s['name']}: {s['date']}" + (f" — [{s['title']}]({s['link']})" if s.get("link") else ""))
    open(os.path.join(VAULT, "Earnings check.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
