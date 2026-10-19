"""Live flight data via LiteAPI's flights product — the air half of Phase A —
widened with Duffel as a second supplier and explained per itinerary.

With LITEAPI_API_KEY set, the planning board's per-city flight menus become
REAL bookable itineraries with live fares instead of AI estimates:

  POST /flights/rates            legs-based search (round trip / one-way / open-jaw)
  GET  /data/flights/airports    free-text place -> IATA (metro codes included)
  POST /flights/verify           re-quote a stored offer while it's still valid

With DUFFEL_ACCESS_TOKEN set as well, Duffel's offers for the same legs are
searched in parallel and merged in before the diversity pass (duplicates of
the same itinerary collapse to the cheaper copy). Airport resolution comes
from LiteAPI, so Duffel widens a LiteAPI board — a Duffel token alone searches
nothing. Every option then carries a lane (Cheapest, Nonstop, Early arrival…)
and data-backed reasons from option_ranking — the "why" is computed, never
written by a model.

Open-jaw routes (land in the route's first city, fly home from its last) are
requested as true open-jaw legs from both suppliers; if they answer with
plain round trips instead — the sandbox does — the trip is re-priced as two
LiteAPI one-way fares paired per role, and failing that the round-trip quote
is kept with an honest "why".

Everything here is best-effort: any failure returns []/None and callers fall
back to the AI flight menu, so live fares never break trip planning.
Booking (prebook -> book, hold orders) is the payments work and intentionally
not wired.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache

from backend.services import option_ranking as ranking
from backend.services.duffel_service import (
    FLIGHT_HTTP_TIMEOUT as _DUFFEL_TIMEOUT,
    duffel_available,
    get_offer as duffel_get_offer,
    itinerary_signature,
    search_flights as duffel_search_flights,
)
from backend.services.hotel_service import _country_code, _lite_request, _split_place, liteapi_sandbox

_RATES_TIMEOUT = 45  # rate searches routinely take 10s+ on a cold route


def flights_available() -> bool:
    return bool(os.environ.get("LITEAPI_API_KEY"))


# Sandbox keys serve TEST inventory — placeholder carriers ("Nuitee Air" is
# LiteAPI's own house brand), not real airlines. Surfaced on the board so the
# UI can say so instead of letting a made-up airline pass as real.
flights_sandbox = liteapi_sandbox


def flight_target() -> int:
    """How many itineraries each departure city shows."""
    try:
        return max(int(os.environ.get("FLIGHT_OPTIONS_PER_CITY") or 8), 1)
    except ValueError:
        return 8


# ---------------------------------------------------------------------------
# Airport resolution — free text ("New York", "Split, Croatia") -> IATA
# ---------------------------------------------------------------------------

@lru_cache(maxsize=256)
def _airport_lookup(place: str) -> tuple[str, tuple[str, ...]] | None:
    """(search code, all plausible codes for the place) or None.

    Prefers a metro "all airports" code (NYC covers JFK/EWR/LGA) so searches
    fan out across the whole area; the full code tuple validates whether an
    open-jaw answer really departs from the requested city. Cached per
    process — airports don't move. Needs the LiteAPI key: without one there
    is nothing to ask."""
    city, country = _split_place(place)
    if not city or len(city) < 2 or not flights_available():
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

_fmt_minutes = ranking.hm  # '9h 10m' — one formatter for cards and reasons


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
        "journey_key": j.get("journeyKey") or ("liteapi:" + itinerary_signature(segs)),
        "expires_at": offer.get("expiration") or "",
        "fare_family": fare.get("family") or "",
        "carry_on": bool(baggage.get("hasCarryOnBag")),
        "checked_bag": bool(baggage.get("hasCheckedBag")),
        "supplier": "liteapi",
        "sandbox": flights_sandbox(),
        "duffel_offer_id": "",
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
# Merging two suppliers' candidates
# ---------------------------------------------------------------------------

def _signature(cand: dict) -> str:
    return itinerary_signature((cand.get("out") or []) + (cand.get("back") or []))


def _merge_candidates(primary: list[dict], secondary: list[dict]) -> list[dict]:
    """LiteAPI + Duffel candidates as one list, cheapest first.

    The same itinerary from both suppliers appears once — the cheaper copy.
    A secondary fare in another currency than the board's is dropped
    outright (no silent FX). Sandbox inventory is never mixed with
    production inventory: production wins and the other supplier is dropped."""
    if not secondary:
        return list(primary)
    if not primary:
        return list(secondary)
    live_primary = not all(c.get("sandbox") for c in primary)
    live_secondary = not all(c.get("sandbox") for c in secondary)
    if live_primary != live_secondary:
        return list(primary) if live_primary else list(secondary)
    merged: dict[str, dict] = {}
    for cand in primary:
        merged.setdefault(_signature(cand), cand)
    board_currency = (primary[0].get("currency") or "USD")
    for cand in secondary:
        if (cand.get("currency") or "USD") != board_currency:
            continue  # a GBP fare can't be ranked against USD ones — no silent FX
        sig = _signature(cand)
        existing = merged.get(sig)
        if existing is None or cand["price"] < existing["price"]:
            merged[sig] = cand
    return sorted(merged.values(), key=lambda c: c["price"])


# ---------------------------------------------------------------------------
# Diversity — one itinerary per role (cheapest, nonstop, early arrival…)
# ---------------------------------------------------------------------------

def _facts(cand: dict, dest_city: str = "", as_return: bool = False) -> dict:
    """The facts option_ranking judges an itinerary by. A one-way candidate
    from the return search carries its leg under "out"; as_return=True
    files it as the homeward leg."""
    out = cand.get("out") or []
    back = cand.get("back") or []
    if as_return and not back:
        out, back = [], out
    carriers = [(s.get("carrier") or {}).get("marketingCode") or (s.get("carrier") or {}).get("marketingName")
                for s in out + back]
    return {
        "key": cand.get("journey_key") or "",
        "price": float(cand["price"]),
        "minutes": int(cand.get("out_minutes") or 0) + int(cand.get("back_minutes") or 0),
        "stops": cand.get("stops"),
        "out_depart": ranking.parse_hhmm(out[0].get("departureTime")) if out else None,
        "out_arrive": ranking.parse_hhmm(out[-1].get("arrivalTime")) if out else None,
        "out_arrive_date": (out[-1].get("arrivalTime") or "")[:10] if out else "",
        "back_depart": ranking.parse_hhmm(back[0].get("departureTime")) if back else None,
        "checked_bag": bool(cand.get("checked_bag")),
        "carry_on": bool(cand.get("carry_on")),
        "single_carrier": len(set(c for c in carriers if c)) == 1,
        "carrier": (cand.get("carriers") or ["Airline"])[0],
        "dest_city": dest_city,
        "live": True,
    }


def _pick_diverse(cands: list[dict], limit: int | None = None, pace: str | None = None,
                  dest_city: str = "") -> list[tuple[str, dict, list, int]]:
    """[(role, candidate, reasons, match_score), ...] — up to `limit`
    genuinely different journeys, one per role, in the pace's claim order.
    A pick must EARN its slot: near-identical price and door-to-door time to
    an existing pick reads as a duplicate on the board, so it's skipped."""
    if not cands:
        return []
    facts = [_facts(c, dest_city) for c in cands]
    picks = ranking.pick_flight_roles(facts, limit or flight_target(), pace)
    return [(p["role"], cands[p["index"]], p["reasons"], p["match_score"]) for p in picks]


# ---------------------------------------------------------------------------
# Mapping candidates into the board's flight-option schema
# ---------------------------------------------------------------------------

def _option_from(role: str, out_cand: dict, back_cand: dict | None = None,
                 why_extra: str = "", reasons: list | None = None,
                 match_score: int | None = None) -> dict:
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
    lane = ranking.ROLE_LANES.get(role, "Best balance")
    reasons = list(reasons or [])
    why = ranking.why_line(None, reasons, why_extra) if reasons else (why_extra or lane)

    legs = (out_cand, back_cand) if pair else (out_cand,)
    expiries = [c.get("expires_at") for c in legs if c.get("expires_at")]
    suppliers = list(dict.fromkeys(c.get("supplier") or "liteapi" for c in legs))
    duffel_ids = [c.get("duffel_offer_id") for c in legs if c.get("duffel_offer_id")]
    return {
        "airline": " + ".join(carriers[:2]) + (" and partners" if len(carriers) > 2 else ""),
        "route": _route_str(out_segs),
        "return_route": _route_str(back_segs),
        "depart": _fmt_when(out_segs[0].get("departureTime")),
        "arrive": _fmt_when(out_segs[-1].get("arrivalTime")),
        "return": _fmt_when(back_segs[0].get("departureTime")) if back_segs else "",
        "return_arrive": _fmt_when(back_segs[-1].get("arrivalTime")) if back_segs else "",
        "stops": stops,
        "duration": _fmt_minutes(out_cand["out_minutes"]),
        "return_duration": _fmt_minutes(back_minutes),
        "est_price_per_person": round(price),
        "currency": out_cand.get("currency") or "USD",
        "why": why,
        "live_price": True,
        "offer_ids": [c["offer_id"] for c in legs if c.get("offer_id")],
        "journey_key": "+".join(filter(None, [out_cand["journey_key"],
                                              back_cand["journey_key"] if pair else ""])),
        "fare_family": out_cand["fare_family"],
        "bags": {"carry_on": out_cand["carry_on"], "checked": out_cand["checked_bag"]},
        "segments": {"out": out_rows, "back": back_rows},
        "priced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires_at": min(expiries) if expiries else "",
        "lane": lane,
        "role": role,
        "reasons": reasons,
        "match_score": match_score,
        "supplier": suppliers[0] if len(suppliers) == 1 else "+".join(suppliers),
        "sandbox": any(c.get("sandbox") for c in legs),
        "duffel_offer_id": duffel_ids[0] if len(duffel_ids) == 1 and not pair else "",
        "duffel_offer_ids": duffel_ids,
    }


def _search_both(legs: list[dict], travelers: int, origin, dest, return_from, home_to,
                 depart_date: str, return_date: str) -> list[dict]:
    """LiteAPI and Duffel at once, each bounded by its own wait. The pool is
    NOT a context manager: a supplier that trickles past its bound is
    abandoned (shutdown without waiting), never allowed to hold the board."""
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        lite_f = pool.submit(_search_journeys, legs, travelers)
        duffel_f = pool.submit(duffel_search_flights, origin[0], dest[0], depart_date, return_date,
                               travelers, return_from[0], home_to[0]) if duffel_available() else None
        try:
            lite = lite_f.result(timeout=_RATES_TIMEOUT + 5)
        except Exception:
            lite = []
        try:
            # _DUFFEL_TIMEOUT bounds the socket; this bounds the wait itself
            duffel = duffel_f.result(timeout=_DUFFEL_TIMEOUT + 0.5) if duffel_f else []
        except Exception:
            duffel = []
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return _merge_candidates(lite, duffel)


def real_flight_options(origin_city: str, dest_city: str, depart_date: str,
                        return_date: str, travelers: int = 1,
                        return_from_city: str | None = None,
                        return_to_city: str | None = None,
                        pace: str | None = None) -> list[dict]:
    """Up to FLIGHT_OPTIONS_PER_CITY live round-trip offers from one departure
    city, mapped to the board's flight-option schema, one per role
    (cheapest / nonstop / fastest / best balance / bags / early arrival /
    no early start / late return / one airline), LiteAPI and Duffel merged.

    return_from_city: open-jaw trips fly home from the route's last city.
    return_to_city:   travelers who end the trip in a different home city.
    pace:             the group's pace reorders which roles claim first.
    Needs the LiteAPI key (airports are resolved through it); Duffel joins
    when its token is set too. Returns [] on ANY failure — callers must fall
    back to the AI menu."""
    if not (flights_available() and origin_city and dest_city and depart_date and return_date):
        return []
    if origin_city.strip().casefold() in ("", "your city"):
        return []  # questionnaire placeholder, not a place
    try:
        town = _split_place(dest_city)[0]
        origin = _resolve(origin_city)
        dest = _resolve(dest_city)
        if not (origin and dest):
            return []
        home_to = origin
        if return_to_city and return_to_city.strip().casefold() != origin_city.strip().casefold():
            home_to = _resolve(return_to_city) or origin

        open_jaw = bool(return_from_city) and (
            _split_place(return_from_city)[0].casefold() != town.casefold())
        return_from = (_resolve(return_from_city) if open_jaw else None) or dest

        legs = [
            {"origin": origin[0], "destination": dest[0], "date": depart_date, "direction": "OUTBOUND"},
            {"origin": return_from[0], "destination": home_to[0], "date": return_date, "direction": "INBOUND"},
        ]
        cands = _search_both(legs, travelers, origin, dest, return_from, home_to, depart_date, return_date)

        if open_jaw:
            honored = [c for c in cands
                       if c["back"] and c["back"][0].get("originCode") in return_from[1]]
            if honored:
                return [_option_from(role, c, reasons=rs, match_score=ms)
                        for role, c, rs, ms in _pick_diverse(honored, pace=pace, dest_city=town)]
            # The provider ignored the open-jaw return (sandbox inventory does)
            # — price the trip as two one-way fares instead, which honors both
            # cities and stays fully bookable.
            pair_options = _open_jaw_as_one_ways(
                origin, dest, return_from, home_to, depart_date, return_date, travelers,
                pace=pace, dest_city=town)
            if pair_options:
                return pair_options
            if cands:  # last resort: keep the live quote, say what it really is
                note = (f"Live price is a round trip via {town} — "
                        f"your actual return leaves from {_split_place(return_from_city)[0]}, so treat it as a floor")
                return [_option_from(role, c, why_extra=note, reasons=rs, match_score=ms)
                        for role, c, rs, ms in _pick_diverse(cands, pace=pace, dest_city=town)]
            return []

        return [_option_from(role, c, reasons=rs, match_score=ms)
                for role, c, rs, ms in _pick_diverse(cands, pace=pace, dest_city=town)]
    except Exception:
        # Live fares are an enhancement — they must never break trip planning
        return []


def _open_jaw_as_one_ways(origin, dest, return_from, home_to,
                          depart_date: str, return_date: str, travelers: int,
                          pace: str | None = None, dest_city: str = "") -> list[dict]:
    """Two LiteAPI one-way searches (into the first city, home from the
    last), paired per role: outbound-driven roles (early arrival, no early
    start) take their outbound pick plus the cheapest return; late return
    takes the cheapest outbound plus its return pick; one airline and bags
    need both legs to qualify; nonstop must be nonstop both ways; price/time
    roles pair role-for-role. Duffel's true open-jaw offers already took
    part in the primary search — this fallback is LiteAPI-only."""
    def one_way(frm, to, date):
        try:
            return _search_journeys(
                [{"origin": frm[0], "destination": to[0], "date": date, "direction": "OUTBOUND"}],
                travelers)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        out_f = pool.submit(one_way, origin, dest, depart_date)
        back_f = pool.submit(one_way, return_from, home_to, return_date)
        out_cands, back_cands = out_f.result(), back_f.result()
    if not (out_cands and back_cands):
        return []

    all_roles = len(ranking.FLIGHT_ROLES)
    out_picks = {role: c for role, c, _, _ in _pick_diverse(out_cands, limit=all_roles, pace=pace, dest_city=dest_city)}
    back_facts = [_facts(c, dest_city, as_return=True) for c in back_cands]
    back_picks = {p["role"]: back_cands[p["index"]]
                  for p in ranking.pick_flight_roles(back_facts, all_roles, pace)}
    cheapest_out = min(out_cands, key=lambda c: c["price"])
    cheapest_back = min(back_cands, key=lambda c: c["price"])

    def single_carrier(c):
        return _facts(c)["single_carrier"]

    def cheapest_pair(pred):
        pairs = [(o, b) for o in out_cands for b in back_cands if pred(o, b)]
        return min(pairs, key=lambda p: p[0]["price"] + p[1]["price"]) if pairs else (None, None)

    pairs = []
    for role in ranking.flight_role_order(pace):
        if role in ("early_arrival", "no_early_start"):
            o, b = out_picks.get(role), cheapest_back
        elif role == "late_return":
            o, b = cheapest_out, back_picks.get(role)
        elif role == "one_airline":
            o, b = cheapest_pair(lambda o, b: single_carrier(o) and single_carrier(b)
                                 and o["carriers"][:1] == b["carriers"][:1])
        elif role == "bags":
            o, b = cheapest_pair(lambda o, b: o["checked_bag"] and b["checked_bag"])
        elif role == "nonstop":
            o, b = cheapest_pair(lambda o, b: o["stops"] == 0 and b["stops"] == 0)
        else:
            o, b = out_picks.get(role), back_picks.get(role)
            o = o or cheapest_out
            b = b or cheapest_back
        if o and b:
            pairs.append((role, o, b))

    # Reasons are judged on the combined trip, against the other pairs
    combined = []
    for role, o, b in pairs:
        f = _facts(o, dest_city)
        fb = _facts(b, dest_city, as_return=True)
        f.update({
            "key": f"{o['journey_key']}+{b['journey_key']}",
            "price": o["price"] + b["price"],
            "minutes": int(o.get("out_minutes") or 0) + int(b.get("out_minutes") or b.get("back_minutes") or 0),
            "stops": max(o["stops"], b["stops"]),
            "back_depart": fb["back_depart"],
            "checked_bag": o["checked_bag"] and b["checked_bag"],
            "carry_on": o["carry_on"] and b["carry_on"],
            "single_carrier": single_carrier(o) and single_carrier(b) and o["carriers"][:1] == b["carriers"][:1],
        })
        combined.append(f)
    if not combined:
        return []
    note = "Priced as two one-way fares (booked separately)"
    options = []
    for (role, o, b), label in zip(pairs, ranking.explain_fixed_roles(combined, [r for r, _, _ in pairs])):
        if label is None:
            continue  # a near-duplicate of an earlier pair
        options.append(_option_from(role, o, back_cand=b, why_extra=note, reasons=label["reasons"],
                                    match_score=label["match_score"]))
    return options


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


def _offer_alive(expires: str) -> bool:
    """Whether a stored offer can still be re-verified. Supplier stamps may
    arrive without an offset — those are read as UTC."""
    if not expires:
        return False
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < when


def live_flight_details(option: dict) -> dict:
    """The option-detail payload for a flight. Live options serve their stored
    segments instantly; the fare is re-verified only while the quoted offer
    can still be alive — through the supplier that quoted it (LiteAPI's
    /flights/verify, or Duffel's GET /air/offers/{id}). AI/sample flights stay
    source="board" so the screen keeps rendering the board's own facts.
    Never raises: on any surprise the page still gets the stored facts."""
    try:
        segments = option.get("segments") or {}
        if not (option.get("live_price") and segments.get("out")):
            return {"kind": "live", "source": "board"}

        supplier = option.get("supplier") or "liteapi"
        details = {
            "kind": "live",
            "source": "duffel" if supplier == "duffel" else "liteapi",
            "supplier": supplier,
            "segments": segments,
            "out_duration": option.get("duration") or "",
            "back_duration": option.get("return_duration") or "",
            "fare_family": option.get("fare_family") or "",
            "bags": option.get("bags") or {},
            "price_per_person": option.get("est_price_per_person"),
            "priced_at": option.get("priced_at") or "",
            "price_fresh": False,
        }
        if not _offer_alive(str(option.get("expires_at") or "")):
            return details
        try:
            if supplier == "duffel":
                fresh = duffel_get_offer(option.get("duffel_offer_id") or "")
                if fresh and (fresh.get("currency") or "USD") == (option.get("currency") or "USD"):
                    details["price_per_person"] = round(fresh["price_per_person"])
                    details["price_fresh"] = True
            elif supplier == "liteapi":
                offer_ids = option.get("offer_ids") or []
                if offer_ids and flights_available():
                    fresh = [_verified_price(oid) for oid in offer_ids]
                    if all(p is not None for p in fresh):
                        details["price_per_person"] = round(sum(fresh))
                        details["price_fresh"] = True
        except Exception:
            pass  # stored quote + its age is the honest fallback
        return details
    except Exception:
        return {"kind": "live", "source": "board"}
