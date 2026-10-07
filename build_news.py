#!/usr/bin/env python3
"""
Builds news.json for the website's News page: recent headlines from publisher RSS feeds, newest first,
with photos where the feed has them, a category (markets, earnings, economy, biotech, tech, deals, crypto),
the tickers each story mentions, and stories several outlets share grouped together.

Usage:  python build_news.py      Env: OUT_DIR
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

CT = ZoneInfo("America/Chicago")
OUT_DIR = os.environ.get("OUT_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
HOURS = 72
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# (source, default category, url)
FEEDS = [
    ("Fierce Biotech", "biotech", "https://www.fiercebiotech.com/rss/xml"),
    ("Fierce Pharma", "biotech", "https://www.fiercepharma.com/rss/xml"),
    ("STAT", "biotech", "https://www.statnews.com/feed/"),
    ("Endpoints", "biotech", "https://endpts.com/feed/"),
    ("BioPharma Dive", "biotech", "https://www.biopharmadive.com/feeds/news/"),
    ("CNBC Health", "biotech", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000108"),
    ("CNBC", "markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Markets", "markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("CNBC Earnings", "earnings", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135"),
    ("CNBC Economy", "economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("CNBC Tech", "tech", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910"),
    ("CNBC Finance", "markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ("MarketWatch", "markets", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch Pulse", "markets", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("WSJ Markets", "markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("WSJ Business", "markets", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness"),
    ("WSJ Tech", "tech", "https://feeds.content.dowjones.io/public/rss/RSSWSJD"),
    ("Yahoo Finance", "markets", "https://finance.yahoo.com/news/rssindex"),
    ("Bloomberg", "markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Bloomberg Economics", "economy", "https://feeds.bloomberg.com/economics/news.rss"),
    ("Bloomberg Tech", "tech", "https://feeds.bloomberg.com/technology/news.rss"),
    ("Seeking Alpha", "markets", "https://seekingalpha.com/market_currents.xml"),
    ("Investing.com", "markets", "https://www.investing.com/rss/news.rss"),
    ("Nasdaq", "markets", "https://www.nasdaq.com/feed/rssoutbound?category=Stocks"),
    ("NYT Business", "markets", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("BBC Business", "economy", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Federal Reserve", "economy", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("SEC", "markets", "https://www.sec.gov/news/pressreleases.rss"),
    ("CoinDesk", "crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]
CATEGORY_RULES = [  # first match wins; overrides the feed's default category
    ("deals", r"\b(acquir\w*|acquisition|merger|merge|buyout|takeover|to buy\b|deal to|bid for|tender offer|go[- ]private|stake in)\b"),
    ("biotech", r"\b(FDA|PDUFA|biotech\w*|pharma\w*|drug\w*|vaccine|trial|phase (1|2|3|i|ii|iii)\b|oncology|cancer|obesity|GLP-1|Alzheimer|clinical|therap\w+)\b"),
    ("earnings", r"\b(earnings|quarterly (results|profit|revenue)|Q[1-4] (results|earnings|revenue)|EPS|beats estimates|misses estimates|guidance|outlook raised|cuts outlook)\b"),
    ("economy", r"\b(Fed\b|Federal Reserve|Powell|inflation|CPI|PCE|jobs report|payrolls|unemployment|GDP|recession|Treasury yield|interest rate|rate cut|rate hike|tariff\w*|economy)\b"),
    ("crypto", r"\b(bitcoin|crypto\w*|ethereum|stablecoin|blockchain|Coinbase)\b"),
    ("tech", r"\b(AI\b|artificial intelligence|Nvidia|chip\w*|semiconductor\w*|Apple|Microsoft|Alphabet|Google|Meta|Amazon|OpenAI|Anthropic|software|cloud|data center\w*)\b"),
]
STOP = set("a an the of to in on for and or but with at by from as is are was were be been has have had it its this that "
           "after before over under into about amid says say said new more than up down out will would could may can how why what "
           "who when your you our their they them we not".split())
TICKER_RES = [re.compile(r"\((?:NASDAQ|NYSE|NYSE American|AMEX|OTC|Nasdaq|NYSEARCA|CBOE)\s*:\s*([A-Z]{1,5})\)"), re.compile(r"\$([A-Z]{1,5})\b")]


def when(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def first_image(node, desc_html):
    for ch in node.iter():
        t = ch.tag.split("}")[-1]
        url = ch.attrib.get("url") or ch.attrib.get("href") or ""
        if t in ("content", "thumbnail") and url and (ch.attrib.get("medium") in (None, "image") or re.search(r"\.(jpe?g|png|webp)", url, re.I)):
            return url
        if t == "enclosure" and url and (ch.attrib.get("type", "").startswith("image") or re.search(r"\.(jpe?g|png|webp)", url, re.I)):
            return url
    m = re.search(r'<img[^>]+src="([^"]+)"', desc_html or "")
    return html.unescape(m.group(1)) if m else ""


def fetch(feed):
    name, section, url = feed
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        return name, [], type(e).__name__
    items = []
    for node in root.iter():
        if node.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = link = pub = desc = ""
        for ch in node:
            t = ch.tag.split("}")[-1]
            if t == "title":
                title = "".join(ch.itertext()).strip()
            elif t == "link" and not link:
                link = (ch.text or "").strip() or ch.attrib.get("href", "")
            elif t in ("pubDate", "published", "updated", "date") and not pub:
                pub = (ch.text or "").strip()
            elif t in ("description", "summary") and not desc:
                desc = "".join(ch.itertext())
        d = when(pub)
        if not (title and link and d):
            continue
        title = re.sub(r"\s+", " ", html.unescape(title))
        summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", desc or ""))).strip()[:260]
        items.append({"title": title, "link": link, "source": name, "section": section, "t": d,
                      "image": first_image(node, desc), "summary": summary if summary.lower() != title.lower() else ""})
    return name, items, None


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9$%]+", title.lower()) if len(w) > 2 and w not in STOP}


def categorize(it):
    text = it["title"] + " " + it["summary"]
    for cat, rx in CATEGORY_RULES:
        if re.search(rx, text, re.I):
            if cat == "tech" and it["section"] == "biotech":
                continue
            return cat
    return it["section"]


def tickers(text):
    found = []
    for rx in TICKER_RES:
        for m in rx.finditer(text):
            if m.group(1) not in found and m.group(1) not in ("USD", "AI", "CEO", "IPO", "ETF"):
                found.append(m.group(1))
    return found[:4]


def main():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=HOURS)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(fetch, FEEDS))
    items, failed = [], []
    for name, its, err in results:
        if err:
            failed.append(f"{name} ({err})")
        items.extend(i for i in its if i["t"] >= cutoff)
    items.sort(key=lambda i: i["t"], reverse=True)

    groups = []
    for it in items:
        tk = tokens(it["title"])
        for g in groups:
            inter = len(tk & g["tk"])
            if inter >= 3 and inter / max(1, min(len(tk), len(g["tk"]))) >= 0.5:
                if it["source"] not in g["sources"]:
                    g["also"].append({"source": it["source"], "link": it["link"]})
                    g["sources"].add(it["source"])
                if not g["lead"]["image"] and it["image"]:
                    g["lead"]["image"] = it["image"]
                break
        else:
            groups.append({"lead": dict(it), "tk": tk, "sources": {it["source"]}, "also": []})

    out = []
    for g in groups[:400]:
        lead = g["lead"]
        out.append({
            "title": lead["title"], "link": lead["link"], "source": lead["source"],
            "section": "biotech" if lead["section"] == "biotech" else "markets",   # kept for older site code
            "cat": categorize(lead), "image": lead["image"], "summary": lead["summary"],
            "tickers": tickers(lead["title"] + " " + lead["summary"]),
            "time": lead["t"].astimezone(CT).isoformat(timespec="minutes"),
            "also": g["also"][:6], "outlets": len(g["sources"]),
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "news.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": out,
                   "feeds_ok": len(FEEDS) - len(failed), "feeds_total": len(FEEDS), "failed": failed},
                  f, separators=(",", ":"), ensure_ascii=False)
    cats = {}
    for o in out:
        cats[o["cat"]] = cats.get(o["cat"], 0) + 1
    print(f"News: {len(out)} stories ({sum(1 for o in out if o['image'])} with photos) from {len(FEEDS) - len(failed)}/{len(FEEDS)} feeds. "
          f"Categories: {cats}. Failed: {', '.join(failed) or 'none'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {type(e).__name__}: {e}")
        sys.exit(1)
