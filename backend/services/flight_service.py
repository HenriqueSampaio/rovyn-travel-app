"""Live flight data via Duffel — the air half of Phase A — explained per
itinerary.

All flight bookings go through Duffel (paid from the Duffel Balance, see
duffel_booking.py), so the board searches Duffel and nothing else: with
DUFFEL_ACCESS_TOKEN set, the per-city flight menus become REAL bookable
itineraries with live fares instead of AI estimates. Each carries its
duffel_offer_id, which gates auto-booking at checkout and drives the
detail page's re-verify; the lock itself (booking_service.build_lock)
re-searches the same itinerary from the stored segments and matches it by
itinerary signature — the stored id is never re-read:

  GET  /places/suggestions   free-text place -> IATA (city codes like NYC included)
  POST /air/offer_requests   round trip, open jaw (one multi-slice request) or one-way
  GET  /air/offers/{id}      re-quote a stored offer while it's still valid

Every option carries a lane (Cheapest, Nonstop, Early arrival…) and
data-backed reasons from option_ranking — the "why" is computed, never
written by a model.

Open-jaw routes (land in the route's first city, fly home from its last) are
requested as one two-slice offer request; only if Duffel ANSWERED and no
offer honors the return city is the trip re-priced as two Duffel one-way
fares paired per role. Such a pair is two separate tickets Rovyn can't book
as one item yet, so it is labeled "you book these yourself" and checkout
sends it straight to self-book. A return city that can't be resolved, or a
first search that failed or timed out, returns [] (the AI menu covers it)
rather than passing a plain round trip off as the open jaw.

Everything here is best-effort: any failure returns []/None and callers fall
back to the AI flight menu, so live fares never break trip planning.
Booking (hold orders, balance payments) lives in booking_service.
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
    search_flights as duffel_search_flights,
    suggest_places,
)
from backend.services.hotel_service import _country_code, _split_place

# Every fare on the board is priced in this currency: the settlement charges
# USD, and an offer in another currency can't be ranked against the rest
# without a silent FX guess, so it is left off.
BOARD_CURRENCY = "USD"


def flights_available() -> bool:
    return duffel_available()


def flight_target() -> int:
    """How many itineraries each departure city shows."""
    try:
        return max(int(os.environ.get("FLIGHT_OPTIONS_PER_CITY") or 8), 1)
    except ValueError:
        return 8


# ---------------------------------------------------------------------------
# Airport resolution — free text ("New York", "Split, Croatia") -> IATA
# ---------------------------------------------------------------------------

def _place_codes(place: dict) -> list[str]:
    """A Duffel place's own code, then its airports' (a city lists them; an
    airport in a metro area lists its siblings under city.airports)."""
    codes = [place.get("iata_code")]
    codes += [a.get("iata_code") for a in place.get("airports") or [] if isinstance(a, dict)]
    return [c for c in codes if c]


@lru_cache(maxsize=256)
def _airport_lookup(place: str) -> tuple[str, tuple[str, ...]] | None:
    """(search code, all plausible codes for the place) or None.

    Prefers a city code (NYC covers JFK/EWR/LGA) so searches fan out across
    the whole metro area; the full code tuple validates whether an open-jaw
    answer really departs from the requested city. Places whose name matches
    the town come first, and a country in the text ("Rome, Italy") filters
    out namesakes elsewhere. Cached per process — airports don't move; a
    failed lookup raises and is never cached."""
    city, country = _split_place(place)
    if not city or len(city) < 2 or not flights_available():
        return None
    places = [p for p in suggest_places(city) if p.get("iata_code")]
    code = _country_code(country)
    if code:
        places = [p for p in places if (p.get("iata_country_code") or "").upper() == code] or places
    if not places:
        return None
    town = city.casefold()

    def named(p: dict) -> bool:
        return town in ((p.get("city_name") or "").casefold(), (p.get("name") or "").casefold(),
                        ((p.get("city") or {}).get("name") or "").casefold())

    pool = [p for p in places if named(p)] or places  # Duffel's own order within each
    metro = next((p for p in pool if p.get("type") == "city"), None)
    if metro is None and (pool[0].get("city") or {}).get("iata_code"):
        metro = pool[0]["city"]  # an airport inside a metro area: search the city code
    primary = (metro or pool[0])["iata_code"]
    codes = [primary] + _place_codes(metro or {})
    codes += [c for p in pool for c in _place_codes(p) + _place_codes(p.get("city") or {})]
    return primary, tuple(dict.fromkeys(codes))[:10]


def _resolve(place: str) -> tuple[str, tuple[str, ...]] | None:
    try:
        return _airport_lookup((place or "").strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Segment rows for the board (candidates come from duffel_service)
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


# An open jaw priced as two one-way Duffel tickets. Rovyn books one Duffel
# offer per checklist flight, so a pair is never auto-booked: the board says
# so, and booking_service.build_lock sends it straight to self-book.
PAIR_NOTE = "Priced as two one-way tickets — you book these yourself"


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
    # A paired return comes from its own one-way search — its leg sits in
    # the candidate's "out" bucket.
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
        "currency": out_cand.get("currency") or BOARD_CURRENCY,
        "why": why,
        "live_price": True,
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
        "supplier": "duffel",
        "sandbox": any(c.get("sandbox") for c in legs),
        "duffel_offer_id": duffel_ids[0] if len(duffel_ids) == 1 and not pair else "",
        # Both legs' ids — the detail page re-verifies each; never auto-booked
        "duffel_offer_ids": duffel_ids,
        "separate_tickets": pair,
    }


def _search(origin, dest, depart_date: str, return_date: str | None, travelers: int,
            return_from=None, home_to=None) -> list[dict] | None:
    """One Duffel offer request, board-currency candidates only — or None
    when Duffel never answered (timeout, 429/5xx after its retry, transport
    error), so callers can tell "no offers" from "no answer". The pool is
    NOT a context manager: a search that trickles past its bound is
    abandoned (shutdown without waiting), never allowed to hold the board."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(duffel_search_flights, origin[0], dest[0], depart_date, return_date, travelers,
                             return_from[0] if return_from else None, home_to[0] if home_to else None,
                             raise_errors=True)
        try:
            # _DUFFEL_TIMEOUT bounds the socket; this bounds the wait itself
            cands = future.result(timeout=_DUFFEL_TIMEOUT + 0.5)
        except Exception:
            return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [c for c in cands if (c.get("currency") or BOARD_CURRENCY) == BOARD_CURRENCY]


def real_flight_options(origin_city: str, dest_city: str, depart_date: str,
                        return_date: str, travelers: int = 1,
                        return_from_city: str | None = None,
                        return_to_city: str | None = None,
                        pace: str | None = None) -> list[dict]:
    """Up to FLIGHT_OPTIONS_PER_CITY live round-trip offers from one departure
    city, mapped to the board's flight-option schema, one per role
    (cheapest / nonstop / fastest / best balance / bags / early arrival /
    no early start / late return / one airline), all from Duffel.

    return_from_city: open-jaw trips fly home from the route's last city.
    return_to_city:   travelers who end the trip in a different home city.
    pace:             the group's pace reorders which roles claim first.
    Needs the Duffel token. Returns [] on ANY failure — callers must fall
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
            home_to = _resolve(return_to_city)
            if not home_to:
                return []  # never price a return to the wrong home city as if it were right

        open_jaw = bool(return_from_city) and (
            _split_place(return_from_city)[0].casefold() != town.casefold())
        return_from = dest
        if open_jaw:
            return_from = _resolve(return_from_city)
            if not return_from:
                # Unknown return city (lookup failed, or no airport there):
                # a plain round trip would pass the honor check below and be
                # held and charged as if it flew home from the right city.
                return []

        cands = _search(origin, dest, depart_date, return_date, travelers, return_from, home_to)
        if cands is None:
            return []  # Duffel never answered — the AI menu covers this city
        if open_jaw:
            # Offers answer the slices asked for; the check guards against a
            # return that left from somewhere the place lookup doesn't know
            cands = [c for c in cands if c["back"] and c["back"][0].get("originCode") in return_from[1]]
            if not cands:
                # Duffel answered and no airline sold the open jaw as one
                # ticket — price it as two one-way fares instead. That honors
                # both cities, but it is two separate tickets the travelers
                # book themselves (checkout does not book pairs).
                return _open_jaw_as_one_ways(origin, dest, return_from, home_to, depart_date, return_date,
                                             travelers, pace=pace, dest_city=town)

        return [_option_from(role, c, reasons=rs, match_score=ms)
                for role, c, rs, ms in _pick_diverse(cands, pace=pace, dest_city=town)]
    except Exception:
        # Live fares are an enhancement — they must never break trip planning
        return []


def _open_jaw_as_one_ways(origin, dest, return_from, home_to,
                          depart_date: str, return_date: str, travelers: int,
                          pace: str | None = None, dest_city: str = "") -> list[dict]:
    """Two Duffel one-way searches (into the first city, home from the
    last), paired per role: outbound-driven roles (early arrival, no early
    start) take their outbound pick plus the cheapest return; late return
    takes the cheapest outbound plus its return pick; one airline and bags
    need both legs to qualify; nonstop must be nonstop both ways; price/time
    roles pair role-for-role."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        out_f = pool.submit(_search, origin, dest, depart_date, None, travelers)
        back_f = pool.submit(_search, return_from, home_to, return_date, None, travelers)
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
    note = PAIR_NOTE
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
    """The option-detail payload for a flight. Live Duffel options serve their
    stored segments instantly; the fare is re-verified through GET
    /air/offers/{id} only while the quoted offer can still be alive. AI and
    sample flights — and live fares stored by an older board from another
    supplier — stay source="board" so the screen keeps rendering the board's
    own facts. Never raises: on any surprise the page still gets the stored
    facts."""
    try:
        segments = option.get("segments") or {}
        if not (option.get("live_price") and segments.get("out") and option.get("supplier") == "duffel"):
            return {"kind": "live", "source": "board"}

        details = {
            "kind": "live",
            "source": "duffel",
            "supplier": "duffel",
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
            # A one-way pair re-verifies both legs; the sum is the trip's fare
            offer_ids = option.get("duffel_offer_ids") or [option.get("duffel_offer_id") or ""]
            fresh = [duffel_get_offer(oid) for oid in offer_ids if oid]
            if fresh and all(f and (f.get("currency") or BOARD_CURRENCY) == (option.get("currency") or BOARD_CURRENCY)
                             for f in fresh):
                details["price_per_person"] = round(sum(f["price_per_person"] for f in fresh))
                details["price_fresh"] = True
        except Exception:
            pass  # stored quote + its age is the honest fallback
        return details
    except Exception:
        return {"kind": "live", "source": "board"}
