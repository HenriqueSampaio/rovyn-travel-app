"""Duffel (duffel.com) — the planning board's ONLY live flight source, search
side. Every flight Rovyn books goes through Duffel too (duffel_booking.py,
paid from the Duffel Balance), so every live fare on the board is one the
booking engine can re-price and hold. Stays are LiteAPI's alone.

  suggest_places  GET  /places/suggestions   free text -> airports and cities
                                             (flight_service resolves IATA codes)
  search_flights  POST /air/offer_requests   round trip, open jaw or one-way ->
                                             the lean candidates flight_service
                                             ranks, with duffel_offer_id
  get_offer       GET  /air/offers/{id}      the fresh price of one offer (the
                                             detail page's re-verify)

Best-effort like every live integration here: any failure returns []/None
(suggest_places raises, so a failed lookup is never cached as "no airport")
and the board falls back to the AI menu; without a token no request is ever
made. A 429 is retried once after Duffel's ratelimit-reset, a 5xx once after
a short pause — both only when the wait fits the call's own timeout.
Test tokens (duffel_test_…) serve Duffel's own test airline, "Duffel Airways"
(IATA ZZ) — options carry sandbox=True so the board can say so.
"""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

import certifi


_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_BASE = "https://api.duffel.com"
_VERSION = "v2"
_OFFER_SUPPLIER_TIMEOUT_MS = 12000  # Duffel's per-airline wait (2–60 s allowed)
FLIGHT_HTTP_TIMEOUT = 15            # the board never waits longer than this on Duffel
_OFFER_VERIFY_TIMEOUT = 10
_PLACES_TIMEOUT = 8
_MAX_PASSENGERS = 9
_RETRY_PAUSE = 1.5


def duffel_available() -> bool:
    return bool(os.environ.get("DUFFEL_ACCESS_TOKEN"))


def duffel_test_mode() -> bool:
    """Test tokens serve Duffel's own test airline ("Duffel Airways", ZZ) —
    the UI must not pass that off as a real carrier."""
    return os.environ.get("DUFFEL_ACCESS_TOKEN", "").startswith("duffel_test_")


def _retry_pause(exc: urllib.error.HTTPError) -> float:
    """How long Duffel asked us to wait (ratelimit-reset is an HTTP date),
    else a short default."""
    reset = (exc.headers or {}).get("ratelimit-reset") if exc.headers else None
    if reset:
        try:
            wait = (parsedate_to_datetime(reset) - parsedate_to_datetime(
                exc.headers.get("date") or reset)).total_seconds()
            if wait > 0:
                return wait
        except (TypeError, ValueError):
            pass
    return _RETRY_PAUSE


def _request(path: str, payload: dict | None = None, params: dict | None = None,
             timeout: int = 30) -> dict:
    """One Duffel call. Bodies are wrapped in {"data": ...} as the API
    expects. A 429 or 5xx is retried once when the pause fits the timeout."""
    url = _BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    started = time.monotonic()
    for attempt in range(2):
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
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            if attempt == 0 and exc.code in (429, 502, 503, 504):
                pause = _retry_pause(exc) if exc.code == 429 else _RETRY_PAUSE
                if time.monotonic() - started + pause < timeout:
                    time.sleep(pause)
                    continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------

def suggest_places(query: str) -> list[dict]:
    """GET /places/suggestions?query=… -> Duffel Place objects: {type
    "airport"|"city", iata_code, iata_city_code, iata_country_code, name,
    city_name, city{iata_code, airports[]}, airports[] (cities only)}.
    [] without a token or a query; RAISES on a transport failure so the
    caller's cache never stores an outage as "no airport here"."""
    if not (duffel_available() and (query or "").strip()):
        return []
    data = _request("/places/suggestions", params={"query": query.strip()}, timeout=_PLACES_TIMEOUT)
    places = data.get("data") or []
    return [p for p in places if isinstance(p, dict)] if isinstance(places, list) else []


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
    """Duffel stamps local times as '2026-11-20T07:40:00' (the airport's own
    clock, no offset) — what the board prints and ranks by."""
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
    """One key per itinerary — sorted (carrier, flight number, departure)
    tuples, so two fare brands on the same flights read as one journey."""
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
    for; the currency travels with it (no silent FX). A one-way offer has
    its single slice under "out" and no "back"."""
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
        "journey_key": "duffel:" + itinerary_signature(out + back),
        "expires_at": offer.get("expires_at") or "",
        "fare_family": slices[0].get("fare_brand_name") or "",
        "carry_on": carry,
        "checked_bag": checked,
        "supplier": "duffel",
        "sandbox": not bool(offer.get("live_mode", not duffel_test_mode())),
        "duffel_offer_id": offer.get("id") or "",
    }


def search_flights(origin: str, destination: str, depart_date: str, return_date: str | None = None,
                   travelers: int = 1, return_from: str | None = None,
                   return_to: str | None = None, raise_errors: bool = False) -> list[dict]:
    """Offers from Duffel as ranked candidates, cheapest first: a round trip,
    an open jaw (return_from / return_to differ from the outbound pair — one
    multi-slice request, as Duffel documents), or a one-way without a
    return_date. IATA airport or city codes. [] without a token, and [] on
    any failure — unless raise_errors, which lets a caller tell "Duffel
    answered with no offers" apart from a timeout, 429 or 5xx."""
    if not (duffel_available() and origin and destination and depart_date):
        return []
    n = max(1, min(int(travelers or 1), _MAX_PASSENGERS))
    slices = [{"origin": origin, "destination": destination, "departure_date": depart_date}]
    if return_date:
        slices.append({"origin": return_from or destination, "destination": return_to or origin,
                       "departure_date": return_date})
    body = {
        "slices": slices,
        "passengers": [{"type": "adult"}] * n,
        "cabin_class": "economy",
        "max_connections": 1,
    }
    try:
        data = _request("/air/offer_requests", payload=body,
                        params={"return_offers": "true", "supplier_timeout": _OFFER_SUPPLIER_TIMEOUT_MS},
                        timeout=FLIGHT_HTTP_TIMEOUT)
    except Exception:
        if raise_errors:
            raise
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

