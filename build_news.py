#!/usr/bin/env python3
"""
Builds news.json for the website's News page: recent headlines from publisher RSS feeds,
newest first, tagged "biotech" or "markets", with headlines several outlets share grouped together.

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

FEEDS = [
    ("FDA", "biotech", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),
    ("Fierce Biotech", "biotech", "https://www.fiercebiotech.com/rss/xml"),
    ("Fierce Pharma", "biotech", "https://www.fiercepharma.com/rss/xml"),
    ("STAT", "biotech", "https://www.statnews.com/feed/"),
    ("Endpoints", "biotech", "https://endpts.com/feed/"),
    ("BioPharma Dive", "biotech", "https://www.biopharmadive.com/feeds/news/"),
    ("BioSpace", "biotech", "https://www.biospace.com/rss/news"),
    ("CNBC Health", "biotech", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000108"),
    ("CNBC", "markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),
    ("CNBC Markets", "markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("MarketWatch", "markets", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("WSJ Markets", "markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"),
    ("Yahoo Finance", "markets", "https://finance.yahoo.com/news/rssindex"),
    ("Bloomberg", "markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("NYT Business", "markets", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
    ("BBC Business", "markets", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("Federal Reserve", "markets", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("SEC", "markets", "https://www.sec.gov/news/pressreleases.rss"),
]
STOP = set("a an the of to in on for and or but with at by from as is are was were be been has have had it its this that "
           "after before over under into about amid says say said new more than up down out will would could may can how why what "
           "who when your you our their they them we not".split())


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
        title = link = pub = ""
        for ch in node:
            t = ch.tag.split("}")[-1]
            if t == "title":
                title = "".join(ch.itertext()).strip()
            elif t == "link" and not link:
                link = (ch.text or "").strip() or ch.attrib.get("href", "")
            elif t in ("pubDate", "published", "updated", "date") and not pub:
                pub = (ch.text or "").strip()
        d = when(pub)
        if title and link and d:
            items.append({"title": re.sub(r"\s+", " ", html.unescape(title)), "link": link, "source": name, "section": section, "t": d})
    return name, items, None


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9$%]+", title.lower()) if len(w) > 2 and w not in STOP}


def main():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=HOURS)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, FEEDS))
    items, failed = [], []
    for name, its, err in results:
        if err:
            failed.append(f"{name} ({err})")
        items.extend(i for i in its if i["t"] >= cutoff)
    items.sort(key=lambda i: i["t"], reverse=True)

    # group the same story from different outlets; the newest headline leads
    groups = []
    for it in items:
        tk = tokens(it["title"])
        for g in groups:
            inter = len(tk & g["tk"])
            if inter >= 3 and inter / max(1, min(len(tk), len(g["tk"]))) >= 0.5:
                if it["source"] not in g["sources"]:
                    g["also"].append({"source": it["source"], "link": it["link"]})
                    g["sources"].add(it["source"])
                break
        else:
            groups.append({"lead": it, "tk": tk, "sources": {it["source"]}, "also": []})

    out = [{
        "title": g["lead"]["title"], "link": g["lead"]["link"], "source": g["lead"]["source"],
        "section": g["lead"]["section"], "time": g["lead"]["t"].astimezone(CT).isoformat(timespec="minutes"),
        "also": g["also"][:5],
    } for g in groups][:250]

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "news.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.now(CT).isoformat(timespec="minutes"), "items": out,
                   "feeds_ok": len(FEEDS) - len(failed), "feeds_total": len(FEEDS), "failed": failed},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(f"News: {len(out)} stories from {len(FEEDS) - len(failed)}/{len(FEEDS)} feeds. Failed: {', '.join(failed) or 'none'}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[error] {type(e).__name__}: {e}")
        sys.exit(1)
