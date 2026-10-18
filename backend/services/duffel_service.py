"""Duffel (duffel.com) as a SECOND supply source for the planning board —
flights and stays, search only.

LiteAPI is the board's first source. With a DUFFEL_ACCESS_TOKEN set (test
tokens are free and self-serve), Duffel's offers are searched alongside and
merged in, so the group sees airlines one supplier alone would miss.
Everything maps into the exact candidate shapes the LiteAPI code already
produces, so ranking, lanes and reasons (option_ranking) never know which
supplier a row came from:

  search_flights  -> flight_service._parse_journey-shaped candidates, with
                     supplier="duffel" and duffel_offer_id (a hold order later)
  get_offer       -> the fresh price of one offer (the detail page's re-verify)
  search_stays    -> hotel_service stay records, with supplier="duffel",
                     duffel_accommodation_id and duffel_search_result_id.
                     Wired into the board only behind DUFFEL_STAYS=1 — Duffel
                     stays carry their own price and content but no LiteAPI
                     rates, so they are never routed to rooming or booking.

Best-effort like every live integration here: any failure returns []/None and
the board falls back to what LiteAPI (or the AI menu) produced; without a
token no request is ever made. Booking (hold orders, quotes) is the
payments work — intentionally not wired here.
"""
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_BASE = "https://api.duffel.com"
_VERSION = "v2"
_OFFER_SUPPLIER_TIMEOUT_MS = 12000  # Duffel's per-airline wait (2–60 s allowed)
FLIGHT_HTTP_TIMEOUT = 15            # the board never waits longer than this on Duffel
_OFFER_VERIFY_TIMEOUT = 10
_STAYS_HTTP_TIMEOUT = 20
_STAYS_RADIUS_KM = 5
_MAX_PASSENGERS = 9


def duffel_available() -> bool:
    return bool(os.environ.get("DUFFEL_ACCESS_TOKEN"))


def duffel_test_mode() -> bool:
    """Test tokens serve Duffel's own test airline and test hotels — the UI
    must not pass those off as real inventory."""
    return os.environ.get("DUFFEL_ACCESS_TOKEN", "").startswith("duffel_test_")


def duffel_stays_enabled() -> bool:
    """Duffel accommodations on the Stay tab — opt-in until their content
    and prices have been checked against real inventory."""
    return duffel_available() and os.environ.get("DUFFEL_STAYS", "").strip() in ("1", "true", "yes")


def _request(path: str, payload: dict | None = None, params: dict | None = None,
             timeout: int = 30) -> dict:
    """One Duffel call. Bodies are wrapped in {"data": ...} as the API expects."""
    url = _BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps({"data": payload}).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ.get('DUFFEL_ACCESS_TOKEN', '')}",
            "Duffel-Version": _VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Rovyn/1.0 (group trip planner)",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as res:
        return json.loads(res.read().decode())


# ---------------------------------------------------------------------------
# Flights
# ---------------------------------------------------------------------------

_ISO_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def _iso_minutes(duration: str | None) -> int:
    """'PT9H30M' -> 570. Duffel durations are ISO 8601."""
    m = _ISO_DURATION.fullmatch((duration or "").strip())
    if not m:
        return 0
    days, hours, minutes = (int(x or 0) for x in m.groups())
    return days * 24 * 60 + hours * 60 + minutes


def _plain_time(iso: str | None) -> str:
    """Duffel stamps local times as '2026-11-20T07:40:00' — the same local
    convention LiteAPI uses, so the board's formatting applies unchanged."""
    return (iso or "")[:19]


def _segment(seg: dict, direction: str) -> dict:
    carrier = seg.get("marketing_carrier") or {}
    origin = seg.get("origin") or {}
    dest = seg.get("destination") or {}
    return {
        "direction": direction,
        "originCode": origin.get("iata_code") or "?",
        "destinationCode": dest.get("iata_code") or "?",
        "originName": origin.get("name") or "",
        "destinationName": dest.get("name") or "",
        "departureTime": _plain_time(seg.get("departing_at")),
        "arrivalTime": _plain_time(seg.get("arriving_at")),
        "duration": {"minutes": _iso_minutes(seg.get("duration"))},
        "carrier": {"marketingName": carrier.get("name") or "Airline",
                    "marketingCode": carrier.get("iata_code") or ""},
        "flight": {"marketingNumber": seg.get("marketing_carrier_flight_number") or ""},
    }


def itinerary_signature(segments: list[dict]) -> str:
    """The same itinerary from two suppliers must dedupe: sorted
    (carrier, flight number, departure) tuples make a supplier-neutral key."""
    parts = sorted(
        f"{(s.get('carrier') or {}).get('marketingCode', '')}{(s.get('flight') or {}).get('marketingNumber', '')}"
        f"@{(s.get('departureTime') or '')[:16]}"
        for s in segments
    )
    return "|".join(parts)


def _baggage(slice_: dict) -> tuple[bool, bool]:
    """(carry-on included, checked bag included) from the first segment's
    passenger allowances — what the whole party gets on that fare."""
    carry, checked = False, False
    segs = slice_.get("segments") or []
    for p in (segs[0].get("passengers") or []) if segs else []:
        for b in p.get("baggages") or []:
            if (b.get("quantity") or 0) > 0:
                if b.get("type") == "checked":
                    checked = True
                elif b.get("type") == "carry_on":
                    carry = True
        break
    return carry, checked


def _offer_to_candidate(offer: dict, travelers: int) -> dict | None:
    """One Duffel offer -> the lean candidate shape flight_service ranks.
    Per-person price = the offer total over the passengers it was priced
    for; the currency travels with it (no silent FX)."""
    slices = offer.get("slices") or []
    if not slices:
        return None
    try:
        total = float(offer.get("total_amount"))
    except (TypeError, ValueError):
        return None
    out = [_segment(s, "OUTBOUND") for s in (slices[0].get("segments") or [])]
    back = [_segment(s, "INBOUND") for s in (slices[1].get("segments") or [])] if len(slices) > 1 else []
    if not out:
        return None
    passengers = len(offer.get("passengers") or []) or max(int(travelers or 1), 1)
    carry, checked = _baggage(slices[0])
    carriers = list(dict.fromkeys(s["carrier"]["marketingName"] for s in out + back))
    return {
        "out": out,
        "back": back,
        "price": round(total / passengers, 2),
        "currency": offer.get("total_currency") or "USD",
        "out_minutes": _iso_minutes(slices[0].get("duration")) or sum(s["duration"]["minutes"] for s in out),
        "back_minutes": (_iso_minutes(slices[1].get("duration")) if len(slices) > 1 else 0)
                        or sum(s["duration"]["minutes"] for s in back),
        "stops": max(len(out) - 1, (len(back) - 1) if back else 0),
        "carriers": carriers,
        "offer_id": "",  # LiteAPI offer ids only — the booking paths tell them apart
        "journey_key": "duffel:" + itinerary_signature(out + back),
        "expires_at": offer.get("expires_at") or "",
        "fare_family": slices[0].get("fare_brand_name") or "",
        "carry_on": carry,
        "checked_bag": checked,
        "supplier": "duffel",
        "sandbox": not bool(offer.get("live_mode", not duffel_test_mode())),
        "duffel_offer_id": offer.get("id") or "",
    }


def search_flights(origin: str, destination: str, depart_date: str, return_date: str,
                   travelers: int = 1, return_from: str | None = None,
                   return_to: str | None = None) -> list[dict]:
    """Round-trip (or open-jaw) offers from Duffel as ranked candidates,
    cheapest first. IATA airport or metro codes. [] on any failure or
    without a token — the LiteAPI search carries the board alone then."""
    if not (duffel_available() and origin and destination and depart_date and return_date):
        return []
    n = max(1, min(int(travelers or 1), _MAX_PASSENGERS))
    body = {
        "slices": [
            {"origin": origin, "destination": destination, "departure_date": depart_date},
            {"origin": return_from or destination, "destination": return_to or origin,
             "departure_date": return_date},
        ],
        "passengers": [{"type": "adult"}] * n,
        "cabin_class": "economy",
        "max_connections": 1,
    }
    try:
        data = _request("/air/offer_requests", payload=body,
                        params={"return_offers": "true", "supplier_timeout": _OFFER_SUPPLIER_TIMEOUT_MS},
                        timeout=FLIGHT_HTTP_TIMEOUT)
    except Exception:
        return []
    offers = (data.get("data") or {}).get("offers") or []
    cands = [c for c in (_offer_to_candidate(o, n) for o in offers if isinstance(o, dict)) if c]
    cands.sort(key=lambda c: c["price"])
    return cands


def get_offer(offer_id: str) -> dict | None:
    """GET /air/offers/{id} -> {"price_per_person", "currency", "expires_at"}
    or None. Offers expire, so this mostly succeeds right after a search."""
    if not (duffel_available() and offer_id):
        return None
    try:
        data = _request(f"/air/offers/{urllib.parse.quote(offer_id)}", timeout=_OFFER_VERIFY_TIMEOUT)
        offer = data.get("data") or {}
        total = float(offer.get("total_amount"))
        passengers = len(offer.get("passengers") or []) or 1
        return {"price_per_person": round(total / passengers, 2),
                "currency": offer.get("total_currency") or "USD",
                "expires_at": offer.get("expires_at") or ""}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stays
# ---------------------------------------------------------------------------

_TYPE_WORDS = (  # a label from the name — Duffel has no property-type field
    ("hostel", "hostel"), ("apartments", "apartment"), ("apartment", "apartment"),
    ("villa", "villa"), ("resort", "resort"), ("b&b", "bed & breakfast"),
    ("bed & breakfast", "bed & breakfast"), ("guest house", "guest house"),
    ("guesthouse", "guest house"), ("aparthotel", "aparthotel"), ("residence", "residence"),
    ("loft", "apartment"), ("suites", "aparthotel"), ("inn", "inn"), ("riad", "riad"),
    ("palace", "hotel"), ("hotel", "hotel"),
)


def _type_label(name: str, stars) -> str:
    lab = "hotel"
    low = (name or "").casefold()
    for word, label in _TYPE_WORDS:
        if word in low:
            lab = label
            break
    try:
        stars_f = float(stars) if stars is not None else 0.0
    except (TypeError, ValueError):
        stars_f = 0.0
    if lab in ("hotel", "resort") and stars_f:
        shown = int(stars_f) if stars_f.is_integer() else stars_f
        return f"{shown}-star {lab}"
    return lab


def _result_to_record(result: dict) -> dict | None:
    """One Duffel stays search result -> a provider-neutral stay record,
    priced from the search itself (cheapest rate for every room requested).
    Non-USD results are dropped: a price must compare like for like."""
    acc = result.get("accommodation") or {}
    name = (acc.get("name") or "").strip()
    if not name:
        return None
    currency = result.get("cheapest_rate_currency") or "USD"
    if currency != "USD":
        return None
    try:
        total = float(result.get("cheapest_rate_total_amount"))
    except (TypeError, ValueError):
        return None
    loc = acc.get("location") or {}
    coords = loc.get("geographic_coordinates") or {}
    addr = loc.get("address") or {}
    score = acc.get("review_score")
    rating10 = None
    if score is not None:
        try:
            s = float(score)
            rating10 = round(s * 2, 1) if s <= 5 else round(s, 1)
        except (TypeError, ValueError):
            rating10 = None
    photos = [p.get("url") for p in (acc.get("photos") or []) if p.get("url")]
    stars = acc.get("rating")
    facilities = [a.get("description") or a.get("type") or "" for a in (acc.get("amenities") or [])]
    description = (acc.get("description") or "").strip()
    address = ", ".join(p for p in (addr.get("line_one"), addr.get("city_name")) if p)
    return {
        "key": f"duffel:{acc.get('id') or result.get('id') or name}",
        "name": name,
        "type_id": None,
        "type_label": _type_label(name, stars),
        "stars": stars,
        "rating10": rating10,
        "review_count": acc.get("review_count"),
        "lat": coords.get("latitude"),
        "lng": coords.get("longitude"),
        "price_total": total,
        "currency": currency,
        "facilities": [f for f in facilities if f],
        "description": description[:400],
        "address": address,
        "image": {"url": photos[0], "thumb": photos[0], "alt": name, "credit": name, "credit_url": ""}
                 if photos else None,
        "supplier": "duffel",
        "sandbox": duffel_test_mode(),
        "duffel_accommodation_id": acc.get("id"),
        "duffel_search_result_id": result.get("id"),
        # The option page for a Duffel stay is built from this very payload
        # (no second endpoint to call), cached on the option like live details
        "details": {
            "kind": "live",
            "source": "duffel",
            "description": description,
            "address": address,
            "facilities": [f for f in facilities if f],
            "photos": [{"url": u, "caption": ""} for u in photos[:10]],
            "star_rating": stars,
            **({"lat": coords.get("latitude"), "lng": coords.get("longitude")}
               if coords.get("latitude") is not None and coords.get("longitude") is not None else {}),
        },
    }


def search_stays(lat: float, lng: float, check_in: str, check_out: str,
                 rooms: int, guests: int, radius_km: int = _STAYS_RADIUS_KM) -> list[dict]:
    """Accommodations around a point for the group's dates, as stay records
    priced for the whole party. [] on any failure or without a token."""
    if not (duffel_available() and check_in and check_out and lat is not None and lng is not None):
        return []
    body = {
        "rooms": max(1, int(rooms or 1)),
        "guests": [{"type": "adult"}] * max(1, int(guests or 1)),
        "check_in_date": check_in,
        "check_out_date": check_out,
        "location": {"radius": radius_km,
                     "geographic_coordinates": {"latitude": lat, "longitude": lng}},
    }
    try:
        data = _request("/stays/search", payload=body, timeout=_STAYS_HTTP_TIMEOUT)
    except Exception:
        return []
    results = (data.get("data") or {}).get("results") or []
    return [r for r in (_result_to_record(x) for x in results if isinstance(x, dict)) if r]
