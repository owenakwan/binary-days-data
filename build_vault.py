"""Cross-check every upcoming catalyst against every source we have -> verified.json + an Obsidian vault.

Each event (an FDA date, a readout, an FDA filing decision, earnings, a lock-up) gets a status:
  confirmed  - the company's own SEC filings say it (2+ filings), or one filing plus the sheet / a news story agree
  corrected  - the sheet had it wrong and the company's filings say otherwise (MLYS: Oct 23 -> Dec 22)
  conflict   - sources disagree and nothing settles it
  single     - only one source says it (a calendar site, one headline, one filing)
  estimate   - a computed date (FDA filing decision ~day 60-74, a lock-up from the listing date, an unconfirmed earnings date)

The vault is plain Markdown for Obsidian: Calendar.md, Needs a second look.md, and one note per stock.
It goes to the "vault" branch (normal commits, so the Obsidian Git plugin can pull it).
"""
import datetime as dt
import json
import os
import re
import shutil
from zoneinfo import ZoneInfo

import setup_stats

CT = ZoneInfo("America/Chicago")
OUT_DIR = os.environ.get("OUT_DIR", "out")
VAULT = os.environ.get("VAULT_DIR", "vault_out")
DAYS = 70   # 10 weeks ahead (covers the December FDA dates)
BINARY = ("fda", "readout", "adcom", "acceptance")   # events that can make or break a stock
STATS = {}   # ticker -> move stats (setup_stats.py)
ICON = {"confirmed": "✅", "corrected": "🔧", "conflict": "⚠️", "single": "❓", "estimate": "📐"}
WORD = {"confirmed": "confirmed", "corrected": "corrected (sheet was wrong)", "conflict": "sources disagree", "single": "one source only", "estimate": "estimated date"}


def load(name, default=None):
    try:
        with open(os.path.join(OUT_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def d_(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def tickers(s):
    return [t for t in re.findall(r"[A-Z]{1,5}", str(s or "").upper()) if t not in ("N", "A", "NA")]


def kind_of(text):
    t = text.lower()
    if re.search(r"accept", t) and "est" in t:
        return "acceptance"
    if re.search(r"pdufa|fda decision|fda action|approval decision", t):
        return "fda"
    if re.search(r"adcom|advisory committee", t):
        return "adcom"
    if re.search(r"earning", t):
        return "earnings"
    return "readout"


def main():
    today = dt.datetime.now(CT).date()
    horizon = today + dt.timedelta(days=DAYS)
    cats = load("catalysts.json")
    pdufa = (load("pdufa.json").get("items") or [])
    pcache = load("pdufa_cache.json")
    guide = (load("guidance.json").get("items") or [])
    locks = (load("lockups.json").get("items") or [])
    earn = load("earnings.json")
    prof = (load("profiles.json").get("items") or {})

    # how many separate filings state each (ticker, FDA date)
    n_filings = {}
    for k, v in pcache.items():
        if k == "_v":
            continue
        for d, _ in v.get("dates") or []:
            n_filings.setdefault((v["ticker"], d), set()).add(k.split(":")[0])
    news_fda = {}
    for r in cats.get("discovered") or []:
        if r.get("type") == "PDUFA" and r.get("source") != "SEC filing" and r.get("ticker"):
            news_fda.setdefault(r["ticker"], []).append(r)

    ev = []

    def add(t, company, kind, start, end, title, status, sources, note=""):
        if not t or not start or start > horizon or (end or start) < today:
            return
        ev.append({"ticker": t, "company": company, "kind": kind, "date": start.isoformat(), "end": (end or start).isoformat(),
                   "title": title, "status": status, "sources": sources, "note": note,
                   "bio": bool(re.search(r"Biotech|Health", (prof.get(t) or {}).get("g", ""))), "cap_m": (prof.get(t) or {}).get("c")})

    # 1) FDA decision dates: the companies' filings, checked against the sheet and news
    sheet_fda = {}
    for c in cats.get("catalysts") or []:
        if kind_of(c["type"] + " " + c["catalyst"]) == "fda" and c.get("sort"):
            for t in tickers(c["ticker"])[:1]:
                sheet_fda.setdefault(t, []).append(c)
    seen_fda = set()
    for i in pdufa:
        if i.get("decided"):
            continue
        t, d = i["ticker"], d_(i["date"])
        n = len(n_filings.get((t, i["date"]), ())) or 1
        src = [{"name": f"{t} SEC {i['form']} ({i['filed']})" + (f" + {n - 1} more filing{'s' * (n > 2)}" if n > 1 else ""), "link": i["link"], "quote": i["text"][:260]}]
        sh = [c for c in sheet_fda.get(t, []) if c.get("verified") in ("SEC", "moved") and abs((d_(c["sort"]) - d).days) <= 3]
        nw = [r for r in news_fda.get(t, []) if d_(r["date"]) and abs((d_(r["date"]) - d).days) <= 3]
        if sh:
            src.append({"name": "your sheet", "link": ""})
        for r in nw[:1]:
            src.append({"name": r.get("source") or "news", "link": r.get("link") or "", "quote": r.get("headline", "")[:200]})
        moved = [c for c in sh if c.get("verified") == "moved"]
        status = "corrected" if moved else "confirmed" if (n >= 2 or sh or nw) else "single"
        drugs = ", ".join(i.get("drugs") or [])
        add(t, i["company"], "fda", d, None, "FDA decision" + (f" ({drugs})" if drugs else ""), status, src,
            (moved[0]["notes"].split(". ")[0] + ".") if moved else ("Only one filing states this date." if status == "single" else ""))
        seen_fda.add((t, i["date"]))
    # sheet FDA dates no filing backs up
    for t, rows in sheet_fda.items():
        for c in rows:
            d = d_(c["sort"])
            if c.get("status", "").lower().startswith("resolved") or any((t, x) in seen_fda for x in [(d + dt.timedelta(days=k)).isoformat() for k in range(-3, 4)]):
                continue
            mine = [i for i in pdufa if i["ticker"] == t and not i.get("decided")]
            add(t, c["company"], "fda", d, None, c["catalyst"] or "FDA decision", "conflict" if mine else "single",
                [{"name": "your sheet", "link": ""}],
                (f"Company filings give {', '.join(i['date'] for i in mine)} instead." if mine else "Not found in the company's SEC filings; came from a calendar. Treat as unconfirmed."))
    # news-only FDA dates (no filing for that company)
    for t, rows in news_fda.items():
        if any(i["ticker"] == t for i in pdufa) or t in sheet_fda:
            continue
        for r in rows[:1]:
            add(t, r.get("company") or "", "fda", d_(r["date"]), None, "FDA decision (from news)", "single",
                [{"name": r.get("source") or "news", "link": r.get("link") or "", "quote": r.get("headline", "")[:200]}], "One headline only; not in SEC filings.")

    # 2) readouts / data events: sheet vs the company's own guidance (SEC) vs company announcements (news)
    g_by = {}
    for g in guide:
        g_by.setdefault(g["ticker"], []).append(g)
    for c in cats.get("catalysts") or []:
        k = kind_of(c["type"] + " " + c["catalyst"])
        if k in ("fda", "earnings") or not c.get("sort"):
            continue
        t = (tickers(c["ticker"]) or [""])[0]
        s, e = d_(c["sort"]), d_(c.get("end")) or d_(c["sort"])
        gs = [g for g in g_by.get(t, []) if d_(g["start"]) <= e + dt.timedelta(days=7) and d_(g["end"]) >= s - dt.timedelta(days=7)]
        src = [{"name": "your sheet", "link": ""}] + [{"name": f"{t} SEC {g['form']} ({g['filed']}): {g['when']}", "link": g["link"], "quote": g["text"][:220]} for g in gs[:2]]
        add(t, c["company"], k, s, e, c["catalyst"], "confirmed" if gs else "single", src, "" if gs else "Only on your sheet; not in the company's filings.")
    for r in cats.get("discovered") or []:
        if r.get("type") in ("PDUFA",) or not r.get("ticker"):
            continue
        k = "acceptance" if "accept" in (r.get("type") or "").lower() else "adcom" if r.get("type") == "AdComm" else "readout"
        d = d_(r.get("date"))
        # a company press release naming the day ("to report data on Oct 26") is the company's own word
        own = bool(re.search(r"\b(to|will) (announce|report|present|host|share|unveil)\b", r.get("headline", ""), re.I))
        add(r["ticker"], r.get("company") or "", k, d, None, r.get("headline", "")[:140],
            "estimate" if k == "acceptance" else "confirmed" if own else "single",
            [{"name": r.get("source") or "news", "link": r.get("link") or "", "quote": r.get("headline", "")[:200]}],
            "FDA usually says whether it accepts a filing ~60-74 days after submission; companies announce it around then." if k == "acceptance" else "")
    # guidance windows open in the next 8 weeks (the company says "in Q4" / "by end of October")
    sheet_t = {t for c in cats.get("catalysts") or [] for t in tickers(c["ticker"])}
    for g in guide:
        s, e = d_(g["start"]), d_(g["end"])
        if not s or not e or (e - s).days > 100 or g["ticker"] in sheet_t:
            continue
        if re.search(r"fda decision", g["kind"], re.I):
            continue   # exact FDA dates come from pdufa.json
        add(g["ticker"], g["company"], "acceptance" if "accept" in g["kind"].lower() else "readout", max(s, today), e,
            f"{g['kind']} expected {g['when']}", "estimate" if "accept" in g["kind"].lower() else "confirmed",
            [{"name": f"{g['ticker']} SEC {g['form']} ({g['filed']})", "link": g["link"], "quote": g["text"][:220]}],
            "A window the company gave, not an exact day.")

    # 3) lock-ups and drug-company earnings
    for x in locks:
        d = d_(x.get("unlock"))
        add(x["symbol"], x.get("name") or "", "lockup", d, None, f"{x.get('kind') or 'IPO'} lock-up ends",
            "estimate" if x.get("estimated") else "confirmed", [{"name": "SEC listing date + 180 days", "link": ""}], "")
    for r in earn.get("rows") or []:
        t, d = r[0], d_(r[1])
        if (prof.get(t) or {}).get("g", "").startswith("Biotech"):
            add(t, (prof.get(t) or {}).get("n", ""), "earnings", d, None, "Earnings" + (f" ({ {'bmo': 'before open', 'amc': 'after close'}.get(r[2], r[2])})" if r[2] else ""),
                "single", [{"name": "Finnhub earnings calendar", "link": ""}], "")

    # one event per (ticker, kind, date)
    uniq = {}
    rank = {"corrected": 0, "confirmed": 1, "conflict": 2, "single": 3, "estimate": 4}
    for e in ev:
        k = (e["ticker"], e["kind"], e["date"])
        if k not in uniq or rank[e["status"]] < rank[uniq[k]["status"]]:
            uniq[k] = e
    ev = sorted(uniq.values(), key=lambda e: (e["date"], e["ticker"]))
    prev = load("verified.json").get("events") or []   # last run's list, for the "What changed" note
    with open(os.path.join(OUT_DIR, "verified.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "events": ev}, f, separators=(",", ":"), ensure_ascii=False)
    counts = {s: sum(e["status"] == s for e in ev) for s in ICON}
    print(f"Wrote verified.json: {len(ev)} events in the next {DAYS} days {counts}")
    soon = (today + dt.timedelta(days=21)).isoformat()
    binaries = [e for e in ev if e["kind"] in BINARY and (e["kind"] == "fda" or e["end"] <= soon)]
    try:
        STATS.update(setup_stats.update(binaries, prof))
    except Exception as ex:  # noqa: BLE001  (never let the move stats break the vault)
        print(f"[warn] move stats failed: {ex}")
    write_vault(ev, today)
    write_changes(prev, ev, today)


def write_changes(prev, ev, today):
    """What changed.md: new catalysts, moved dates, status changes and ones that dropped off, newest first (2 weeks kept)."""
    if not prev:
        return
    key = lambda e: (e["ticker"], e["kind"], re.sub(r"\W+", " ", e["title"].lower())[:40])   # noqa: E731
    old, new = {key(e): e for e in prev}, {key(e): e for e in ev}
    lines = []
    for k, e in new.items():
        if e["kind"] in ("earnings",):
            continue
        o = old.get(k)
        if not o:
            lines.append(f"🆕 New: {md_line(e)[2:]}")
        elif o["date"] != e["date"] and e["end"] == e["date"] and o["end"] == o["date"]:
            lines.append(f"📆 Date moved {d_(o['date']):%b} {d_(o['date']).day} → {md_line(e)[2:]}")
        elif o["status"] != e["status"]:
            lines.append(f"{ICON[e['status']]} Now {WORD[e['status']]} (was {WORD[o['status']]}): {md_line(e)[2:]}")
    for k, o in old.items():
        if k not in new and o["kind"] != "earnings" and o["end"] >= today.isoformat():
            lines.append(f"✔️ Dropped off (happened, decided or no longer in any source): {md_line(o)[2:]}")
    path = os.path.join(VAULT, "What changed.md")
    try:
        body = open(path, encoding="utf-8").read().split("\n")[4:]
    except OSError:
        body = []
    cutoff = (today - dt.timedelta(days=14)).isoformat()
    # drop sections older than 2 weeks (a section runs from its "## date" header to the next one)
    kept, keep = [], True
    for b in body:
        if b.startswith("## "):
            keep = b[3:13] >= cutoff
        if keep:
            kept.append(b)
    if lines:
        kept = [f"## {dt.datetime.now(CT):%Y-%m-%d %I:%M %p} CT", ""] + [f"- {x}" for x in lines[:150]] + [""] + kept
    head = ["# What changed", "", "New catalysts, moved dates and ones that dropped off, newest first. Kept for 2 weeks.", ""]
    open(path, "w", encoding="utf-8").write("\n".join(head + kept).rstrip() + "\n")
    print(f"What changed: {len(lines)} changes this run")


def md_line(e, link_ticker=True):
    d = d_(e["date"])
    if e["end"] != e["date"]:
        when = f"now–{d_(e['end']):%b} {d_(e['end']).day}" if d <= dt.datetime.now(CT).date() else f"{d:%b} {d.day}–{d_(e['end']):%b} {d_(e['end']).day}"
    else:
        when = f"{d:%a %b} {d.day}"
    t = f"[[{e['ticker']}]]" if link_ticker else e["ticker"]
    out = f"- {ICON[e['status']]} **{when}** · {t} · {e['title']}" + (f" — _{e['note']}_" if e["note"] else "")
    st = setup_stats.line(STATS.get(e["ticker"])) if e["kind"] in BINARY else ""
    return out + (f"\n    - 📏 {st}" if st else "")


def write_vault(ev, today):
    if os.path.isdir(os.path.join(VAULT, "Stocks")):
        shutil.rmtree(os.path.join(VAULT, "Stocks"))
    os.makedirs(os.path.join(VAULT, "Stocks"), exist_ok=True)
    stamp = dt.datetime.now(CT).strftime("%a %b %d, %I:%M %p CT")
    legend = " · ".join(f"{ICON[s]} {WORD[s]}" for s in ICON)
    main_ev = [e for e in ev if e["kind"] != "earnings" or e["bio"]]
    # Calendar
    lines = [f"# Catalyst calendar (next 10 weeks)", "", f"Updated {stamp}. {legend}", ""]
    wk = None
    is_window = lambda e: (d_(e["end"]) - d_(e["date"])).days > 10   # noqa: E731  ("in Q4", "by year-end")
    for e in main_ev:
        if e["kind"] == "earnings" or is_window(e):
            continue
        d = d_(e["date"])
        w = d - dt.timedelta(days=d.weekday())
        if w != wk:
            wk = w
            lines += ["", f"## Week of {w:%b} {w.day}"]
        lines.append(md_line(e))
    wins = sorted((e for e in main_ev if e["kind"] != "earnings" and is_window(e)), key=lambda e: (e["end"], e["ticker"]))
    lines += ["", "## Open windows: could land any day", "", "The company gave a window, not a day. Sorted by when the window closes.", ""] + [md_line(e) for e in wins]
    lines += ["", "## Drug-company earnings", ""] + [md_line(e) for e in main_ev if e["kind"] == "earnings"][:80]
    open(os.path.join(VAULT, "Calendar.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    # Today: today, tomorrow, the rest of this week, and windows closing within a week
    tmr = today + dt.timedelta(days=1)
    wk_end = today + dt.timedelta(days=6 - today.weekday())
    def sect(title, es):
        """Catalysts first; that day's drug-company earnings on one line underneath."""
        cat = [e for e in es if e["kind"] != "earnings"]
        earn = [e for e in es if e["kind"] == "earnings"]
        if not es:
            return []
        out = [f"## {title}", ""] + [md_line(e) for e in cat]
        if earn:
            out.append("- 📊 Earnings: " + ", ".join(f"[[{e['ticker']}]]" + (" (pre)" if "before" in e["title"] else " (post)" if "after" in e["title"] else "") for e in earn))
        return out + [""]
    exact = [e for e in main_ev if e["end"] == e["date"]]
    closing = sorted((e for e in main_ev if e["end"] != e["date"] and e["kind"] != "earnings" and d_(e["end"]) <= today + dt.timedelta(days=7)), key=lambda e: e["end"])
    lines = [f"# Today · {today:%A, %B} {today.day}", "", f"Updated {stamp}. {legend}", ""]
    lines += sect("Today", [e for e in exact if e["date"] == today.isoformat()])
    lines += sect(f"Tomorrow · {tmr:%a %b} {tmr.day}", [e for e in exact if e["date"] == tmr.isoformat()])
    lines += sect("Rest of this week", [e for e in exact if tmr.isoformat() < e["date"] <= wk_end.isoformat()])
    lines += sect("Windows closing within a week (could land any day)", closing)
    lines += ["See also: [[Calendar]] · [[What changed]] · [[Needs a second look]]"]
    open(os.path.join(VAULT, "Today.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    # Needs a second look
    look = [e for e in ev if e["status"] in ("conflict", "single", "corrected") and e["kind"] in ("fda", "readout", "adcom")]
    lines = ["# Needs a second look", "", f"Updated {stamp}. Dates where sources disagree, only one source says it, or the sheet was corrected.", ""]
    for s in ("corrected", "conflict", "single"):
        sub = [e for e in look if e["status"] == s]
        if sub:
            lines += ["", f"## {ICON[s]} {WORD[s].capitalize()} ({len(sub)})"] + [md_line(e) for e in sub]
    open(os.path.join(VAULT, "Needs a second look.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    # one note per stock
    by_t = {}
    for e in ev:
        by_t.setdefault(e["ticker"], []).append(e)
    for t, es in by_t.items():
        if not re.fullmatch(r"[A-Z]{1,5}", t):
            continue
        co = next((e["company"] for e in es if e["company"]), "")
        out = ["---", f"ticker: {t}", f"company: \"{co.replace(chr(34), '')}\"", "tags: [catalyst" + (", biotech" if es[0]["bio"] else "") + "]", "---", "",
               f"# {t} · {co}", "", f"[Open on Binary Days](https://golden-fox-642e38.netlify.app/stock.html?s={t})", ""]
        for e in es:
            out.append(md_line(e, link_ticker=False))
            for s in e["sources"]:
                out.append(f"    - Source: " + (f"[{s['name']}]({s['link']})" if s.get("link") else s["name"]) + (f" — \"{s['quote']}\"" if s.get("quote") else ""))
        out += ["", "## My notes", "", "_Put your own notes in a separate note (e.g. `My notes/{t}.md`); this one is rewritten every 30 minutes._".replace("{t}", t)]
        open(os.path.join(VAULT, "Stocks", f"{t}.md"), "w", encoding="utf-8").write("\n".join(out) + "\n")
    readme = f"""# Binary Days vault

Rebuilt every 30 minutes from the Binary Days data (SEC filings, news, your spreadsheet, earnings and lock-up calendars).
Last update: {stamp}.

- [[Today]]: today, tomorrow and the rest of this week on one page
- [[What changed]]: new catalysts, moved dates, ones that dropped off (2 weeks of history)
- [[Calendar]]: every catalyst in the next 10 weeks, by week
- [[Needs a second look]]: dates that are corrected, disputed, or rest on one source
- `Stocks/`: one note per company, with every source and a quote from it

📏 under a catalyst = how much it can move the stock: market cap (MC), the options-implied move to the first expiry
after the event, 30-day volatility (annualized), the usual daily move (last 60 days) and the biggest one-day moves of the past year.

{legend}

**Your own notes:** keep them in a `My notes` folder. Everything else here is overwritten on each update.
"""
    open(os.path.join(VAULT, "README.md"), "w", encoding="utf-8").write(readme)
    print(f"Wrote the vault: {len(by_t)} stock notes, {len(look)} items to double-check")


if __name__ == "__main__":
    main()
