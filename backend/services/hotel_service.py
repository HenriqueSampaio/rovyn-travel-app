"""Live hotel data via LiteAPI (Nuitée) — Phase A of the booking architecture.

With a free LITEAPI_API_KEY (liteapi.travel, self-serve signup), the planning
board's Stay options become REAL properties with LIVE prices instead of AI
estimates. Without a key — or on any API hiccup — callers fall back to the
existing AI/sample options, so this module never breaks trip planning.

Optional companion: GOOGLE_PLACES_API_KEY enriches hotels (and any stay option)
with their official website via Google Places Text Search, and provides guest
reviews for stays that don't come from LiteAPI.

This module also builds the option-detail screens: LiteAPI's hotel page +
live room offers for real stays, Google Places facts (hours, address,
reviews, photos) for visits, events and AI stays — see real_option_details.
"""
import html
import http.client
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import certifi
import pycountry

from backend.services import option_ranking as ranking
from backend.services.duffel_service import duffel_stays_enabled, search_stays as duffel_search_stays
from backend.services.links import safe_url
from backend.services.maps_service import geocode_place, maps_available, travel_from_center

_log = logging.getLogger(__name__)
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_LITE_BASE = "https://api.liteapi.travel/v3.0"

# One process-wide pacer for EVERY LiteAPI call — board searches, flight
# searches, galleries, reviews and detail pages all share the key's rate
# limit (5 req/s on sandbox keys, 250 on production): a token bucket at
# LITEAPI_RATE_PER_S (default 8/s; 4/s on a sandbox key) that holds at most
# ONE token, so no one-second window ever sees more than rate + 1 calls. It
# is a rate, not a concurrency cap: a 45 s flight search in progress must
# never queue a stay search behind it (the stay search has a deadline).
_bucket_lock = threading.Lock()
_bucket = {"tokens": 0.0, "at": 0.0, "rate": 0.0}
_BUCKET_CAPACITY = 1.0
_clock = time.monotonic  # seams the tests swap for a fake clock and a recording sleep
_sleep = time.sleep


def liteapi_sandbox() -> bool:
    """Sandbox keys (sand_…) serve test inventory at 5 req/s."""
    return os.environ.get("LITEAPI_API_KEY", "").startswith("sand_")


def _lite_rate_per_s() -> float:
    try:
        configured = float(os.environ.get("LITEAPI_RATE_PER_S") or 0)
    except ValueError:
        configured = 0.0
    if configured > 0:
        return configured
    return 4.0 if liteapi_sandbox() else 8.0


def _take_token(deadline_at: float | None = None) -> None:
    """Block until the bucket has a token — the pacing half of the throttle.
    With a deadline, a wait that would overrun it raises TimeoutError
    instead of queuing a call whose answer nobody will read."""
    while True:
        with _bucket_lock:
            rate = _lite_rate_per_s()
            now = _clock()
            if _bucket["rate"] != rate:  # first call, or the key changed
                _bucket.update(tokens=_BUCKET_CAPACITY, at=now, rate=rate)
            _bucket["tokens"] = min(_BUCKET_CAPACITY, _bucket["tokens"] + (now - _bucket["at"]) * rate)
            _bucket["at"] = now
            if _bucket["tokens"] >= 1:
                _bucket["tokens"] -= 1
                return
            wait_s = (1 - _bucket["tokens"]) / rate
        if deadline_at is not None and now + wait_s >= deadline_at:
            raise TimeoutError("LiteAPI pacing would overrun the search deadline")
        _sleep(min(wait_s, 1.0))


def _country_code(country: str) -> str | None:
    """'Croatia' -> 'HR'; LiteAPI requires ISO-2 countryCode for city search."""
    if not country:
        return None
    if len(country) == 2 and country.isalpha():
        return country.upper()
    try:
        return pycountry.countries.lookup(country).alpha_2
    except LookupError:
        try:
            matches = pycountry.countries.search_fuzzy(country)
            return matches[0].alpha_2 if matches else None
        except LookupError:
            return None


def liteapi_available() -> bool:
    return bool(os.environ.get("LITEAPI_API_KEY"))


def places_available() -> bool:
    return bool(os.environ.get("GOOGLE_PLACES_API_KEY"))


def _lite_request(path: str, payload: dict | None = None, params: dict | None = None,
                  timeout: int = 20, attempts: int = 3, deadline_at: float | None = None):
    """One LiteAPI call (hotels or flights — same base, key and etiquette).

    Flight-rate searches routinely take 10s+, so they pass a longer timeout;
    business errors that retrying can't fix (like a sold-out offer on
    /flights/verify) pass attempts=1. A call made under a search deadline
    passes deadline_at: once it has passed, nothing is sent."""
    url = _LITE_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    # Several cities and top-ups run concurrently under the token bucket
    # above; this backoff covers the occasional 429/5xx or dropped
    # connection that still slips through, instead of silently degrading a
    # whole city to AI estimates.
    last_err = None
    for attempt in range(attempts):
        if attempt:
            _sleep(1.2 * attempt)
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TimeoutError("search deadline passed before the call was sent")
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "X-API-Key": os.environ.get("LITEAPI_API_KEY", ""),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Rovyn/1.0 (group trip planner)",
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            _take_token(deadline_at)
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as res:
                raw = res.read().decode()
                # A 204 "no availability found" (error code 2001) is an
                # empty answer, not a transport failure
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise  # 4xx like a bad request won't improve with retries
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException,
                ConnectionError, json.JSONDecodeError) as exc:
            # Dropped keep-alives (RemoteDisconnected), resets and truncated
            # bodies are as transient as a 503 — retried the same way
            last_err = exc
    raise last_err


def _retry_after_seconds(exc: urllib.error.HTTPError, default: float = 1.5) -> float:
    try:
        return max(0.0, float((exc.headers or {}).get("Retry-After") or default))
    except (TypeError, ValueError):
        return default


def _split_place(destination: str) -> tuple[str, str]:
    """'Hvar, Croatia' -> ('Hvar', 'Croatia'); single names return ('X', '')."""
    parts = [p.strip() for p in destination.split(",") if p.strip()]
    city = parts[0] if parts else destination
    country = parts[-1] if len(parts) > 1 else ""
    return city, country


# LiteAPI hotelTypeId -> human label (from GET /data/hotelTypes)
_TYPE_NAMES = {
    201: "apartment", 203: "hostel", 204: "hotel", 205: "motel", 206: "resort",
    207: "residence", 208: "bed & breakfast", 209: "ryokan", 210: "farm stay",
    212: "holiday park", 213: "villa", 214: "campsite", 215: "boat stay",
    216: "guest house", 218: "inn", 219: "aparthotel", 220: "holiday home",
    221: "lodge", 222: "homestay", 223: "country house", 224: "luxury tent",
    225: "capsule hotel", 227: "riad", 228: "chalet", 229: "condo",
    230: "cottage", 231: "economy hotel", 232: "gite",
}

def _type_label(hotel: dict) -> str:
    """'4-star hotel', 'villa', 'hostel'… from LiteAPI's type id and stars."""
    label = _TYPE_NAMES.get(hotel.get("hotelTypeId"), "hotel")
    return ranking.star_label(label, hotel.get("stars") or hotel.get("starRating"))


def _hotel_image(h: dict) -> dict | None:
    url = h.get("main_photo") or h.get("thumbnail")
    if not url:
        imgs = h.get("hotelImages") or []
        url = imgs[0].get("url") if imgs else None
    if not url:
        return None
    return {"url": url, "thumb": url, "alt": h.get("name", "hotel"),
            "credit": h.get("name", ""), "credit_url": ""}


# ---------------------------------------------------------------------------
# The wide stay search — docs/OPTIONS-BREADTH-SPEC.md
# ---------------------------------------------------------------------------

# Provider ceilings, one named constant each (docs.liteapi.travel, 2026-10-18)
_LITE_LIST_LIMIT = 5000        # GET /data/hotels `limit`: default 200, max 5000
_LITE_RATES_LIMIT = 5000       # POST /hotels/rates `limit`: default 200, max 5000
_LITE_RATES_BATCH = 50         # hotelIds per rates call on the id-batched fallback path (no documented max)
_LITE_RATES_TIMEOUT = 10       # LiteAPI's own live-rate cutoff in seconds (docs recommend 6–12)
_LITE_RATES_MAX_PER_HOTEL = 1  # the wide pass needs each hotel's cheapest offer, nothing more
_LITE_LIST_HTTP_TIMEOUT = 8
_LITE_RATES_HTTP_TIMEOUT = 20
_TOP_UP_FLOOR = 8              # fewer core matches than this for a voted style -> one filtered top-up
_TOP_UP_LIMIT = 40
_MAX_TOP_UPS = 2
_RERUN_BATCH = 50              # the one re-price of unpriced top-affinity hotels when pricing was thin
_FACILITIES_TTL = 24 * 3600
_FACILITIES_RETRY = 300


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        value = default
    return max(value, 0)


def stay_candidates(per_city: bool = False) -> int:
    """How many properties to price before choosing — the net's width."""
    default = 60 if per_city else 150
    configured = _env_int("STAY_CANDIDATES", 150)
    return min(configured if not per_city else min(configured, default), _LITE_LIST_LIMIT)


def stay_options_target(per_city: bool = False) -> int:
    """How many stays the board shows (a route city gets fewer)."""
    if per_city:
        return _env_int("STAY_OPTIONS_PER_CITY", 5)
    return _env_int("STAY_OPTIONS_SINGLE", 12)


def stay_search_deadline() -> float:
    """One absolute wall-time budget for a city's whole stay search, seconds."""
    try:
        return max(float(os.environ.get("STAY_SEARCH_DEADLINE_S") or 25), 1.0)
    except ValueError:
        return 25.0


_facilities_lock = threading.Lock()
_facilities = {"at": 0.0, "failed_at": 0.0, "names": {}}


def facility_names() -> dict[int, str]:
    """facility_id -> English name from GET /data/facilities, cached per
    process for a day. The hotel list carries facilityIds; joined with this
    map they become the facts behind a "Pool & spa" reason — without a
    per-hotel details call. {} when the catalog can't be fetched (then the
    vibe dimension simply stays out of the score)."""
    now = time.monotonic()
    with _facilities_lock:
        if _facilities["names"] and now - _facilities["at"] < _FACILITIES_TTL:
            return _facilities["names"]
        if _facilities["failed_at"] and now - _facilities["failed_at"] < _FACILITIES_RETRY:
            return _facilities["names"]
    names: dict[int, str] = {}
    try:
        data = _lite_request("/data/facilities", timeout=_LITE_LIST_HTTP_TIMEOUT, attempts=1)
        for f in data.get("data") or []:
            if f.get("facility_id") is not None and f.get("facility"):
                names[int(f["facility_id"])] = str(f["facility"])
    except Exception:
        names = {}
    with _facilities_lock:
        if names:
            _facilities.update(at=now, names=names, failed_at=0.0)
        else:
            _facilities["failed_at"] = now
        return _facilities["names"]


def _rates_payload(check_in: str, check_out: str, rooms_needed: int, **location) -> dict:
    """The body every rates call in the wide search shares; `location` is one
    of the documented main queries (countryCode+cityName, or hotelIds[]),
    optionally narrowed by hotelTypeIds[]."""
    return {
        "checkin": check_in,
        "checkout": check_out,
        "currency": "USD",
        "guestNationality": "US",
        "occupancies": [{"adults": 2}] * rooms_needed,
        "timeout": _LITE_RATES_TIMEOUT,
        "maxRatesPerHotel": _LITE_RATES_MAX_PER_HOTEL,
        **location,
    }


def _cheapest_offers(rates: dict) -> dict[str, float]:
    """hotelId -> cheapest offerRetailRate. The offer rate is the WHOLE offer
    (all requested rooms, the whole stay, per LiteAPI docs) — per-occupancy
    rate totals must never be mixed in."""
    priced: dict[str, float] = {}
    for entry in (rates or {}).get("data") or []:
        hid = entry.get("hotelId")
        offers = []
        for rt in entry.get("roomTypes") or []:
            amount = (rt.get("offerRetailRate") or {}).get("amount")
            if amount:
                try:
                    offers.append(float(amount))
                except (TypeError, ValueError):
                    continue
        if hid and offers:
            priced[hid] = min(priced.get(hid, float("inf")), min(offers))
    return priced


def _rates_call(payload: dict, deadline_at: float) -> tuple[dict[str, float], list[dict]]:
    """(cheapest offer per hotel, hotels[] summary block). One attempt; a 429
    is retried once after Retry-After when the deadline allows. Past the
    deadline nothing is sent — the answer could not be used anyway."""
    for attempt in range(2):
        if time.monotonic() >= deadline_at:
            raise TimeoutError("search deadline passed")
        try:
            rates = _lite_request("/hotels/rates", payload=payload, timeout=_LITE_RATES_HTTP_TIMEOUT,
                                  attempts=1, deadline_at=deadline_at)
            return _cheapest_offers(rates), (rates or {}).get("hotels") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                pause = _retry_after_seconds(exc)
                if time.monotonic() + pause < deadline_at:
                    _sleep(pause)
                    continue
            raise
    raise TimeoutError("search deadline passed")


def _settle(future, deadline_at: float, stats: dict, kind: str):
    """A future's result within what is left of the deadline, or None —
    counting failures and truncation so the board can say what it saw."""
    if future is None:
        return None
    try:
        return future.result(timeout=max(0.0, deadline_at - time.monotonic()))
    except TimeoutError:
        stats["truncated"] = True
        return None
    except Exception:
        if kind == "rates":
            stats["batches_failed"] = stats.get("batches_failed", 0) + 1
        return None


def _hotel_from_rates_block(h: dict | None) -> dict | None:
    """The hotels[] summary a rates search returns, in the list's shape — the
    fallback identity for a priced hotel the content list didn't include."""
    if not h or not h.get("id"):
        return None
    return {
        "id": h["id"], "name": h.get("name") or "Hotel", "main_photo": h.get("main_photo"),
        "thumbnail": h.get("thumbnail"), "address": h.get("address"), "city": h.get("city_name"),
        "latitude": h.get("latitude"), "longitude": h.get("longitude"), "rating": h.get("rating"),
        "stars": h.get("stars"), "reviewCount": h.get("review_count"), "hotelTypeId": None,
        "facilityIds": [],
    }


def _stay_record(hotel: dict, total: float, group_size: int, nights: int,
                 center, facilities: dict[int, str]) -> dict:
    """A priced hotel as the provider-neutral record option_ranking scores."""
    lat, lng = hotel.get("latitude"), hotel.get("longitude")
    distance = ranking.distance_km(center[0], center[1], lat, lng) if center and lat is not None else None
    rating10 = None
    if hotel.get("rating"):
        try:
            rating10 = round(float(hotel["rating"]), 1)
        except (TypeError, ValueError):
            rating10 = None
    names = [facilities[i] for i in (hotel.get("facilityIds") or []) if i in facilities]
    return {
        "key": hotel["id"],
        "name": hotel.get("name") or "Hotel",
        "type_id": hotel.get("hotelTypeId"),
        "type_label": _type_label(hotel),
        "stars": hotel.get("stars") or hotel.get("starRating"),
        "rating10": rating10,
        "review_count": hotel.get("reviewCount"),
        "lat": lat,
        "lng": lng,
        "distance_km": distance,
        "price_total": float(total),
        "per_person_total": float(total) / max(int(group_size or 1), 1),
        "nights": max(int(nights or 1), 1),
        "facilities": names,
        "description": _strip_html(hotel.get("hotelDescription") or "")[:600],
        "group_size": group_size,
        "image": _hotel_image(hotel),
        "address": hotel.get("address") or hotel.get("city") or "",
    }


def _stay_option(rec: dict) -> dict:
    """The board's stay-option schema (every existing field kept) plus the
    picked-for-you fields: lane, match_score, reasons, supplier, why. A
    Splurge arrives with its over-ceiling line already leading (the lane
    picker owns that)."""
    reasons = list(rec.get("reasons") or [])
    lane = rec.get("lane")
    nights = max(int(rec.get("nights") or 1), 1)
    supplier = rec.get("supplier") or "liteapi"
    option = {
        "name": rec["name"],
        "type": rec["type_label"],
        "area": rec.get("address") or "",
        "price_per_night": round(rec["price_total"] / nights),
        "total_per_person": round(rec["per_person_total"]),
        "rating": rec.get("rating10"),
        "rating10": rec.get("rating10"),
        "rating_scale": "10" if rec.get("rating10") is not None else None,
        "review_count": rec.get("review_count"),
        "amenities": list(rec.get("facilities") or [])[:6],
        "why": ranking.why_line(lane, reasons),
        "image": rec.get("image"),
        "live_price": True,
        # The official site is healed lazily on the option page (trips.py
        # read_option_details) — a Places call per candidate at board time
        # would cost more than the whole hotel search.
        "official_website": None,
        "lane": lane,
        "match_score": rec.get("match_score"),
        "reasons": reasons,
        "supplier": supplier,
        "sandbox": bool(rec.get("sandbox")) if supplier == "duffel" else liteapi_sandbox(),
        "currency": rec.get("currency") or "USD",
        "distance_km": rec.get("distance_km"),
        "lat": rec.get("lat"),
        "lng": rec.get("lng"),
    }
    if supplier == "duffel":
        # Priced and described by Duffel's search alone: not on the LiteAPI
        # rooming/booking paths (no liteapi_hotel_id), and its option page is
        # the search payload
        option.update({
            "bookable": False,
            "duffel_accommodation_id": rec.get("duffel_accommodation_id"),
            "duffel_search_result_id": rec.get("duffel_search_result_id"),
            "duffel_details": rec.get("details"),
        })
    else:
        option["liteapi_hotel_id"] = rec["key"]
    return option


def _same_property(a: dict, b: dict) -> bool:
    """The same hotel from two suppliers: matching distinctive name words
    and, when both have coordinates, within 150 m of each other."""
    if not _names_match(a.get("name") or "", b.get("name") or ""):
        return False
    if a.get("lat") is None or b.get("lat") is None:
        return True
    d = ranking.distance_km(a["lat"], a["lng"], b["lat"], b["lng"])
    return d is not None and d <= 0.15


def _core_count(hotels: list[dict], vote: str) -> int:
    """How many listed hotels the voted-style lane would accept as a core
    match — the same rule the lane applies (type id AND, for the four-star
    vote, the star band), so the top-up decision cannot drift from it."""
    return sum(1 for h in hotels
               if ranking.is_core_match(vote, h.get("hotelTypeId"), "", h.get("stars") or h.get("starRating")))


def _fetch_priced_hotels(city: str, code: str, destination: str, check_in: str, check_out: str,
                         group_size: int, nights: int, votes: dict, want: int, per_city: bool,
                         stats: dict) -> list[dict]:
    """Everything that talks to a supplier, under one deadline: the content
    list, the by-city rates search, the voted-style top-ups, the facilities
    catalog, the city center, the id-batch fallback, the thin re-price, the
    content fetch for top-up hotels and (behind its flag) Duffel stays.
    Returns provider-neutral records ready to score; `stats` says what the
    search saw. Work is never submitted once the deadline has passed."""
    candidates = stay_candidates(per_city)
    rooms_needed = max(1, (group_size + 1) // 2)  # two per room, rounding up
    deadline_at = time.monotonic() + stay_search_deadline()
    payload = lambda **loc: _rates_payload(check_in, check_out, rooms_needed, **loc)  # noqa: E731

    def in_time() -> bool:
        return time.monotonic() < deadline_at

    priced: dict[str, float] = {}
    block: dict[str, dict] = {}

    def absorb(result) -> None:
        if not result:
            return
        found, summary = result
        for hid, total in found.items():
            priced[hid] = min(priced.get(hid, float("inf")), total)
        for h in summary:
            if h.get("id"):
                block.setdefault(h["id"], h)

    pool = ThreadPoolExecutor(max_workers=8)
    try:
        content_f = pool.submit(_lite_request, "/data/hotels", params={
            "cityName": city, "countryCode": code, "limit": candidates,
        }, timeout=_LITE_LIST_HTTP_TIMEOUT, attempts=1, deadline_at=deadline_at)
        rates_f = pool.submit(_rates_call, payload(
            countryCode=code, cityName=city, limit=min(candidates, _LITE_RATES_LIMIT)), deadline_at)
        facilities_f = pool.submit(facility_names)
        center_f = pool.submit(geocode_place, destination) if maps_available() else None

        content = _settle(content_f, deadline_at, stats, "list") or {}
        hotels = [h for h in (content.get("data") or []) if h.get("id")]
        by_id = {h["id"]: h for h in hotels}
        stats["listed"] = len(hotels)
        center = _settle(center_f, deadline_at, stats, "geocode") if center_f else None
        # Duffel accommodations join only on live Duffel Stays access
        # (DUFFEL_STAYS=1): test mode serves synthetic test hotels only
        duffel_f = None
        if duffel_stays_enabled() and center and in_time():
            duffel_f = pool.submit(duffel_search_stays, center[0], center[1], check_in, check_out,
                                   rooms_needed, group_size)

        # Styles the group voted for but the first page barely carries (by
        # the lane's own definition of a core match): one filtered top-up
        # each, two at most, priced right away
        under = []
        for vote, _count in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0])):
            core = ranking.VOTE_TYPE_IDS.get(vote, (set(), set()))[0]
            if core and _core_count(hotels, vote) < _TOP_UP_FLOOR:
                under.append(sorted(core))
        top_up_fs = [pool.submit(_rates_call, payload(
            countryCode=code, cityName=city, hotelTypeIds=core, limit=_TOP_UP_LIMIT), deadline_at)
            for core in under[:_MAX_TOP_UPS] if in_time()]
        stats["top_ups"] = len(top_up_fs)

        main = _settle(rates_f, deadline_at, stats, "rates")
        absorb(main)
        main_priced = bool(main and main[0])
        for f in top_up_fs:
            absorb(_settle(f, deadline_at, stats, "rates"))

        if not main_priced and hotels and in_time():
            # The by-city rates search answered nothing (a 204, a timeout,
            # a sandbox quirk) — price the content list by id batches
            # instead, the path every board used before. Top-ups may have
            # priced a few styles already; the batches cover the rest.
            stats["mode"] = "ids"
            ids = [hid for hid in by_id if hid not in priced]
            batches = [ids[i:i + _LITE_RATES_BATCH] for i in range(0, len(ids), _LITE_RATES_BATCH)]
            for f in [pool.submit(_rates_call, payload(hotelIds=batch), deadline_at) for batch in batches]:
                absorb(_settle(f, deadline_at, stats, "rates"))

        floor = max(4, want // 3)
        if 0 < len(priced) < floor and hotels and in_time():
            # Thin pricing: one re-price of the unpriced hotels the group
            # would most want, before the board pads with estimates
            def affinity(h):
                return ranking.style_affinity(votes, h.get("hotelTypeId"), "", h.get("stars"))["score"]
            unpriced = sorted((h for h in hotels if h["id"] not in priced),
                              key=lambda h: (-affinity(h), -(h.get("rating") or 0)))[:_RERUN_BATCH]
            if unpriced:
                absorb(_settle(pool.submit(_rates_call, payload(
                    hotelIds=[h["id"] for h in unpriced]), deadline_at), deadline_at, stats, "rates"))

        missing = [hid for hid in priced if hid not in by_id][:200]
        if missing and in_time():
            # Priced by the city search but absent from the content page —
            # one direct content fetch so types and facilities are known
            extra = _settle(pool.submit(_lite_request, "/data/hotels", params={
                "countryCode": code, "hotelIds": ",".join(missing), "limit": len(missing),
            }, timeout=_LITE_LIST_HTTP_TIMEOUT, attempts=1, deadline_at=deadline_at),
                deadline_at, stats, "list") or {}
            for h in extra.get("data") or []:
                if h.get("id"):
                    by_id.setdefault(h["id"], h)

        facilities = _settle(facilities_f, deadline_at, stats, "facilities") or {}
        duffel_records = (_settle(duffel_f, deadline_at, stats, "duffel") or []) if duffel_f else []
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    stats["priced"] = len(priced)
    records = []
    for hid, total in priced.items():
        hotel = by_id.get(hid) or _hotel_from_rates_block(block.get(hid))
        if hotel:
            records.append(_stay_record(hotel, total, group_size, nights, center, facilities))
    added = 0
    for rec in duffel_records:
        if any(_same_property(rec, known) for known in records):
            continue  # LiteAPI already carries it — and can price and detail it
        rec = dict(rec)
        rec["per_person_total"] = rec["price_total"] / max(int(group_size or 1), 1)
        rec["nights"] = max(int(nights or 1), 1)
        rec["group_size"] = group_size
        if center and rec.get("lat") is not None:
            rec["distance_km"] = ranking.distance_km(center[0], center[1], rec["lat"], rec["lng"])
        records.append(rec)
        added += 1
    stats["duffel"] = added
    return records


def _rank_records(records: list[dict], prefs: dict, want: int) -> list[dict]:
    """The pure half: score every record, claim lanes, build options. No
    supplier can make this fail — a failure here is a bug worth seeing."""
    votes = {k: v for k, v in (prefs.get("accommodation_votes") or {}).items() if v}
    band = prefs.get("lodging_band")
    scored = [dict(rec, **ranking.score_stay(rec, prefs, rec.get("supplier") or "liteapi"))
              for rec in records]
    chosen = ranking.pick_stay_lanes(scored, want, votes, band, allow_splurge=ranking.splurge_allowed(prefs))
    return [_stay_option(rec) for rec in chosen]


def real_stay_options(destination: str, check_in: str, check_out: str,
                      group_size: int, nights: int, country_hint: str = "",
                      type_votes: dict | None = None, *, prefs: dict | None = None,
                      target: int | None = None, per_city: bool = False,
                      report: dict | None = None) -> list[dict]:
    """Up to `target` real properties with live prices, each with a lane and
    data-backed reasons it was picked for THIS group.

    Under one wall-time deadline (STAY_SEARCH_DEADLINE_S) and the shared
    LiteAPI pacer, concurrently: the content list (types, facilities, guest
    scores, coordinates), one rates search by city (cheapest offer per hotel,
    up to STAY_CANDIDATES hotels), up to two rates top-ups filtered to voted
    styles the first page under-serves, the facilities catalog and the city
    center. Thin pricing gets one re-price of the best unpriced hotels; an
    empty by-city rates answer falls back to id batches. Whatever is not
    back by the deadline is dropped and reported in `report`.

    `prefs` (accommodation_votes, lodging_band, top_vibes, group_size…) is the
    group's profile; `type_votes` only fills the votes when prefs lacks them.
    Returns [] when no supplier answered — callers fall back to the AI/sample
    menu."""
    stats = report if isinstance(report, dict) else {}
    stats.update({"listed": 0, "priced": 0, "batches_failed": 0, "truncated": False,
                  "top_ups": 0, "mode": "city", "duffel": 0})
    if not liteapi_available():
        return []
    city, country = _split_place(destination)
    code = _country_code(country) or _country_code(country_hint)
    if not code:
        # LiteAPI city search requires an ISO-2 countryCode; without a
        # resolvable country we can't search reliably — use AI stays.
        return []
    prefs = dict(prefs or {})
    prefs.setdefault("accommodation_votes", type_votes or {})
    prefs.setdefault("group_size", group_size)
    votes = {k: v for k, v in (prefs.get("accommodation_votes") or {}).items() if v}
    want = int(target or stay_options_target(per_city))
    try:
        records = _fetch_priced_hotels(city, code, destination, check_in, check_out, group_size,
                                       nights, votes, want, per_city, stats)
    except Exception:
        # Live hotel data is an enhancement — it must never break trip planning
        return []
    if not records:
        return []
    try:
        return _rank_records(records, prefs, want)
    except Exception:
        _log.warning("stay ranking failed for %s", destination, exc_info=True)
        return []


_HOTEL_PAGE_TTL = 3600
_hotel_pages_lock = threading.Lock()
_hotel_pages: dict[str, tuple[float, dict]] = {}


def _hotel_page(hotel_id: str) -> dict:
    """GET /data/hotel, memoized per hotel for an hour. The gallery (stay
    media) and the property page (option details) both need it, and a
    board's first open asks for both within seconds — one call, not two."""
    now = time.monotonic()
    with _hotel_pages_lock:
        hit = _hotel_pages.get(hotel_id)
        if hit and now - hit[0] < _HOTEL_PAGE_TTL:
            return hit[1]
    detail = (_lite_request("/data/hotel", params={"hotelId": hotel_id})).get("data") or {}
    with _hotel_pages_lock:
        _hotel_pages[hotel_id] = (now, detail)
        if len(_hotel_pages) > 500:  # a bounded cache, oldest out
            for key in sorted(_hotel_pages, key=lambda k: _hotel_pages[k][0])[:100]:
                _hotel_pages.pop(key, None)
    return detail


def _liteapi_gallery(hotel_id: str, limit: int = 10) -> tuple[list[dict], float | None, int | None]:
    """(photos, rating /10, review count) from LiteAPI's hotel-details endpoint.
    The list endpoint only carries one photo; details carry the full gallery."""
    detail = _hotel_page(hotel_id)
    images = list(detail.get("hotelImages") or [])
    images.sort(key=lambda i: (not i.get("defaultImage"),
                               i["order"] if isinstance(i.get("order"), (int, float)) else 10_000))
    photos, seen = [], set()
    for img in images:
        url = safe_url(img.get("url") or img.get("urlHd"))
        if not url or url in seen:
            continue
        seen.add(url)
        photos.append({"url": url, "caption": (img.get("caption") or "").strip()})
        if len(photos) >= limit:
            break
    rating = detail.get("rating")
    return photos, (round(float(rating), 1) if rating else None), detail.get("reviewCount")


def _liteapi_reviews(hotel_id: str, limit: int = 6) -> list[dict]:
    """Recent guest reviews from LiteAPI, mapped to Rovyn's review shape."""
    data = _lite_request("/data/reviews", params={"hotelId": hotel_id, "limit": limit, "timeout": 8})
    reviews = []
    for r in data.get("data") or []:
        headline = (r.get("headline") or "").strip()
        pros = (r.get("pros") or "").strip()
        cons = (r.get("cons") or "").strip()
        if not (headline or pros or cons):
            continue
        score = r.get("averageScore")
        reviews.append({
            "score10": round(float(score), 1) if score is not None else None,
            "headline": headline,
            "pros": pros,
            "cons": cons,
            "author": (r.get("name") or "").strip() or "A guest",
            "date": (r.get("date") or "")[:10],
            "traveler_type": (r.get("type") or "").replace("_", " "),
            "source": "liteapi",
        })
    return reviews


def _google_place_reviews(name: str, place: str) -> dict | None:
    """Aggregate rating + up to 5 guest reviews via Google Places Text Search —
    the review source for stays that didn't come from LiteAPI. None when the
    key is missing, the place is unknown, or anything fails."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key or not name:
        return None
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": f"{name} {place}".strip()}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "places.rating,places.userRatingCount,places.reviews",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        places = data.get("places") or []
        if not places:
            return None
        top = places[0]
        reviews = []
        for r in top.get("reviews") or []:
            text = ((r.get("text") or {}).get("text") or "").strip()
            if not text:
                continue
            reviews.append({
                # Google rates /5 — normalize to /10 so the UI shows one scale
                "score10": round(float(r["rating"]) * 2, 1) if r.get("rating") else None,
                "headline": "",
                "pros": "",
                "cons": "",
                "text": text,
                "author": (r.get("authorAttribution") or {}).get("displayName") or "A guest",
                "date": (r.get("publishTime") or "")[:10],
                "traveler_type": "",
                "source": "google",
            })
        rating = top.get("rating")
        return {
            "rating10": round(float(rating) * 2, 1) if rating else None,
            "review_count": top.get("userRatingCount"),
            "reviews": reviews[:5],
        }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def stay_media_for_options(stays: list[dict], destination: str) -> tuple[dict, bool]:
    """Photo galleries + guest reviews for every stay option, keyed by option id.

    LiteAPI properties get their real gallery and guest reviews; other stays
    fall back to Google Places reviews when that key is configured. Fetches run
    concurrently (three stays at a time — a 12-stay board must not burst the
    key's rate limit) and every one is best-effort — an option simply has no
    entry when nothing could be found. Returns (media, complete): complete is
    False when LiteAPI throttled a fetch, so callers must NOT cache that
    partial answer on the trip and will try again on the next open."""
    throttled = threading.Event()

    def fetch(opt: dict):
        photos, reviews, rating10, count = [], [], None, None
        hotel_id = opt.get("liteapi_hotel_id")
        if hotel_id and liteapi_available():
            # Gallery and reviews are independent LiteAPI calls — overlap them
            with ThreadPoolExecutor(max_workers=2) as inner:
                gallery_f = inner.submit(_liteapi_gallery, hotel_id)
                reviews_f = inner.submit(_liteapi_reviews, hotel_id)
                try:
                    photos, rating10, count = gallery_f.result()
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        throttled.set()
                except Exception:
                    pass
                try:
                    reviews = reviews_f.result()
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        throttled.set()
                except Exception:
                    pass
        if not reviews:
            # Multi-city stays carry their own city — search reviews there
            google = _google_place_reviews(opt.get("name", ""), opt.get("city") or destination)
            if google:
                reviews = google["reviews"]
                rating10 = rating10 or google["rating10"]
                count = count or google["review_count"]
        if not (photos or reviews):
            return opt.get("id"), None
        return opt.get("id"), {
            "photos": photos,
            "reviews": reviews,
            "rating10": rating10 if rating10 is not None else opt.get("rating"),
            "review_count": count if count is not None else opt.get("review_count"),
        }

    stays = [s for s in stays if s.get("id")]
    if not stays:
        return {}, True
    with ThreadPoolExecutor(max_workers=min(3, len(stays))) as pool:
        media = {oid: m for oid, m in pool.map(fetch, stays) if m}
    return media, not throttled.is_set()


def official_website(name: str, place: str) -> str | None:
    """The venue's own website via Google Places Text Search; None when unknown."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key or not name:
        return None
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": f"{name} {place}".strip()}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "places.websiteUri,places.displayName",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        places = data.get("places") or []
        return safe_url(places[0].get("websiteUri")) if places else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None


_NAME_STOPWORDS = {
    "hotel", "hotels", "the", "a", "an", "of", "and", "de", "del", "della",
    "di", "da", "la", "le", "les", "el", "los", "das", "der", "at", "by",
    "hostel", "resort", "inn", "suites", "suite", "apartments", "apartment",
    "aparthotel", "bb", "guesthouse", "guest", "house", "boutique",
}


def _names_match(a: str, b: str) -> bool:
    """Do two property names plausibly describe the same place? Places Text
    Search answers SOMETHING for almost any query, so before trusting its
    links for a hotel we require real overlap between the distinctive words
    ("Lutetia", "Excelsior") of both names — brand suffixes and rebrandings
    still pass, a completely different property doesn't."""
    def tokens(s: str) -> set[str]:
        return {t for t in re.split(r"[^a-z0-9]+", s.casefold()) if len(t) > 1}

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    shorter = min(ta, tb, key=len)
    overlap = ta & tb
    return (len(overlap) * 2 >= len(shorter)) and bool(overlap - _NAME_STOPWORDS)


def places_stay_links(name: str, place: str) -> dict | None:
    """Official website + Google Maps link (+ coordinates) for a stay that
    already has its facts from LiteAPI — the two links LiteAPI never carries.

    {} means Google answered but nothing was trustworthy: no result, or the
    best match looked like a different property (a wrong link is worse than
    no link — callers may cache the verdict). None means we couldn't ask
    (missing key / network) — callers should try again another time."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key or not name:
        return None
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": f"{name} {place}".strip()}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask":
                    "places.displayName,places.websiteUri,places.googleMapsUri,places.location",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        places = data.get("places") or []
        if not places:
            return {}
        top = places[0]
        found_name = ((top.get("displayName") or {}).get("text") or "").strip()
        if not _names_match(name, found_name):
            return {}
        location = top.get("location") or {}
        out = {
            "website": safe_url(top.get("websiteUri")),
            "maps_url": safe_url(top.get("googleMapsUri")),
        }
        out = {k: v for k, v in out.items() if v}
        if location.get("latitude") is not None and location.get("longitude") is not None:
            out["lat"] = location["latitude"]
            out["lng"] = location["longitude"]
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Option-detail screens — live facts only, no AI anywhere below this line
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """LiteAPI hotel descriptions arrive as HTML — flatten to clean paragraphs."""
    if not text:
        return ""
    text = re.sub(r"(?i)</p\s*>|<br\s*/?>", "\n", text)
    text = html.unescape(_TAG_RE.sub(" ", text))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _liteapi_stay_page(hotel_id: str) -> dict:
    """The property's own page data: description, address, check-in times,
    facilities. Photos are NOT fetched here — the stay-media endpoint already
    carries the gallery and the frontend reuses it (same memoized page)."""
    detail = _hotel_page(hotel_id)
    times = detail.get("checkinCheckoutTimes") or {}
    country = detail.get("country") or ""
    out = {
        "description": _strip_html(detail.get("hotelDescription") or ""),
        "address": ", ".join(filter(None, [
            detail.get("address"), detail.get("city"),
            country.upper() if len(country) == 2 else country,
        ])),
        "check_in": times.get("checkin") or "",
        "check_out": times.get("checkout") or "",
        "facilities": detail.get("hotelFacilities") or [],
        "star_rating": detail.get("starRating"),
        "phone": detail.get("phone") or "",
    }
    out = {k: v for k, v in out.items() if v}
    # The property's exact coordinates — the location map's pin. LiteAPI
    # nests them under "location"; keep reading top-level too, just in case.
    loc = detail.get("location") or {}
    lat = loc.get("latitude", detail.get("latitude"))
    lng = loc.get("longitude", detail.get("longitude"))
    if lat is not None and lng is not None:
        out["lat"] = lat
        out["lng"] = lng
    return out


_QUICK_RATES_TIMEOUT = 5      # the option page's re-quote: one short socket timeout…
_QUICK_REFRESH_WAIT_S = 5.5   # …and the longest the page waits for it before serving the cached sheet


def _liteapi_room_offers(hotel_id: str, check_in: str, check_out: str,
                         group_size: int, nights: int, quick: bool = False) -> dict | None:
    """Live bookable room offers for the trip's dates — the Booking.com-style
    room table. Prices come straight from LiteAPI's rates endpoint, which
    occasionally 200s with no data for a hotel that does have availability,
    so an empty answer is retried once before believing it. `quick` is the
    option page's re-quote: one attempt, one short timeout, because the page
    already has a cached sheet to show and must not wait on a fresh price."""
    if not (check_in and check_out):
        return None
    rooms_needed = max(1, (group_size + 1) // 2)
    best: dict[tuple, dict] = {}
    for attempt in range(1 if quick else 2):
        if attempt:
            time.sleep(0.8)
        rates = _lite_request("/hotels/rates", payload={
            "hotelIds": [hotel_id],
            "checkin": check_in,
            "checkout": check_out,
            "currency": "USD",
            "guestNationality": "US",
            "occupancies": [{"adults": 2}] * rooms_needed,
        }, timeout=_QUICK_RATES_TIMEOUT if quick else 20, attempts=1 if quick else 3)
        for entry in rates.get("data") or []:
            for rt in entry.get("roomTypes") or []:
                # offerRetailRate = ALL requested rooms for the WHOLE stay
                total = (rt.get("offerRetailRate") or {}).get("amount")
                if not total:
                    continue
                total = float(total)
                rate = (rt.get("rates") or [{}])[0]
                name = (rate.get("name") or "").strip() or "Room"
                board = (rate.get("boardName") or "").strip()
                refundable = ((rate.get("cancellationPolicies") or {}).get("refundableTag")) == "RFN"
                key = (name.casefold(), board.casefold(), refundable)
                if key not in best or total < best[key]["total"]:
                    best[key] = {
                        "name": name,
                        "board": board,
                        "refundable": refundable,
                        "sleeps": rate.get("maxOccupancy") or rate.get("adultCount"),
                        "total": round(total),
                        "per_room_per_night": round(total / max(nights, 1) / rooms_needed),
                    }
        if best:
            break
    if not best:
        return None
    offers = sorted(best.values(), key=lambda o: o["total"])[:8]
    return {"rooms_needed": rooms_needed, "room_offers": offers}


def refresh_room_offers(option: dict, context: dict) -> dict | None:
    """Fresh room offers for an already-cached LiteAPI stay — prices are the
    one part of a detail page that should not be served stale. One quick
    attempt, bounded by _QUICK_REFRESH_WAIT_S: the page already has its
    cached sheet, so a slow or failed re-quote returns None and the sheet is
    served unchanged. Never blocks the page on a fresh price."""
    hotel_id = option.get("liteapi_hotel_id")
    if not (hotel_id and liteapi_available()):
        return None
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            _liteapi_room_offers, hotel_id, context.get("check_in") or "", context.get("check_out") or "",
            context.get("group_size", 2), context.get("nights", 1), True,
        )
        return future.result(timeout=_QUICK_REFRESH_WAIT_S)
    except Exception:
        return None  # the cached sheet is served unchanged
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


_PLACE_DETAIL_FIELDS = ",".join(f"places.{f}" for f in (
    "displayName", "editorialSummary", "formattedAddress", "rating",
    "userRatingCount", "regularOpeningHours.weekdayDescriptions",
    "websiteUri", "googleMapsUri", "internationalPhoneNumber",
    "reviews", "photos", "location",
))


def _places_photo_urls(names: list[str], key: str, limit: int = 6) -> list[dict]:
    """Resolve Places photo resource names to plain image URLs server-side, so
    the API key never reaches the browser."""
    names = [n for n in names if n][:limit]
    if not names:
        return []

    def fetch(name: str) -> str | None:
        url = f"https://places.googleapis.com/v1/{name}/media?maxHeightPx=900&skipHttpRedirect=true"
        req = urllib.request.Request(url, headers={"X-Goog-Api-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
                return safe_url(json.loads(res.read().decode()).get("photoUri"))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(6, len(names))) as pool:
        return [{"url": u, "caption": ""} for u in pool.map(fetch, names) if u]


def place_details(name: str, place: str) -> dict | None:
    """Everything Google Places knows about one venue: summary, address,
    opening hours, rating, reviews, website, photos. None when the key is
    missing, the place is unknown, or anything fails."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key or not name:
        return None
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": f"{name} {place}".strip()}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": _PLACE_DETAIL_FIELDS,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        places = data.get("places") or []
        if not places:
            return None
        top = places[0]
        reviews = []
        for r in top.get("reviews") or []:
            text = ((r.get("text") or {}).get("text") or "").strip()
            if not text:
                continue
            reviews.append({
                # Google rates /5 — normalize to /10 so the UI shows one scale
                "score10": round(float(r["rating"]) * 2, 1) if r.get("rating") else None,
                "headline": "",
                "pros": "",
                "cons": "",
                "text": text,
                "author": (r.get("authorAttribution") or {}).get("displayName") or "A visitor",
                "date": (r.get("publishTime") or "")[:10],
                "traveler_type": "",
                "source": "google",
            })
        rating = top.get("rating")
        location = top.get("location") or {}
        out = {
            "summary": ((top.get("editorialSummary") or {}).get("text") or "").strip(),
            "address": top.get("formattedAddress") or "",
            "hours": (top.get("regularOpeningHours") or {}).get("weekdayDescriptions") or [],
            "website": safe_url(top.get("websiteUri")),
            "maps_url": safe_url(top.get("googleMapsUri")),
            "phone": top.get("internationalPhoneNumber") or "",
            "rating10": round(float(rating) * 2, 1) if rating else None,
            "review_count": top.get("userRatingCount"),
            "photos": _places_photo_urls([p.get("name") for p in top.get("photos") or []], key),
            "reviews": reviews[:5],
        }
        out = {k: v for k, v in out.items() if v}
        # Coordinates feed the location map on the option-detail screen
        if location.get("latitude") is not None and location.get("longitude") is not None:
            out["lat"] = location["latitude"]
            out["lng"] = location["longitude"]
        return out or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def real_option_details(category: str, option: dict, context: dict) -> dict:
    """Live facts for ONE board option — the option-detail screen's data.

    stay with liteapi_hotel_id -> LiteAPI hotel page + live room offers
    stay (AI/sample)           -> Google Places lookup of the property
    visits / events            -> Google Places (hours, address, reviews…)
    transport                  -> no live source wired yet: source="board",
                                  the screen shows the board's own facts
    (flights never reach here — the router serves them via
     flight_service.live_flight_details)

    Every branch degrades to {"source": "board"} instead of raising, so the
    detail screen always renders something."""
    details: dict = {"kind": "live", "source": "board"}
    where = option.get("city") or context.get("destination") or ""

    def with_center_chip(d: dict) -> dict:
        """"12 min walk from the center" — computed once the venue has
        coordinates, cached with the rest of the details. Needs the Routes
        API; silently absent until that's enabled on the key."""
        if d.get("lat") is not None and where:
            try:
                chip = travel_from_center(d["lat"], d["lng"], where)
            except Exception:
                chip = None
            if chip:
                d["from_center"] = chip
        return d

    if category == "stay":
        if option.get("duffel_accommodation_id") and isinstance(option.get("duffel_details"), dict):
            # A Duffel accommodation's page is the search payload itself —
            # photos, amenities, description — nothing else to fetch
            details.update(option["duffel_details"])
            details["source"] = "duffel"
            return with_center_chip(details)
        hotel_id = option.get("liteapi_hotel_id")
        if hotel_id and liteapi_available():
            # Page, room offers and Google links are independent — overlap them
            with ThreadPoolExecutor(max_workers=3) as pool:
                page_f = pool.submit(_liteapi_stay_page, hotel_id)
                offers_f = pool.submit(
                    _liteapi_room_offers, hotel_id,
                    context.get("check_in") or "", context.get("check_out") or "",
                    context.get("group_size", 2), context.get("nights", 1),
                )
                links_f = pool.submit(places_stay_links, option.get("name", ""), where)
                try:
                    page = page_f.result()
                except Exception:
                    page = {}
                try:
                    offers = offers_f.result()
                except Exception:
                    offers = None
                try:
                    links = links_f.result() or {}
                except Exception:
                    links = {}
            if page or offers:
                details.update(page)
                details.update(offers or {})
                # LiteAPI never carries the property's own website or a Maps
                # link — Google fills those gaps; LiteAPI wins where both know
                for k, v in links.items():
                    details.setdefault(k, v)
                details["source"] = "liteapi"
                return with_center_chip(details)
        found = place_details(option.get("name", ""), where)
        if found:
            details.update(found)
            details["source"] = "places"
            return with_center_chip(details)
        return details

    if category in ("visits", "events"):
        found = place_details(option.get("name", ""), where)
        if found:
            details.update(found)
            details["source"] = "places"
            return with_center_chip(details)
        return details

    return details
