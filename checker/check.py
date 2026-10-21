"""Flight price checker. Runs in GitHub Actions every 30 minutes.

Reads every watch from Firestore (flightwatches), looks up prices on Google
Flights (which lists Delta, Air France, KLM, United, Southwest, Thai, Qatar,
Emirates and most other airlines), writes the results back, and pushes an
ntfy.sh alert when a price is a deal.

  python checker/check.py          normal run
  python checker/check.py --demo   one sample search, no Firestore
"""

import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

PROJECT = "bananagrams-4b5f0"
API_KEY = "AIzaSyAZHx61X7Eh9rVL62N9juocoQRZbFPS4DU"
COLLECTION = "flightwatches"
FS = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
APP_URL = "https://cano-app.github.io/FlightDeals/"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "cano-flightdeals-x7k2")
NEWS = "flightnews"
MAX_SEARCHES = 160  # per run, keeps us polite to Google and under the job timeout
PER_RUN = 3         # dates refreshed per watch per run; the rest come from the cache
ANY_SLOTS = 13      # "anytime" watches cover a departure every 2 weeks for ~6 months
FRESH_MS = 36 * 3600e3  # cached prices older than this are dropped
HIST_LEN = 60
CABINS = {"economy", "premium-economy", "business", "first"}
# Secret Flying blocks robots, so it isn't here.
FEEDS = [("The Flight Deal", "https://www.theflightdeal.com/feed/"),
         ("Fly4free", "https://www.fly4free.com/feed/")]
NEWS_WORDS = ["Lagos", "Nigeria", "Houston", "Denver", "San Antonio", "Texas", "Colorado",
              "IAH", "Hobby"]


# ---------- Firestore REST (values <-> python) ----------

def to_fs(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [to_fs(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: to_fs(x) for k, x in v.items()}}}
    raise TypeError(type(v))


def from_fs(v):
    (kind, x), = v.items()
    if kind == "integerValue":
        return int(x)
    if kind == "doubleValue":
        return float(x)
    if kind == "arrayValue":
        return [from_fs(i) for i in x.get("values", [])]
    if kind == "mapValue":
        return {k: from_fs(i) for k, i in x.get("fields", {}).items()}
    if kind == "nullValue":
        return None
    return x  # string, boolean, timestamp


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def load_watches():
    out, token = [], ""
    while True:
        url = f"{FS}/{COLLECTION}?pageSize=300&key={API_KEY}"
        if token:
            url += "&pageToken=" + urllib.parse.quote(token)
        res = http("GET", url)
        for d in res.get("documents", []):
            w = {k: from_fs(v) for k, v in d.get("fields", {}).items()}
            w["_id"] = d["name"].rsplit("/", 1)[1]
            out.append(w)
        token = res.get("nextPageToken")
        if not token:
            return out


def save(watch_id, fields):
    mask = "&".join("updateMask.fieldPaths=" + k for k in fields)
    url = f"{FS}/{COLLECTION}/{watch_id}?key={API_KEY}&{mask}&currentDocument.exists=true"
    http("PATCH", url, {"fields": {k: to_fs(v) for k, v in fields.items()}})


# ---------- Google Flights ----------

def search(w, dep, ret):
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    airlines = w.get("airlines") or None
    legs = [FlightQuery(date=dep, from_airport=w["from"], to_airport=w["to"], airlines=airlines)]
    if ret:
        legs.append(FlightQuery(date=ret, from_airport=w["to"], to_airport=w["from"], airlines=airlines))
    stops = w.get("stops", -1)
    q = create_query(
        flights=legs,
        trip="round-trip" if ret else "one-way",
        seat=w.get("cabin", "economy"),
        passengers=Passengers(adults=max(1, min(9, int(w.get("adults", 1))))),
        max_stops=None if stops is None or stops < 0 else stops,
        language="en",
        currency="USD",
    )
    try:
        results = get_flights(q)
    except Exception as e:  # FlightsNotFound and parser surprises
        if "no flights" in str(e).lower():
            return [], q.url()
        raise
    return results, q.url()


def summarize(results):
    """Cheapest itinerary plus cheapest price per airline."""
    best, by_air = None, {}
    for f in results:
        if not f.price:
            continue
        name = ", ".join(f.airlines[:2]) or "?"
        by_air[name] = min(by_air.get(name, f.price), f.price)
        if best is None or f.price < best.price:
            best = f
    return best, by_air


def fmt_time(sd):
    y, m, d = sd.date
    h, mi = sd.time
    return f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}"


def date_pairs(w, today):
    """Every departure/return pair this watch covers, soonest first.

    Exact mode: the chosen dates shifted by the flex window.
    Anytime mode: a departure every 2 weeks for the next 6 months, with a
    trip length of `nights` (0 = one-way).
    """
    pairs = []
    if w.get("any"):
        nights = max(0, min(30, int(w.get("nights", 0))))
        for k in range(1, ANY_SLOTS + 1):
            d = today + timedelta(days=14 * k - 7)
            r = d + timedelta(days=nights) if nights else None
            pairs.append((d.isoformat(), r.isoformat() if r else ""))
        return pairs
    try:
        dep0 = date.fromisoformat(w["depart"])
    except (KeyError, ValueError):
        return []
    ret0 = None
    if w.get("ret"):
        try:
            ret0 = date.fromisoformat(w["ret"])
        except ValueError:
            ret0 = None
    flex = max(0, min(3, int(w.get("flex", 0))))
    for s in range(-flex, flex + 1):
        d = dep0 + timedelta(days=s)
        r = ret0 + timedelta(days=s) if ret0 else None
        if d > today:
            pairs.append((d.isoformat(), r.isoformat() if r else ""))
    return pairs


# ---------- deal logic ----------

def deal_reason(price, w, hist):
    target = w.get("target") or 0
    prev = [h["p"] for h in hist[:-1] if h.get("p")]
    if target and price <= target:
        return f"Under your ${target:,} target"
    if len(prev) >= 4:
        usual = statistics.median(prev[-30:])
        if price <= usual * 0.85:
            return f"{round(100 - price / usual * 100)}% below usual (${round(usual):,})"
    if len(prev) >= 2 and price < min(prev):
        return f"New lowest price (was ${min(prev):,})"
    return ""


def push(topics, title, body, click):
    for topic in topics:
        try:
            http("POST", "https://ntfy.sh/", {
                "topic": topic, "title": title, "message": body, "priority": 4,
                "tags": ["moneybag"], "click": click or APP_URL,
                "actions": [{"action": "view", "label": "Open FlightDeals", "url": APP_URL}],
            })
        except Exception as e:
            print("  ntfy failed:", e)


def notify(w, price, reason, best):
    route = f"{w['from']} → {w['to']}"
    cabin = w.get("cabin", "economy").replace("-", " ").title()
    body = f"{reason}\n{best.get('air', '')} · {best.get('date', '')}"
    push((NTFY_TOPIC, f"{NTFY_TOPIC}-{w['_id']}"), f"✈️ ${price:,} {route} {cabin}", body, best.get("url"))


def check_watch(w, budget, now_ms, today):
    """Refresh the stalest few dates of a watch, then rate the cheapest fresh one."""
    pairs = date_pairs(w, today)
    if not pairs:
        save(w["_id"], {"checked": now_ms, "err": "Dates have passed", "deal": ""})
        return 0
    if w.get("cabin") not in CABINS:
        w["cabin"] = "economy"
    keys = [f"{d}|{r}" for d, r in pairs]
    old = w.get("cal") or {}
    cal = {k: old[k] for k in keys if k in old and now_ms - old[k].get("t", 0) < FRESH_MS}
    stale = sorted(keys, key=lambda k: cal.get(k, {}).get("t", 0))[:min(PER_RUN, budget)]
    used, errors = 0, []
    for key in stale:
        dep, ret = key.split("|")
        used += 1
        try:
            results, url = search(w, dep, ret)
        except Exception as e:
            errors.append(f"{dep}: {type(e).__name__}")
            print(f"  {dep} failed: {e!r}"[:300])
            continue
        best, air = summarize(results)
        if best:
            leg = best.flights
            cal[key] = {"t": now_ms, "p": best.price, "air": ", ".join(best.airlines[:3]),
                        "stops": max(0, len(leg) - 1), "dur": sum(s.duration or 0 for s in leg),
                        "url": url, "by": dict(sorted(air.items(), key=lambda kv: kv[1])[:6])}
        else:
            cal.pop(key, None)
        time.sleep(random.uniform(1.0, 2.5))

    fields = {"checked": now_ms, "cal": cal}
    if not cal:
        fields["err"] = ("Search failed: " + "; ".join(errors))[:200] if errors else "No flights found"
        fields["deal"] = ""
        save(w["_id"], fields)
        return used

    key, c = min(cal.items(), key=lambda kv: kv[1]["p"])
    dep, ret = key.split("|")
    price = c["p"]
    best = {"price": price, "date": dep + (f" → {ret}" if ret else ""), "air": c["air"],
            "stops": c["stops"], "dur": c["dur"], "url": c["url"]}
    by_air = {}
    for e in cal.values():
        for k, v in (e.get("by") or {}).items():
            by_air[k] = min(by_air.get(k, v), v)
    hist = (w.get("hist") or [])[-(HIST_LEN - 1):] + [{"t": now_ms, "p": price}]
    reason = deal_reason(price, w, hist)
    fields.update({
        "price": price, "low": min(h["p"] for h in hist if h.get("p")), "hist": hist, "best": best,
        "byAir": dict(sorted(by_air.items(), key=lambda kv: kv[1])[:8]), "deal": reason, "err": "",
    })
    alerted = w.get("alerted") or 0
    if reason and (not alerted or price < alerted):
        notify(w, price, reason, best)
        fields["alerted"] = price
        fields["dealAt"] = now_ms
    elif not reason:
        fields["alerted"] = 0
    save(w["_id"], fields)
    print(f"  ${price} {best['air']} {best['date']} {reason}")
    return used


# ---------- deal-site feeds (mistake fares, flash sales) ----------

def check_news(now_ms):
    import hashlib
    import re
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    words = re.compile(r"\b(" + "|".join(NEWS_WORDS) + r")\b", re.I)
    for src, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FlightDeals"})
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            print(f"feed {src} failed: {e!r}"[:200])
            continue
        items = root.findall(".//item")
        print(f"feed {src}: {len(items)} items")
        for it in items[:40]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            hits = sorted({m.lower() for m in words.findall(title)})
            if not hits or not link.startswith("http"):
                continue
            try:
                t = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp() * 1000)
            except Exception:
                t = now_ms
            doc_id = hashlib.sha1(link.encode()).hexdigest()[:20]
            try:
                http("POST", f"{FS}/{NEWS}?documentId={doc_id}&key={API_KEY}", {"fields": {
                    "title": to_fs(title[:300]), "link": to_fs(link[:500]), "src": to_fs(src),
                    "t": to_fs(t), "tags": to_fs(hits[:8])}})
            except urllib.error.HTTPError as e:
                if e.code != 409:  # 409 = already saved earlier
                    print("  news save failed:", e.code)
                continue
            print("  news:", title[:90])
            if now_ms - t < 2 * 86400e3:
                push((NTFY_TOPIC,), f"🔥 {src}: {title[:80]}", title, link)


def demo():
    today = date.today()
    tests = [("JFK", "CDG", "business", 7), ("LOS", "IAH", "economy", 14), ("SAT", "BKK", "business", 14),
             ("DEN", "LHR", "economy", 10), ("IAH", "CDG", "economy", 10)]
    for frm, to, cabin, nights in tests:
        w = {"from": frm, "to": to, "cabin": cabin, "airlines": [], "stops": -1}
        dep = (today + timedelta(days=45)).isoformat()
        ret = (today + timedelta(days=45 + nights)).isoformat()
        try:
            results, url = search(w, dep, ret)
            best, by_air = summarize(results)
            print(frm, to, cabin, len(results), "results; best:", best and (best.price, best.airlines), by_air)
        except Exception as e:
            print(frm, to, cabin, "FAILED", repr(e)[:200])
    for src, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FlightDeals"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
            print("feed", src, len(body), "bytes,", body.count(b"<item"), "items")
        except Exception as e:
            print("feed", src, "FAILED", repr(e)[:200])


def main():
    if "--demo" in sys.argv:
        return demo()
    now_ms = int(time.time() * 1000)
    today = datetime.now(timezone.utc).date()
    try:
        check_news(now_ms)
    except Exception as e:
        print("news failed:", repr(e)[:300])
    watches = load_watches()
    # Least recently checked first, so a big list still rotates fairly.
    watches.sort(key=lambda w: w.get("checked") or 0)
    print(f"{len(watches)} watches")
    budget = MAX_SEARCHES
    for w in watches:
        if budget <= 0:
            break
        if not all(isinstance(w.get(k), str) and len(w[k]) == 3 for k in ("from", "to")):
            continue
        print(f"{w['from']}->{w['to']} {w.get('cabin')} {'anytime' if w.get('any') else w.get('depart')}")
        try:
            budget -= check_watch(w, budget, now_ms, today)
        except urllib.error.HTTPError as e:
            print("  save failed:", e.code, e.read()[:300])


if __name__ == "__main__":
    main()
