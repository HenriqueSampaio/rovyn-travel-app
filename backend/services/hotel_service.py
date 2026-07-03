"""Live hotel data via LiteAPI (Nuitée) — Phase A of the booking architecture.

With a free LITEAPI_API_KEY (liteapi.travel, self-serve signup), the planning
board's Stay options become REAL properties with LIVE prices instead of AI
estimates. Without a key — or on any API hiccup — callers fall back to the
existing AI/sample options, so this module never breaks trip planning.

Optional companion: GOOGLE_PLACES_API_KEY enriches hotels (and any stay option)
with their official website via Google Places Text Search.
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi
import pycountry

from backend.services.links import safe_url

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_LITE_BASE = "https://api.liteapi.travel/v3.0"


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


def _lite_request(path: str, payload: dict | None = None, params: dict | None = None):
    url = _LITE_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
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
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as res:
        return json.loads(res.read().decode())


def _split_place(destination: str) -> tuple[str, str]:
    """'Hvar, Croatia' -> ('Hvar', 'Croatia'); single names return ('X', '')."""
    parts = [p.strip() for p in destination.split(",") if p.strip()]
    city = parts[0] if parts else destination
    country = parts[-1] if len(parts) > 1 else ""
    return city, country


def _hotel_image(h: dict) -> dict | None:
    url = h.get("main_photo") or h.get("thumbnail")
    if not url:
        imgs = h.get("hotelImages") or []
        url = imgs[0].get("url") if imgs else None
    if not url:
        return None
    return {"url": url, "thumb": url, "alt": h.get("name", "hotel"),
            "credit": h.get("name", ""), "credit_url": ""}


def real_stay_options(destination: str, check_in: str, check_out: str,
                      group_size: int, nights: int, country_hint: str = "") -> list[dict]:
    """Up to 4 real hotels with live prices, mapped to Rovyn's stay-option schema.
    Returns [] on any failure — callers must fall back to AI/sample options."""
    if not liteapi_available():
        return []
    try:
        city, country = _split_place(destination)
        code = _country_code(country) or _country_code(country_hint)
        if not code:
            # LiteAPI city search requires an ISO-2 countryCode; without a
            # resolvable country we can't search reliably — use AI stays.
            return []
        data = _lite_request("/data/hotels", params={
            "cityName": city, "countryCode": code, "limit": 40,
        })
        hotels = data.get("data") or []
        if not hotels:
            return []
        by_id = {h.get("id"): h for h in hotels if h.get("id")}

        # Two travelers per room, rounding UP so everyone has a bed
        rooms_needed = max(1, (group_size + 1) // 2)
        rates = _lite_request("/hotels/rates", payload={
            "hotelIds": list(by_id.keys()),
            "checkin": check_in,
            "checkout": check_out,
            "currency": "USD",
            "guestNationality": "US",
            "occupancies": [{"adults": 2}] * rooms_needed,
        })
        priced = []
        for entry in rates.get("data") or []:
            hotel = by_id.get(entry.get("hotelId"))
            if not hotel:
                continue
            # offerRetailRate is the WHOLE offer (all requested rooms summed,
            # per LiteAPI docs) — per-occupancy rate totals must not be mixed in
            offers = [
                float((rt.get("offerRetailRate") or {}).get("amount"))
                for rt in entry.get("roomTypes") or []
                if (rt.get("offerRetailRate") or {}).get("amount")
            ]
            if offers:
                priced.append((hotel, min(offers)))
        if not priced:
            return []

        # Spread across price tiers: cheapest, two middle picks by rating, priciest
        priced.sort(key=lambda p: p[1])
        picks = {0, len(priced) - 1}
        middle = sorted(range(1, len(priced) - 1),
                        key=lambda i: -(priced[i][0].get("rating") or priced[i][0].get("starRating") or 0))
        for i in middle:
            if len(picks) >= 4:
                break
            picks.add(i)

        options = []
        for i in sorted(picks):
            hotel, total = priced[i]
            per_person = round(total / max(group_size, 1))
            per_night = round(total / max(nights, 1))
            stars = hotel.get("starRating") or hotel.get("stars")
            options.append({
                "name": hotel.get("name", "Hotel"),
                "type": f"{stars}-star hotel" if stars else "hotel",
                "area": hotel.get("address") or hotel.get("city") or "",
                "price_per_night": per_night,
                "total_per_person": per_person,
                "rating": round(float(hotel.get("rating")), 1) if hotel.get("rating") else None,
                "amenities": [],
                "why": f"Real availability for your dates — live total ${round(total)} for the group.",
                "image": _hotel_image(hotel),
                "live_price": True,
                "liteapi_hotel_id": hotel.get("id"),
                "official_website": official_website(hotel.get("name", ""), destination),
            })
        return options
    except Exception:
        # Live hotel data is an enhancement — it must never break trip planning
        return []


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
