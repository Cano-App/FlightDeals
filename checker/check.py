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
MAX_SEARCHES = 150  # per run, keeps us polite to Google and under the job timeout
HIST_LEN = 60
CABINS = {"economy", "premium-economy", "business", "first"}


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
    """Departure/return dates to try, shifting both by the flex window."""
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
    pairs = []
    for s in sorted(range(-flex, flex + 1), key=abs):
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


def notify(w, price, reason, best):
    route = f"{w['from']} → {w['to']}"
    cabin = w.get("cabin", "economy").replace("-", " ").title()
    title = f"✈️ ${price:,} {route} {cabin}"
    when = w["depart"] + (f" – {w['ret']}" if w.get("ret") else " one-way")
    body = f"{reason}\n{best.get('air', '')} · {best.get('date') or when}"
    for topic in (NTFY_TOPIC, f"{NTFY_TOPIC}-{w['_id']}"):
        try:
            http("POST", "https://ntfy.sh/", {
                "topic": topic, "title": title, "message": body, "priority": 4,
                "tags": ["moneybag"], "click": best.get("url") or APP_URL,
                "actions": [{"action": "view", "label": "Open FlightDeals", "url": APP_URL}],
            })
        except Exception as e:
            print("  ntfy failed:", e)


def check_watch(w, budget, now_ms, today):
    pairs = date_pairs(w, today)
    if not pairs:
        save(w["_id"], {"checked": now_ms, "err": "Dates have passed", "deal": ""})
        return 0
    if w.get("cabin") not in CABINS:
        w["cabin"] = "economy"
    best_overall, by_air, used, errors = None, {}, 0, []
    for dep, ret in pairs[:budget]:
        used += 1
        try:
            results, url = search(w, dep, ret)
        except Exception as e:
            errors.append(f"{dep}: {type(e).__name__}")
            print(f"  {dep} failed: {e!r}"[:300])
            continue
        best, air = summarize(results)
        for k, v in air.items():
            by_air[k] = min(by_air.get(k, v), v)
        if best and (best_overall is None or best.price < best_overall["price"]):
            leg = best.flights
            best_overall = {
                "price": best.price, "date": dep + (f" → {ret}" if ret else ""),
                "air": ", ".join(best.airlines[:3]), "stops": max(0, len(leg) - 1),
                "dur": sum(s.duration or 0 for s in leg),
                "dep": fmt_time(leg[0].departure) if leg else "",
                "arr": fmt_time(leg[-1].arrival) if leg else "", "url": url,
            }
        time.sleep(random.uniform(1.5, 3.5))

    fields = {"checked": now_ms}
    if best_overall is None:
        fields["err"] = ("Search failed: " + "; ".join(errors))[:200] if errors else "No flights found"
        fields["deal"] = ""
        save(w["_id"], fields)
        return used

    price = best_overall["price"]
    hist = (w.get("hist") or [])[-(HIST_LEN - 1):] + [{"t": now_ms, "p": price}]
    reason = deal_reason(price, w, hist)
    low = min([price] + [h["p"] for h in hist if h.get("p")])
    fields.update({
        "price": price, "low": low, "hist": hist, "best": best_overall,
        "byAir": dict(sorted(by_air.items(), key=lambda kv: kv[1])[:12]),
        "deal": reason, "err": "",
    })
    alerted = w.get("alerted") or 0
    if reason and (not alerted or price < alerted):
        notify(w, price, reason, best_overall)
        fields["alerted"] = price
    elif not reason:
        fields["alerted"] = 0
    save(w["_id"], fields)
    print(f"  ${price} {best_overall['air']} {reason}")
    return used


def demo():
    today = date.today()
    w = {"from": "JFK", "to": "CDG", "depart": (today + timedelta(days=45)).isoformat(),
         "ret": (today + timedelta(days=52)).isoformat(), "cabin": "business",
         "airlines": ["DL", "AF", "KL"], "stops": -1}
    for cabin in ("economy", "business"):
        w["cabin"] = cabin
        results, url = search(w, w["depart"], w["ret"])
        best, by_air = summarize(results)
        print(cabin, len(results), "results; best:", best and (best.price, best.airlines), by_air)
        print(" ", url)
        if not results:
            sys.exit("Demo search returned nothing")


def main():
    if "--demo" in sys.argv:
        return demo()
    now_ms = int(time.time() * 1000)
    today = datetime.now(timezone.utc).date()
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
        print(f"{w['from']}->{w['to']} {w.get('cabin')} {w.get('depart')}")
        try:
            budget -= check_watch(w, budget, now_ms, today)
        except urllib.error.HTTPError as e:
            print("  save failed:", e.code, e.read()[:300])


if __name__ == "__main__":
    main()
