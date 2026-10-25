"""Polymarket client for the Israeli election markets.

Uses only Polymarket's public, keyless APIs:
  - Gamma (https://gamma-api.polymarket.com) for events and markets
  - CLOB  (https://clob.polymarket.com) for price history

Run directly to dump a snapshot to JSON:
  python3 polymarket.py --out data/snapshot.json [--closed] [--history 1w]
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TAG = "israel-election"
PAGE = 100

# interval -> fidelity in minutes, so each range returns a few hundred points
INTERVALS = {"1d": 5, "1w": 60, "1m": 240, "max": 720}

BUCKET_RE = re.compile(r"^\s*[<>≤≥]?\s*\d+(\.\d+)?\s*%?\s*([-–+]\s*(\d+(\.\d+)?)?\s*%?)?\s*$")


def _get(url, params=None, retries=3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "israel-election-dashboard/1.0", "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(value):
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except ValueError:
        return []


def fetch_raw_events(include_closed=False):
    """Every event tagged israel-election, paging until the API runs dry."""
    states = ["false", "true"] if include_closed else ["false"]
    events = {}
    for closed in states:
        offset = 0
        while True:
            page = _get(f"{GAMMA}/events", {"tag_slug": TAG, "closed": closed, "limit": PAGE, "offset": offset})
            for e in page:
                events[e["id"]] = e
            if len(page) < PAGE:
                break
            offset += PAGE
    return list(events.values())


def market_price(m):
    """The probability Polymarket itself shows: the midpoint, or the last trade when the spread is wide."""
    bid, ask, last = _num(m.get("bestBid")), _num(m.get("bestAsk")), _num(m.get("lastTradePrice"))
    if bid is not None and ask is not None and ask > 0 and ask - bid <= 0.10:
        return round((bid + ask) / 2, 4)
    if last is not None and last > 0:
        return last
    prices = _json_list(m.get("outcomePrices"))
    return _num(prices[0], 0.0) if prices else 0.0


def normalize_market(m):
    tokens = _json_list(m.get("clobTokenIds"))
    prices = _json_list(m.get("outcomePrices"))
    closed = bool(m.get("closed"))
    return {
        "id": m.get("id"),
        "label": (m.get("groupItemTitle") or m.get("question") or "").strip(),
        "question": m.get("question"),
        "slug": m.get("slug"),
        # a resolved market's outcomePrices are the settlement (1/0)
        "price": _num(prices[0], 0.0) if closed and prices else market_price(m),
        "bestBid": _num(m.get("bestBid")),
        "bestAsk": _num(m.get("bestAsk")),
        "lastTrade": _num(m.get("lastTradePrice")),
        "change1d": _num(m.get("oneDayPriceChange"), 0.0),
        "change1w": _num(m.get("oneWeekPriceChange"), 0.0),
        "volume": _num(m.get("volume"), 0.0),
        "volume24h": _num(m.get("volume24hr"), 0.0),
        "liquidity": _num(m.get("liquidity"), 0.0),
        "threshold": _num(m.get("groupItemThreshold")),
        "outcomes": _json_list(m.get("outcomes")),
        "tokenYes": tokens[0] if tokens else None,
        "active": bool(m.get("active")),
        "closed": closed,
        "endDate": m.get("endDate"),
    }


def normalize_event(e):
    markets = [normalize_market(m) for m in e.get("markets", [])]
    # drop never-traded placeholders Polymarket pre-creates for future candidates
    markets = [m for m in markets if m["tokenYes"] and (m["volume"] > 0 or m["closed"])]
    if not e.get("closed"):
        live = [m for m in markets if not m["closed"]]
        markets = live or markets
    is_buckets = len(markets) > 1 and all(BUCKET_RE.match(m["label"]) for m in markets)
    if is_buckets:
        markets.sort(key=lambda m: (m["threshold"] is None, m["threshold"] or 0))
    else:
        markets.sort(key=lambda m: -m["price"])
    return {
        "id": e.get("id"),
        "slug": e.get("slug"),
        "title": (e.get("title") or "").strip(),
        "description": e.get("description"),
        "url": f"https://polymarket.com/event/{e.get('slug')}",
        "image": e.get("icon") or e.get("image"),
        "endDate": e.get("endDate"),
        "closed": bool(e.get("closed")),
        "negRisk": bool(e.get("negRisk")),
        "kind": "buckets" if is_buckets else ("binary" if len(markets) == 1 else "multi"),
        "volume": _num(e.get("volume"), 0.0),
        "volume24h": _num(e.get("volume24hr"), 0.0),
        "liquidity": _num(e.get("liquidity"), 0.0),
        "markets": markets,
    }


def fetch_events(include_closed=False):
    events = [normalize_event(e) for e in fetch_raw_events(include_closed)]
    events = [e for e in events if e["markets"]]
    events.sort(key=lambda e: -e["volume"])
    return {
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tag": TAG,
        "events": events,
        "projection": project_seats([e for e in events if not e["closed"]]),
    }


KNESSET = 120
MAJORITY = 61
THRESHOLD_PCT = 3.25
THRESHOLD_SEATS = 4
SEATS_RE = re.compile(r"^Israel Election:\s*(.+?)\s*# of seats\?$", re.I)
SHARE_RE = re.compile(r"^Israel Election:\s*(.+?)\s*vote share\?$", re.I)

# market titles use several spellings for one party
ALIASES = {
    "Dems": "The Democrats",
    "Otzma": "Otzma Yehudit",
    "UTJ": "United Torah Judaism",
    "United Arab List (Ra'am)": "Ra'am",
    "Religious Zionist Party–Zehut": "RZP-Zehut",
    "The Joint List": "Joint List",
    "Reservists and New Economic Party": "Reservists–New Economic Party",
    "Blue & White": "Blue and White",
}

# Polymarket's own definition, from the rules of "Which Bloc Wins More Seats?" and "Any Coalition Wins a Majority?"
BLOCS = {
    "Netanyahu bloc": ["Likud", "Otzma Yehudit", "RZP-Zehut", "Amcha Yisrael", "United Torah Judaism", "Shas"],
    "Opposition bloc": ["The Democrats", "Yashar", "Together", "Yisrael Beiteinu"],
}
OTHERS = "Not in either bloc"


def _bounds(label):
    """'<20' -> (None, 20), '35+' -> (35, None), '20-24' -> (20, 24), '6' -> (6, 6)."""
    s = label.replace("%", "").replace("–", "-").strip()
    if s.startswith("<"):
        return None, float(s[1:])
    if s.endswith("+"):
        return float(s[:-1]), None
    if "-" in s:
        lo, hi = s.split("-")
        return float(lo), float(hi)
    return float(s), float(s)


def _bracket_values(labels, integer, threshold):
    """A representative value per bracket; open-ended brackets borrow their neighbour's width.

    Anything entirely below the electoral threshold counts as 0: the party gets no seats at all.
    """
    bounds = [_bounds(label) for label in labels]
    step = 1 if integer else 0
    values = []
    for i, (lo, hi) in enumerate(bounds):
        if lo is None:
            if hi <= threshold:
                values.append(0.0)
                continue
            nlo, nhi = bounds[i + 1] if i + 1 < len(bounds) else (hi, hi)
            width = (nhi - nlo + step) if nlo is not None and nhi is not None else 1
            values.append(hi - (width + step) / 2)
        elif hi is None:
            plo, phi = bounds[i - 1] if i else (lo, lo)
            width = (phi - plo + step) if plo is not None and phi is not None else 1
            values.append(lo + (width - step) / 2)
        elif (hi < threshold) if integer else (hi <= threshold):
            values.append(0.0)
        else:
            values.append((lo + hi) / 2)
    return values


def _expectation(event, integer, threshold):
    """Expected value of a bracket market, its most likely bracket, and the chance of clearing the threshold."""
    markets = event["markets"]
    total = sum(m["price"] for m in markets) or 1.0
    values = _bracket_values([m["label"] for m in markets], integer, threshold)
    probs = [m["price"] / total for m in markets]
    top = max(range(len(markets)), key=lambda i: probs[i])
    return {
        "mean": sum(v * p for v, p in zip(values, probs)),
        "likely": {"label": markets[top]["label"], "p": probs[top]},
        "pIn": sum(p for v, p in zip(values, probs) if v > 0),
        "bookSum": total,
        "stale": sum(p for m, p in zip(markets, probs) if _on_last_trade(m)),
        "liquidity": event["liquidity"],
    }


def _on_last_trade(m):
    """market_price fell back to the last trade, which may be days old, because the book is too wide."""
    return m["bestBid"] is None or m["bestAsk"] is None or m["bestAsk"] - m["bestBid"] > 0.10


def _fit_to_knesset(parties):
    """Shift each party's raw estimate so the total is 120, taking the gap mostly from thin markets.

    A party's share of the correction is proportional to seats / liquidity, so a deep book like Likud's
    barely moves while a thin one absorbs most of the error. Nobody is pushed below 0.
    """
    for p in parties:
        p["seats"] = p["seatsRaw"]
    for _ in range(len(parties)):
        gap = sum(p["seats"] for p in parties) - KNESSET
        movable = [p for p in parties if p["seats"] > 0 or gap < 0]
        if abs(gap) < 1e-9 or not movable:
            break
        weights = {p["name"]: p["seatsRaw"] / max(p["liquidity"], 1.0) for p in movable}
        total = sum(weights.values()) or 1.0
        for p in movable:
            p["seats"] = max(0.0, p["seats"] - gap * weights[p["name"]] / total)


def project_seats(events):
    """Expected Knesset seats per party, implied by the seat-bracket and vote-share-bracket markets.

    A vote-share estimate is converted at 120 / (100 - the expected % of votes wasted below the
    threshold). Where a party has both markets, the two estimates are averaged by liquidity. The
    independently priced books rarely add up to 120, and the gap is taken mostly from thin markets.
    """
    canon = lambda name: ALIASES.get(name.strip(), name.strip())
    seats, shares = {}, {}
    below, majority, bigger = 5.0, None, None
    p_in_direct = {}
    for e in events:
        if (m := SEATS_RE.match(e["title"])) and e["kind"] == "buckets":
            seats[canon(m.group(1))] = (_expectation(e, True, THRESHOLD_SEATS), e)
        elif (m := SHARE_RE.match(e["title"])) and e["kind"] == "buckets":
            shares[canon(m.group(1))] = (_expectation(e, False, THRESHOLD_PCT), e)
        elif "below threshold" in e["title"].lower() and e["kind"] == "buckets":
            below = _expectation(e, False, 0)["mean"]
        elif e["slug"].startswith("which-parties-will-win-a-seat"):
            p_in_direct = {canon(m["label"]): m["price"] for m in e["markets"]}
        elif e["slug"] == "israel-election-any-coalition-wins-a-majority":
            majority = e["markets"][0]["price"]
        elif e["slug"] == "israel-election-which-bloc-wins-more-seats":
            bigger = e["markets"][0]["price"]  # first outcome is "Bibi"

    if not seats and not shares:
        return None
    per_pct = KNESSET / (100 - below)
    bloc_of = {party: bloc for bloc, parties in BLOCS.items() for party in parties}
    parties = []
    for name in sorted(set(seats) | set(shares)):
        by_seats, by_share = seats.get(name), shares.get(name)
        main, event = by_seats or by_share
        # both markets price the same thing; the one with more money on its book gets the bigger say
        sources = []
        if by_seats:
            sources.append(("seats", by_seats[0]["mean"], by_seats[0]))
        if by_share:
            sources.append(("share", by_share[0]["mean"] * per_pct, by_share[0]))
        liquidity = sum(x["liquidity"] for _, _, x in sources)
        weight = {kind: (x["liquidity"] / liquidity if liquidity else 1 / len(sources)) for kind, _, x in sources}
        parties.append({
            "name": name,
            "bloc": bloc_of.get(name, OTHERS),
            "seatsRaw": sum(weight[kind] * value for kind, value, _ in sources),
            "seatsFromSeats": by_seats[0]["mean"] if by_seats else None,
            "seatsFromShare": by_share[0]["mean"] * per_pct if by_share else None,
            "weights": weight,
            "liquidity": liquidity,
            "stale": sum(weight[kind] * x["stale"] for kind, _, x in sources),
            "source": "seats" if by_seats else "share",
            "likely": main["likely"],
            "pIn": p_in_direct.get(name, main["pIn"]),
            "bookSum": main["bookSum"],
            "slug": event["slug"],
            "volume": event["volume"],
        })

    raw_total = sum(p["seatsRaw"] for p in parties)
    _fit_to_knesset(parties)
    # a party the market all but rules out of the Knesset is noise, not a row
    parties = [p for p in parties if p["seats"] >= 0.5 or p["pIn"] >= 0.05]
    parties.sort(key=lambda p: -p["seats"])

    blocs = [
        {"name": bloc, "seats": sum(p["seats"] for p in parties if p["bloc"] == bloc)}
        for bloc in [*BLOCS, OTHERS]
    ]
    return {
        "knesset": KNESSET,
        "majority": MAJORITY,
        "rawTotal": raw_total,
        "belowThresholdPct": below,
        "parties": parties,
        "blocs": blocs,
        "markets": {"netanyahuBlocBigger": bigger, "anyBlocMajority": majority},
    }


def fetch_history(token, interval="1w"):
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {sorted(INTERVALS)}")
    data = _get(f"{CLOB}/prices-history", {"market": token, "interval": interval, "fidelity": INTERVALS[interval]})
    return [{"t": int(p["t"]), "p": float(p["p"])} for p in data.get("history", [])]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="data/snapshot.json")
    parser.add_argument("--closed", action="store_true", help="include resolved events")
    parser.add_argument("--history", choices=sorted(INTERVALS), help="also pull price history for every market")
    args = parser.parse_args()

    snapshot = fetch_events(args.closed)
    if args.history:
        markets = [m for e in snapshot["events"] for m in e["markets"]]
        with ThreadPoolExecutor(max_workers=8) as pool:
            for m, hist in zip(markets, pool.map(lambda m: fetch_history(m["tokenYes"], args.history), markets)):
                m["history"] = hist

    import os

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(snapshot, f, indent=1)
    n_markets = sum(len(e["markets"]) for e in snapshot["events"])
    print(f"{len(snapshot['events'])} events, {n_markets} markets -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
