"""Live flight data via LiteAPI's flights product — the air half of Phase A.

With LITEAPI_API_KEY set, the planning board's per-city flight menus become
REAL bookable itineraries with live fares instead of AI estimates:

  POST /flights/rates            legs-based search (round trip / one-way / open-jaw)
  GET  /data/flights/airports    free-text place -> IATA (metro codes included)
  POST /flights/verify           re-quote a stored offer while it's still valid

Open-jaw routes (land in the route's first city, fly home from its last) are
requested as true open-jaw legs; if the provider answers with plain round
trips instead — the sandbox does — the trip is re-priced as two one-way
fares, and failing that the round-trip quote is kept with an honest "why".

Everything here is best-effort: any failure returns []/None and callers fall
back to the AI flight menu, so live fares never break trip planning.
Booking (prebook -> book) is Phase B/C and intentionally not wired.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache

from backend.services.hotel_service import _country_code, _lite_request, _split_place

_RATES_TIMEOUT = 45  # rate searches routinely take 10s+ on a cold route


def flights_available() -> bool:
    return bool(os.environ.get("LITEAPI_API_KEY"))


# ---------------------------------------------------------------------------
# Airport resolution — free text ("New York", "Split, Croatia") -> IATA
# ---------------------------------------------------------------------------

@lru_cache(maxsize=256)
def _airport_lookup(place: str) -> tuple[str, tuple[str, ...]] | None:
    """(search code, all plausible codes for the place) or None.

    Prefers a metro "all airports" code (NYC covers JFK/EWR/LGA) so searches
    fan out across the whole area; the full code tuple validates whether an
    open-jaw answer really departs from the requested city. Cached per
    process — airports don't move."""
    city, country = _split_place(place)
    if not city or len(city) < 2:
        return None
    data = _lite_request("/data/flights/airports", params={"q": city})
    groups = data.get("data") or []
    airports = []
    for g in groups if isinstance(groups, list) else []:
        airports.extend(g.get("airports") or [])
    code = _country_code(country)
    if code:
        airports = [a for a in airports if (a.get("country") or "").upper() == code] or airports
    airports = [a for a in airports if a.get("iata")]
    if not airports:
        return None
    served = [a for a in airports if a.get("hasAirlineService")]
    metro = next((a for a in airports
                  if not a.get("hasAirlineService") and "all airports" in (a.get("name") or "").lower()), None)
    primary = (metro or (served[0] if served else airports[0]))["iata"]
    codes = tuple(dict.fromkeys(a["iata"] for a in airports[:8]))
    return primary, codes


def _resolve(place: str) -> tuple[str, tuple[str, ...]] | None:
    try:
        return _airport_lookup((place or "").strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsing /flights/rates journeys into lean candidates
# ---------------------------------------------------------------------------

def _fmt_minutes(minutes) -> str:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ""
    h, m = divmod(max(minutes, 0), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _fmt_when(iso: str) -> str:
    """'2026-08-14T17:55:00' -> '2026-08-14 17:55' (the board's option format)."""
    return (iso or "").replace("T", " ")[:16]


def _seg_row(seg: dict) -> dict:
    carrier = seg.get("carrier") or {}
    flight = seg.get("flight") or {}
    number = flight.get("marketingNumber") or ""
    code = carrier.get("marketingCode") or ""
    return {
        "leg": f"{seg.get('originCode', '?')} → {seg.get('destinationCode', '?')}",
        "carrier": carrier.get("marketingName") or code or "Airline",
        "flight_no": f"{code} {number}".strip(),
        "depart": _fmt_when(seg.get("departureTime")),
        "arrive": _fmt_when(seg.get("arrivalTime")),
        "duration": _fmt_minutes((seg.get("duration") or {}).get("minutes")),
        "from_name": seg.get("originName") or "",
        "to_name": seg.get("destinationName") or "",
    }


def _layovers(rows: list[dict], segs: list[dict]) -> list[dict]:
    """Stamp 'layover_after' on each row that has a wait before the next leg."""
    for i in range(len(rows) - 1):
        try:
            arrive = datetime.fromisoformat(segs[i]["arrivalTime"])
            depart = datetime.fromisoformat(segs[i + 1]["departureTime"])
            wait = int((depart - arrive).total_seconds() // 60)
        except (KeyError, TypeError, ValueError):
            continue
        if wait > 0:
            where = segs[i].get("destinationName") or segs[i].get("destinationCode") or ""
            where = re.sub(r"\s+(international\s+)?airport$", "", where, flags=re.I)
            rows[i]["layover_after"] = f"{_fmt_minutes(wait)} in {where}".strip()
    return rows


def _route_str(segs: list[dict]) -> str:
    if not segs:
        return ""
    start = segs[0].get("originCode", "?")
    end = segs[-1].get("destinationCode", "?")
    vias = [s.get("destinationCode", "?") for s in segs[:-1]]
    return f"{start} → {end}" + (f" (via {', '.join(vias)})" if vias else "")


def _parse_journey(j: dict, travelers: int) -> dict | None:
    """One /flights/rates journey -> a lean candidate, or None if unusable."""
    segs = j.get("segments") or []
    out = [s for s in segs if s.get("direction") != "INBOUND"]
    back = [s for s in segs if s.get("direction") == "INBOUND"]
    if not out:
        return None
    offer = j.get("cheapestOffer") or {}
    pricing = ((offer.get("pricing") or {}).get("display")) or {}
    per_person = ((pricing.get("perPassenger") or {}).get("adult") or {}).get("total") or pricing.get("total")
    if not per_person:
        return None
    fare = offer.get("fare") or {}
    seats = fare.get("seatsRemaining")
    if isinstance(seats, int) and 0 < seats < travelers:
        return None  # not enough seats in this fare for the whole party
    durations = {(d.get("direction") or "OUTBOUND"): (d.get("duration") or {}).get("minutes")
                 for d in j.get("legDurations") or []}
    baggage = offer.get("baggage") or {}
    return {
        "out": out,
        "back": back,
        "price": float(per_person),
        "currency": pricing.get("currency") or "USD",
        "out_minutes": durations.get("OUTBOUND") or sum(
            (s.get("duration") or {}).get("minutes") or 0 for s in out),
        "back_minutes": durations.get("INBOUND") or sum(
            (s.get("duration") or {}).get("minutes") or 0 for s in back),
        "stops": max(len(out) - 1, len(back) - 1 if back else 0),
        "carriers": list(dict.fromkeys(
            (s.get("carrier") or {}).get("marketingName") or "Airline" for s in segs)),
        "offer_id": offer.get("offerId") or "",
        "journey_key": j.get("journeyKey") or "",
        "expires_at": offer.get("expiration") or "",
        "fare_family": fare.get("family") or "",
        "carry_on": bool(baggage.get("hasCarryOnBag")),
        "checked_bag": bool(baggage.get("hasCheckedBag")),
    }


def _search_journeys(legs: list[dict], travelers: int) -> list[dict]:
    """All parseable journey candidates for a legs itinerary, cheapest first."""
    body = _lite_request("/flights/rates", payload={
        "legs": legs,
        "adults": max(1, min(int(travelers or 1), 9)),
        "currency": "USD",
        "cabinClass": "ECONOMY",
    }, timeout=_RATES_TIMEOUT)
    journeys = []
    for group in body.get("data") or []:
        journeys.extend(group.get("journeys") or [])
    cands = [c for c in (_parse_journey(j, travelers) for j in journeys) if c]
    cands.sort(key=lambda c: c["price"])
    return cands


# ---------------------------------------------------------------------------
# Diversity — cheapest / nonstop / fastest / balanced, not 4 near-clones
# ---------------------------------------------------------------------------

_WHY = {
    "cheapest": "Cheapest live fare for your dates",
    "nonstop": "Nonstop — the quickest door to door",
    "fastest": "Fastest routing on these dates",
    "balanced": "Strong balance of price and travel time",
}


def _pick_diverse(cands: list[dict], limit: int = 4) -> list[tuple[str, dict]]:
    """[(role, candidate), ...] — up to `limit` genuinely different journeys.
    A pick must EARN its slot: near-identical price and door-to-door time to
    an existing pick reads as a duplicate on the board, so it's skipped."""
    def total_minutes(c):
        return (c["out_minutes"] or 0) + (c["back_minutes"] or 0)

    picks: list[tuple[str, dict]] = []

    def claim(role: str, cand: dict | None):
        if not cand:
            return
        for _, taken in picks:
            same = taken["journey_key"] == cand["journey_key"]
            similar = (abs(taken["price"] - cand["price"]) < max(10, 0.02 * taken["price"])
                       and abs(total_minutes(taken) - total_minutes(cand)) <= 20)
            if same or similar:
                return
        picks.append((role, cand))

    if not cands:
        return []
    claim("cheapest", cands[0])
    claim("nonstop", next((c for c in cands if c["stops"] == 0), None))
    claim("fastest", min(cands, key=lambda c: (total_minutes(c), c["price"])))
    floor_price = cands[0]["price"] or 1
    floor_time = min(total_minutes(c) for c in cands) or 1
    taken_keys = {c["journey_key"] for _, c in picks}
    remaining = [c for c in cands if c["journey_key"] not in taken_keys]
    if remaining:
        claim("balanced", min(remaining, key=lambda c: c["price"] / floor_price
                              + total_minutes(c) / floor_time))
    return picks[:limit]


# ---------------------------------------------------------------------------
# Mapping candidates into the board's flight-option schema
# ---------------------------------------------------------------------------

def _bags_note(cand: dict) -> str:
    if cand["checked_bag"]:
        return "checked bag included"
    if cand["carry_on"]:
        return "carry-on only"
    return ""


def _option_from(role: str, out_cand: dict, back_cand: dict | None = None,
                 why_extra: str = "") -> dict:
    """A board flight option from one round-trip candidate, or from an
    out + back pair of one-way candidates (the open-jaw fallback)."""
    pair = back_cand is not None
    out_segs = out_cand["out"]
    # A paired return comes from its own one-way search, whose segments are
    # tagged OUTBOUND — they land in the candidate's "out" bucket.
    back_segs = (back_cand["back"] or back_cand["out"]) if pair else out_cand["back"]
    out_rows = _layovers([_seg_row(s) for s in out_segs], out_segs)
    back_rows = _layovers([_seg_row(s) for s in back_segs], back_segs)

    price = out_cand["price"] + (back_cand["price"] if pair else 0)
    carriers = list(dict.fromkeys(out_cand["carriers"] + (back_cand["carriers"] if pair else [])))
    stops = max(len(out_segs) - 1, (len(back_segs) - 1) if back_segs else 0)
    back_minutes = (back_cand["out_minutes"] or back_cand["back_minutes"]) if pair else out_cand["back_minutes"]
    why = _WHY.get(role, _WHY["balanced"])
    bags = _bags_note(out_cand)
    if bags:
        why += f" — {bags}"
    if why_extra:
        why += f". {why_extra}"

    expiries = [e for e in (out_cand["expires_at"], back_cand["expires_at"] if pair else None) if e]
    return {
        "airline": " + ".join(carriers[:2]) + (" and partners" if len(carriers) > 2 else ""),
        "route": _route_str(out_segs),
        "return_route": _route_str(back_segs),
        "depart": _fmt_when(out_segs[0].get("departureTime")),
        "return": _fmt_when(back_segs[0].get("departureTime")) if back_segs else "",
        "stops": stops,
        "duration": _fmt_minutes(out_cand["out_minutes"]),
        "return_duration": _fmt_minutes(back_minutes),
        "est_price_per_person": round(price),
        "why": why,
        "live_price": True,
        "offer_ids": [c["offer_id"] for c in ((out_cand, back_cand) if pair else (out_cand,)) if c["offer_id"]],
        "journey_key": "+".join(filter(None, [out_cand["journey_key"],
                                              back_cand["journey_key"] if pair else ""])),
        "fare_family": out_cand["fare_family"],
        "bags": {"carry_on": out_cand["carry_on"], "checked": out_cand["checked_bag"]},
        "segments": {"out": out_rows, "back": back_rows},
        "priced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires_at": min(expiries) if expiries else "",
    }


def real_flight_options(origin_city: str, dest_city: str, depart_date: str,
                        return_date: str, travelers: int = 1,
                        return_from_city: str | None = None,
                        return_to_city: str | None = None) -> list[dict]:
    """Up to ~4 live round-trip offers from one departure city, mapped to the
    board's flight-option schema (cheapest / nonstop / fastest / balanced).

    return_from_city: open-jaw trips fly home from the route's last city.
    return_to_city:   travelers who end the trip in a different home city.
    Returns [] on ANY failure — callers must fall back to the AI menu."""
    if not (flights_available() and origin_city and dest_city and depart_date and return_date):
        return []
    if origin_city.strip().casefold() in ("", "your city"):
        return []  # questionnaire placeholder, not a place
    try:
        origin = _resolve(origin_city)
        dest = _resolve(dest_city)
        if not (origin and dest):
            return []
        home_to = origin
        if return_to_city and return_to_city.strip().casefold() != origin_city.strip().casefold():
            home_to = _resolve(return_to_city) or origin

        open_jaw = bool(return_from_city) and (
            _split_place(return_from_city)[0].casefold() != _split_place(dest_city)[0].casefold())
        return_from = (_resolve(return_from_city) if open_jaw else None) or dest

        legs = [
            {"origin": origin[0], "destination": dest[0], "date": depart_date, "direction": "OUTBOUND"},
            {"origin": return_from[0], "destination": home_to[0], "date": return_date, "direction": "INBOUND"},
        ]
        cands = _search_journeys(legs, travelers)

        if open_jaw:
            honored = [c for c in cands
                       if c["back"] and c["back"][0].get("originCode") in return_from[1]]
            if honored:
                return [_option_from(role, c) for role, c in _pick_diverse(honored)]
            # The provider ignored the open-jaw return (sandbox inventory does)
            # — price the trip as two one-way fares instead, which honors both
            # cities and stays fully bookable.
            pair_options = _open_jaw_as_one_ways(
                origin, dest, return_from, home_to, depart_date, return_date, travelers)
            if pair_options:
                return pair_options
            if cands:  # last resort: keep the live quote, say what it really is
                note = (f"Live price is a round trip via {_split_place(dest_city)[0]} — "
                        f"your actual return leaves from {_split_place(return_from_city)[0]}, so treat it as a floor")
                return [_option_from(role, c, why_extra=note) for role, c in _pick_diverse(cands)]
            return []

        return [_option_from(role, c) for role, c in _pick_diverse(cands)]
    except Exception:
        # Live fares are an enhancement — they must never break trip planning
        return []


def _open_jaw_as_one_ways(origin, dest, return_from, home_to,
                          depart_date: str, return_date: str, travelers: int) -> list[dict]:
    """Two one-way searches (into the first city, home from the last), paired
    role-for-role: cheapest out + cheapest home, and so on."""
    def one_way(frm, to, date):
        return _search_journeys(
            [{"origin": frm[0], "destination": to[0], "date": date, "direction": "OUTBOUND"}],
            travelers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        out_f = pool.submit(one_way, origin, dest, depart_date)
        back_f = pool.submit(one_way, return_from, home_to, return_date)
        out_cands, back_cands = out_f.result(), back_f.result()
    if not (out_cands and back_cands):
        return []
    out_picks = dict(_pick_diverse(out_cands))
    back_picks = dict(_pick_diverse(back_cands))
    note = "Priced as two one-way fares (booked separately)"
    options = []
    for role in ("cheapest", "nonstop", "fastest", "balanced"):
        o, b = out_picks.get(role), back_picks.get(role)
        if role == "nonstop" and not (o and b):
            continue  # a nonstop pairing must be nonstop BOTH ways
        o = o or out_picks.get("cheapest")
        b = b or back_picks.get("cheapest")
        if o and b:
            options.append(_option_from(role, o, back_cand=b, why_extra=note))
    seen, unique = set(), []
    for opt in options:
        if opt["journey_key"] not in seen:
            seen.add(opt["journey_key"])
            unique.append(opt)
    return unique


# ---------------------------------------------------------------------------
# Option-detail screen — stored segments + best-effort re-quote
# ---------------------------------------------------------------------------

def _verified_price(offer_id: str) -> float | None:
    """POST /flights/verify -> fresh per-person price, or None. Offers expire
    within ~30 minutes of a search, so this mostly succeeds right after board
    generation and quietly fails later — callers keep the stored price."""
    body = _lite_request("/flights/verify", payload={"offerId": offer_id},
                         timeout=15, attempts=1)

    def find_per_adult(node):
        if isinstance(node, dict):
            adult = (node.get("perPassenger") or {}).get("adult") if "perPassenger" in node else None
            if isinstance(adult, dict) and adult.get("total"):
                return float(adult["total"])
            for v in node.values():
                found = find_per_adult(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = find_per_adult(v)
                if found:
                    return found
        return None

    return find_per_adult(body.get("data") or body)


def live_flight_details(option: dict) -> dict:
    """The option-detail payload for a flight. Live options serve their stored
    segments instantly; the fare is re-verified only while the quoted offer
    can still be alive. AI/sample flights stay source="board" so the screen
    keeps rendering the board's own facts."""
    segments = option.get("segments") or {}
    if not (option.get("live_price") and segments.get("out")):
        return {"kind": "live", "source": "board"}

    details = {
        "kind": "live",
        "source": "liteapi",
        "segments": segments,
        "out_duration": option.get("duration") or "",
        "back_duration": option.get("return_duration") or "",
        "fare_family": option.get("fare_family") or "",
        "bags": option.get("bags") or {},
        "price_per_person": option.get("est_price_per_person"),
        "priced_at": option.get("priced_at") or "",
        "price_fresh": False,
    }

    expires = option.get("expires_at") or ""
    offer_ids = option.get("offer_ids") or []
    if offer_ids and flights_available() and expires:
        try:
            if datetime.now(timezone.utc) < datetime.fromisoformat(expires.replace("Z", "+00:00")):
                fresh = [_verified_price(oid) for oid in offer_ids]
                if all(p is not None for p in fresh):
                    details["price_per_person"] = round(sum(fresh))
                    details["price_fresh"] = True
        except Exception:
            pass  # stored quote + its age is the honest fallback
    return details
