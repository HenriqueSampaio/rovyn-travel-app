"""Live hotel data via LiteAPI (Nuitée) — Phase A of the booking architecture.

With a free LITEAPI_API_KEY (liteapi.travel, self-serve signup), the planning
board's Stay options become REAL properties with LIVE prices instead of AI
estimates. Without a key — or on any API hiccup — callers fall back to the
existing AI/sample options, so this module never breaks trip planning.

Optional companion: GOOGLE_PLACES_API_KEY enriches hotels (and any stay option)
with their official website via Google Places Text Search, and provides guest
reviews for stays that don't come from LiteAPI.
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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

# Rovyn questionnaire accommodation vote -> (core typeIds, partial typeIds)
_VOTE_TYPE_IDS = {
    "hostel": ({203, 225}, {208, 216, 218, 222}),
    "boutique hotel": ({208, 216, 218, 227}, {204}),
    "4-star hotel": ({204, 219, 231}, {206}),
    "luxury resort": ({206}, {204}),
    "airbnb/vacation rental": (
        {201, 207, 213, 219, 220, 222, 223, 228, 229, 230, 232}, {208, 216}),
}


def _type_label(hotel: dict) -> str:
    label = _TYPE_NAMES.get(hotel.get("hotelTypeId"), "hotel")
    stars = hotel.get("stars") or hotel.get("starRating")
    if label in ("hotel", "resort") and stars:
        return f"{stars}-star {label}"
    return label


def _affinity(hotel: dict, type_votes: dict | None) -> float:
    """0..1 — how well this property matches the group's accommodation votes."""
    if not type_votes:
        return 0.0
    tid = hotel.get("hotelTypeId")
    stars = hotel.get("stars") or hotel.get("starRating") or 0
    total = sum(type_votes.values()) or 1
    score = 0.0
    for vote, count in type_votes.items():
        core, partial = _VOTE_TYPE_IDS.get(vote, (set(), set()))
        match = 1.0 if tid in core else (0.5 if tid in partial else 0.0)
        if match and vote == "4-star hotel" and stars and not (3.5 <= stars <= 4.5):
            match *= 0.6
        if match and vote == "luxury resort" and tid == 204:
            match = 0.5 if stars >= 5 else 0.15
        score += (count / total) * match
    return score


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
                      group_size: int, nights: int, country_hint: str = "",
                      type_votes: dict | None = None) -> list[dict]:
    """Up to 6 real properties with live prices, mapped to Rovyn's stay-option
    schema and biased toward the group's accommodation votes (a villa-loving
    group sees villas first) while keeping price diversity.
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

        # Rank by group-vote affinity (rating breaks ties), then guarantee
        # price diversity by keeping the cheapest and priciest overall.
        scored = [(hotel, total, _affinity(hotel, type_votes)) for hotel, total in priced]
        scored.sort(key=lambda s: (-s[2], -(s[0].get("rating") or 0), s[1]))
        chosen = scored[:4]
        by_price = sorted(scored, key=lambda s: s[1])
        for extreme in (by_price[0], by_price[-1]):
            if not any(extreme[0].get("id") == c[0].get("id") for c in chosen) and len(chosen) < 6:
                chosen.append(extreme)
        chosen.sort(key=lambda s: s[1])

        options = []
        for hotel, total, affinity in chosen:
            per_person = round(total / max(group_size, 1))
            per_night = round(total / max(nights, 1))
            label = _type_label(hotel)
            why = f"Real availability for your dates — live total ${round(total)} for the group."
            if affinity >= 0.5:
                why = f"A {label} — matching the group's stay vote. Live total ${round(total)} for the group."
            options.append({
                "name": hotel.get("name", "Hotel"),
                "type": label,
                "area": hotel.get("address") or hotel.get("city") or "",
                "price_per_night": per_night,
                "total_per_person": per_person,
                "rating": round(float(hotel.get("rating")), 1) if hotel.get("rating") else None,
                "review_count": hotel.get("reviewCount"),
                "amenities": [],
                "why": why,
                "image": _hotel_image(hotel),
                "live_price": True,
                "liteapi_hotel_id": hotel.get("id"),
                "official_website": official_website(hotel.get("name", ""), destination),
            })
        return options
    except Exception:
        # Live hotel data is an enhancement — it must never break trip planning
        return []


def _liteapi_gallery(hotel_id: str, limit: int = 10) -> tuple[list[dict], float | None, int | None]:
    """(photos, rating /10, review count) from LiteAPI's hotel-details endpoint.
    The list endpoint only carries one photo; details carry the full gallery."""
    detail = (_lite_request("/data/hotel", params={"hotelId": hotel_id})).get("data") or {}
    images = detail.get("hotelImages") or []
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


def stay_media_for_options(stays: list[dict], destination: str) -> dict:
    """Photo galleries + guest reviews for every stay option, keyed by option id.

    LiteAPI properties get their real gallery and guest reviews; other stays
    fall back to Google Places reviews when that key is configured. Fetches run
    concurrently and every one is best-effort — an option simply has no entry
    when nothing could be found. Callers cache the result on the trip."""
    def fetch(opt: dict):
        photos, reviews, rating10, count = [], [], None, None
        hotel_id = opt.get("liteapi_hotel_id")
        if hotel_id and liteapi_available():
            try:
                photos, rating10, count = _liteapi_gallery(hotel_id)
            except Exception:
                pass
            try:
                reviews = _liteapi_reviews(hotel_id)
            except Exception:
                pass
        if not reviews:
            google = _google_place_reviews(opt.get("name", ""), destination)
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
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(stays))) as pool:
        return {oid: media for oid, media in pool.map(fetch, stays) if media}


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
