"""Shared helpers for the site's tool lists (squeeze, dilution, options flow, track record)."""
import datetime as dt
import json
import os
import re

import requests

from build_guidance import CT, UA as SEC_UA, log  # noqa: F401

OUT_DIR = os.environ.get("OUT_DIR", "out")
API = "https://binary-days-api.binary-days-api.workers.dev"
YF_UA = {"User-Agent": "Mozilla/5.0 (binary-days data builder)"}
MIN_PRICE = 3.0
THREADS = 3   # 8 at a time got us refused by Yahoo after about 700 calls


def load(name, default=None):
    try:
        with open(os.path.join(OUT_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def save(name, obj):
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


def now_ct():
    return dt.datetime.now(CT)


def stamp():
    return now_ct().isoformat(timespec="minutes")


def api(path, **params):
    try:
        return requests.get(API + path, params=params, timeout=40).json()
    except Exception:  # noqa: BLE001
        return {}


def chart(t, rng="3mo", interval="1d"):
    """Daily bars [(iso day, open, close, volume)] plus Yahoo's meta; None if Yahoo didn't answer."""
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}", params={"range": rng, "interval": interval}, headers=YF_UA, timeout=25)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
    except Exception:  # noqa: BLE001
        return None
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        if q["close"][i] is not None and q["open"][i] is not None:
            bars.append((dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat(), q["open"][i], q["close"][i], q["volume"][i] or 0))
    return {"bars": bars, "meta": res.get("meta") or {}}


def ticker_of(display_name):
    m = re.search(r"\(([A-Z]{1,5})(?:[,)])", display_name or "")
    return m.group(1) if m else None


def company_of(display_name):
    return re.sub(r"\s+\(.*$", "", display_name or "").strip()


def upcoming_catalysts(days=45):
    """{ticker: (date, what)} soonest binary event per stock, from the cross-checked list (verified.json)."""
    today = now_ct().date().isoformat()
    last = (now_ct().date() + dt.timedelta(days=days)).isoformat()
    out = {}
    for e in load("verified.json").get("events") or []:
        t, d = e.get("ticker"), e.get("date") or ""
        if not t or e.get("kind") not in ("fda", "readout", "adcom", "acceptance"):
            continue
        end = e.get("end") or d
        if end < today or d > last:
            continue
        day = max(d, today)
        if t not in out or day < out[t][0]:
            out[t] = (day, (e.get("title") or "")[:120])
    return out
