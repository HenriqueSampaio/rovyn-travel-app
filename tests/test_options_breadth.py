"""Wide, explained stay & flight options — docs/OPTIONS-BREADTH-SPEC.md.

Runs with plain python (no pytest needed):

    /Users/henriquesampaio/Rovyn/Rovyn/.venv/bin/python tests/test_options_breadth.py

Isolated like tests/test_maps_and_links.py: env pinned before backend
imports, a temp SQLite + map cache, and a fake urllib layer that RECORDS every
outbound call and raises on anything unstubbed — the services' catch-alls
would otherwise turn an unstubbed call into [] silently, so every scenario
also asserts CALLS["unexpected"] is empty — and that no LiteAPI flight
endpoint and no Duffel stays endpoint is ever called: flights are Duffel's
alone, stays LiteAPI's alone. Fixtures come from
tests/fixtures/*.json; every number asserted below is a documented formula
over those fixture fields. Hung fakes wait on an Event the scenario releases,
never on a fixed sleep.
"""
import io
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures"

os.environ.update({
    "DATABASE_URL": "",
    "SUPABASE_URL": "",
    "SUPABASE_ANON_KEY": "",
    "ALLOW_LEGACY_AUTH": "1",
    "ANTHROPIC_API_KEY": "",
    "GEMINI_API_KEY": "",
    "PEXELS_API_KEY": "",
    "LITEAPI_API_KEY": "sand_test_key",
    "DUFFEL_ACCESS_TOKEN": "duffel_test_token",
    "GOOGLE_PLACES_API_KEY": "test-key",
    "GOOGLE_MAPS_EMBED_KEY": "",
    "LITEAPI_RATE_PER_S": "1000",
    "STAY_SEARCH_DEADLINE_S": "20",
    "STAY_OPTIONS_SINGLE": "12",
    "STAY_OPTIONS_PER_CITY": "5",
    "FLIGHT_OPTIONS_PER_CITY": "8",
    "STAY_CANDIDATES": "150",
})

TMP = Path(tempfile.mkdtemp(prefix="rovyn-breadth-test-"))
import backend.db as db  # noqa: E402
db._DB_PATH = TMP / "rovyn.db"
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
import backend.main  # noqa: E402
from backend.services import ai_service, booking_service, duffel_booking, duffel_service, flight_service  # noqa: E402
from backend.services import hotel_service, option_ranking as ranking  # noqa: E402
from backend.services.flight_service import live_flight_details, real_flight_options  # noqa: E402
from backend.services.hotel_service import real_stay_options  # noqa: E402

client = TestClient(backend.main.app)


def load(name):
    return json.loads((FIX / name).read_text())


LIST = load("liteapi_hotels_list.json")
VILLAS = load("liteapi_hotels_villas.json")
FACILITIES = load("liteapi_facilities.json")
RATES = load("liteapi_rates_wide.json")
DUFFEL_OFFERS = load("duffel_offers.json")       # NYC <-> ROM round trips, 16 offers for a party of four
DUFFEL_HOME = load("duffel_offers_home.json")    # FLR -> NYC one-ways (the open jaw's way home)
# The multi-city board flies home from Venice: the same six one-ways, leaving VCE
HOME_BY_CODE = {"FLR": DUFFEL_HOME, "VCE": json.loads(json.dumps(DUFFEL_HOME).replace('"FLR"', '"VCE"'))}
PLACES = load("duffel_places.json")              # /places/suggestions answers by query, documented shape
PLACES["venice"] = {"data": [{"id": "arp_vce_it", "type": "airport", "iata_code": "VCE", "iata_city_code": None,
                              "iata_country_code": "IT", "name": "Venice Marco Polo Airport", "city_name": "Venice",
                              "city": None, "airports": None, "time_zone": "Europe/Rome"}]}
ALL_HOTELS = {h["id"]: h for h in LIST["data"] + VILLAS["data"]}
CENTER = (41.8933, 12.4829)
GEOCODES = {"rome, italy": CENTER, "florence, italy": (43.7700, 11.2577), "venice, italy": (45.4404, 12.3160)}

# ---------------------------------------------------------------------------
# Fake network: LiteAPI (stays), Duffel (flights) and Google, every call recorded
# ---------------------------------------------------------------------------

MODES = {
    "city": "normal",           # normal | empty (204) | thin (two hotels) | 429 (one throttle, then normal)
    "batch": "normal",          # normal | boom (400 on the lp-boom batch) | hang | thin
    "duffel": "normal",         # normal | hang | empty | 429 (one throttle, then normal)
    "duffel_currency": "USD",   # one currency for every offer, or {offer id: currency}
    "duffel_live": False,       # offers answer live_mode=true (a live token)
    "open_jaw": "none",         # none (no airline sells it as one ticket) | sold
    "gallery_429": False,
    "rates_hang_ids": set(),    # hotelIds whose rates call hangs (the option page's re-quote)
    "hang": threading.Event(),  # hung fakes wait on this, bounded by HANG_SECONDS
    "throttled": set(),         # one-shot 429s that already fired
}
HANG_SECONDS = 6.0
CALLS: dict[str, list] = {}
EVER_FORBIDDEN: list[str] = []  # never reset: the whole run's LiteAPI-flight / Duffel-stays calls


def reset_calls():
    for key in ("lite_list", "lite_rates", "lite_facilities", "duffel_places", "duffel_offers",
                "duffel_offer_get", "geocode", "places", "gallery", "reviews", "routes", "forbidden", "unexpected"):
        CALLS[key] = []


reset_calls()


def hang():
    MODES["hang"].wait(HANG_SECONDS)


def arm_hang():
    MODES["hang"] = threading.Event()


def release_hang():
    MODES["hang"].set()
    time.sleep(0.05)  # let the released fake threads return before the next scenario counts calls


class _Resp(io.BytesIO):
    status = 200
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json(obj):
    return _Resp(json.dumps(obj).encode())


def _http_error(url, code, body=b"{}", headers=None):
    return urllib.error.HTTPError(url, code, "err", headers or {}, io.BytesIO(body))


def _rates_response(ids):
    data = []
    for hid in ids:
        if hid not in RATES["prices"]:
            continue
        data.append({"hotelId": hid, "roomTypes": [{
            "offerId": f"off-{hid}",
            "offerRetailRate": {"amount": RATES["prices"][hid], "currency": "USD"},
            "rates": [{"name": "Double", "maxOccupancy": 2, "boardName": "Room only",
                       "cancellationPolicies": {"refundableTag": "RFN"}}],
        }]})
    return {"data": data, "sandbox": True, "guestLevel": 1,
            "hotels": [RATES["hotels_block"][hid] for hid in ids if hid in RATES["hotels_block"]]}


def _offers(payload, offers=None):
    """A fresh copy of an offer-request answer, its offers in the scenario's
    currency and mode."""
    payload = json.loads(json.dumps(payload))
    if offers is not None:
        payload["data"]["offers"] = offers
    for o in payload["data"]["offers"]:
        cur = MODES["duffel_currency"]
        o["total_currency"] = cur if isinstance(cur, str) else cur.get(o["id"], "USD")
        o["live_mode"] = MODES["duffel_live"]
    return payload


def _one_way(slice_):
    """A one-slice request: home from Florence answers the FLR fixture; the
    outbound answers the round trips' outbound slices, priced at 55 %."""
    if slice_["origin"] in HOME_BY_CODE:
        return _offers(HOME_BY_CODE[slice_["origin"]])
    out = []
    for o in DUFFEL_OFFERS["data"]["offers"]:
        oo = json.loads(json.dumps(o))
        oo["id"] = o["id"] + "_ow"
        oo["slices"] = o["slices"][:1]
        oo["total_amount"] = f"{round(float(o['total_amount']) / 4 * 0.55) * 4:.2f}"
        out.append(oo)
    return _offers(DUFFEL_OFFERS, out)


def _open_jaw(home="FLR"):
    """NYC -> ROM out, FLR (or VCE) -> NYC home as one ticket: the first six
    round trips' outbound slices sold with the six returns."""
    if MODES["open_jaw"] != "sold":
        return _offers(DUFFEL_OFFERS, [])
    sold = []
    for o, h in zip(DUFFEL_OFFERS["data"]["offers"], HOME_BY_CODE[home]["data"]["offers"]):
        oo = json.loads(json.dumps(o))
        oo["id"] = f"{o['id']}_{h['id'][4:]}"
        oo["slices"] = [o["slices"][0], h["slices"][0]]
        oo["total_amount"] = f"{round(float(o['total_amount']) * 0.55 + float(h['total_amount'])):.2f}"
        sold.append(oo)
    return _offers(DUFFEL_OFFERS, sold)


# Flights are Duffel's alone and stays LiteAPI's alone: these must never be called
FORBIDDEN = ("https://api.liteapi.travel/v3.0/flights/", "https://api.liteapi.travel/v3.0/data/flights/",
             "https://api.duffel.com/stays/")


def fake_urlopen(req, timeout=None, context=None):
    url = req.full_url if hasattr(req, "full_url") else req
    body = {}
    if hasattr(req, "data") and req.data:
        body = json.loads(req.data.decode())
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}

    if url.startswith("https://api.liteapi.travel/v3.0/data/hotels"):
        CALLS["lite_list"].append(qs)
        if qs.get("hotelIds"):
            ids = qs["hotelIds"].split(",")
            rows = [ALL_HOTELS[i] for i in ids if i in ALL_HOTELS]
            return _json({"data": rows, "hotelIds": [h["id"] for h in rows], "total": len(rows)})
        if qs.get("hotelTypeIds"):
            types = {int(t) for t in qs["hotelTypeIds"].split(",")}
            rows = [h for h in ALL_HOTELS.values() if h["hotelTypeId"] in types]
            return _json({"data": rows, "hotelIds": [h["id"] for h in rows], "total": len(rows)})
        return _json(LIST)
    if url.startswith("https://api.liteapi.travel/v3.0/data/facilities"):
        CALLS["lite_facilities"].append(url)
        return _json(FACILITIES)
    if url.startswith("https://api.liteapi.travel/v3.0/hotels/rates"):
        CALLS["lite_rates"].append(body)
        ids = body.get("hotelIds")
        if ids:
            if MODES["batch"] == "boom" and "lp-boom" in ids:
                raise _http_error(url, 400, b'{"error":{"code":4000,"message":"bad request"}}')
            if (MODES["batch"] == "hang" and "lp-boom" in ids) or (MODES["rates_hang_ids"] & set(ids)):
                hang()
            if MODES["batch"] == "thin":
                ids = [i for i in ids if i in ("lp-r01", "lp-r02")]
            return _json(_rates_response(ids))
        if body.get("hotelTypeIds"):
            types = set(body["hotelTypeIds"])
            ids = [hid for hid, t in RATES["types"].items() if t in types][: body.get("limit", 40)]
            if MODES["city"] == "thin":
                ids = [i for i in ids if i in ("lp-r01", "lp-r02")]
            return _json(_rates_response(ids))
        if MODES["city"] == "empty":
            return _Resp(b"")  # 204 — "no availability found"
        if MODES["city"] == "429" and "city" not in MODES["throttled"]:
            MODES["throttled"].add("city")
            raise _http_error(url, 429, b'{"error":{"message":"Too many requests."}}', {"Retry-After": "1"})
        ids = [h["id"] for h in LIST["data"]][: body.get("limit", 200)]
        if MODES["city"] == "thin":
            ids = ids[:2]
        return _json(_rates_response(ids))
    if url.startswith(FORBIDDEN):
        CALLS["forbidden"].append(url)
        EVER_FORBIDDEN.append(url)
        raise AssertionError(f"forbidden outbound URL in test: {url}")
    if url.startswith("https://api.liteapi.travel/v3.0/data/reviews"):
        CALLS["reviews"].append(qs)
        return _json({"data": []})
    if url.startswith("https://api.liteapi.travel/v3.0/data/hotel?"):
        CALLS["gallery"].append(qs)
        if MODES["gallery_429"] and qs.get("hotelId") == "lp-r11":
            raise _http_error(url, 429, b'{"error":{"message":"Too many requests."}}', {"Retry-After": "1"})
        return _json({"data": {"hotelImages": [{"url": f"https://img.example/{qs.get('hotelId')}-1.jpg",
                                                "defaultImage": True, "order": 1}],
                               "rating": 8.5, "reviewCount": 100}})
    if url.startswith("https://api.duffel.com/places/suggestions"):
        CALLS["duffel_places"].append(qs.get("query"))
        return _json(PLACES.get((qs.get("query") or "").casefold(), {"data": []}))
    if url.startswith("https://api.duffel.com/air/offer_requests"):
        CALLS["duffel_offers"].append(body)
        if MODES["duffel"] == "hang":
            hang()
        if MODES["duffel"] == "429" and "duffel" not in MODES["throttled"]:
            MODES["throttled"].add("duffel")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            raise _http_error(url, 429, b'{"errors":[{"code":"rate_limit_exceeded"}]}',
                              {"date": format_datetime(now), "ratelimit-reset": format_datetime(now + timedelta(seconds=1))})
        slices = body["data"]["slices"]
        if MODES["duffel"] == "empty":
            return _json(_offers(DUFFEL_OFFERS, []))
        if len(slices) == 1:
            return _json(_one_way(slices[0]))
        if slices[1]["origin"] in HOME_BY_CODE:
            return _json(_open_jaw(slices[1]["origin"]))
        return _json(_offers(DUFFEL_OFFERS))
    if url.startswith("https://api.duffel.com/air/offers/"):
        CALLS["duffel_offer_get"].append(url)
        return _json({"data": {"id": url.rsplit("/", 1)[1], "total_amount": "1488.00", "total_currency": "USD",
                               "passengers": [{"id": f"pas_{i}"} for i in range(1, 5)],
                               "expires_at": "2099-01-01T00:00:00Z"}})
    if url.startswith("https://maps.googleapis.com/maps/api/geocode/json"):
        CALLS["geocode"].append(qs.get("address"))
        addr = (qs.get("address") or "").casefold()
        if addr in GEOCODES:
            lat, lng = GEOCODES[addr]
            return _json({"status": "OK", "results": [{"geometry": {"location": {"lat": lat, "lng": lng}}}]})
        return _json({"status": "ZERO_RESULTS", "results": []})
    if url.startswith("https://routes.googleapis.com/directions/v2:computeRoutes"):
        CALLS["routes"].append(url)
        raise _http_error(url, 403, b"{}")
    if "places.googleapis.com/v1/places:searchText" in url:
        CALLS["places"].append(json.loads(req.data.decode())["textQuery"])
        return _json({})
    CALLS["unexpected"].append(url)
    raise AssertionError(f"unexpected outbound URL in test: {url}")


urllib.request.urlopen = fake_urlopen

passed = 0


def ok(label, cond, extra=""):
    global passed
    assert cond, f"FAIL: {label} {extra}"
    passed += 1
    print(f"  ok - {label}")


def no_surprises(label=""):
    ok(f"no unexpected outbound calls {label}".strip(), not CALLS["unexpected"], CALLS["unexpected"])
    ok(f"no LiteAPI flight or Duffel stays call {label}".strip(), not CALLS["forbidden"], CALLS["forbidden"])


def prefs_for(votes, budget=(300, 900), vibes=("relaxation", "food"), group=4, nights=3, pace="moderately"):
    return {
        "accommodation_votes": votes,
        "lodging_band": ranking.lodging_band([budget] * group, nights),
        "budget_range": {"min": budget[0], "max": budget[1]},
        "top_vibes": list(vibes),
        "group_size": group,
        "group_pace": pace,
    }


def by_hotel(options):
    return {o["liteapi_hotel_id"]: o for o in options if o.get("liteapi_hotel_id")}


def walk_minutes(hid):
    h = ALL_HOTELS[hid]
    return max(1, round(ranking.distance_km(CENTER[0], CENTER[1], h["latitude"], h["longitude"]) * 1000 / 80))


def money(n):
    return f"${int(round(n)):,}"


def cut_mid_word(reason):
    """A chip that is its own sentence hard-cut inside a word — the review's
    "Airbnb/vacation re". A chip cut at a boundary ("Beachfront & Sea…") or
    naming its reason in other words ("In budget") is fine."""
    short = reason["short"].removesuffix("…")
    text = reason["text"].removeprefix("est. ")
    return (bool(short) and text.startswith(short) and len(short) < len(text)
            and text[len(short)] not in " /&+,;:·-–—")


def search_rome(votes, prefs, target=12, report=None, **kw):
    return real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", votes,
                             prefs=prefs, target=target, report=report if report is not None else {}, **kw)


def fly(**kw):
    return real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", travelers=4, **kw)


# ---------------------------------------------------------------------------
# 1. The lodging band — a documented formula over the questionnaire
# ---------------------------------------------------------------------------
print("lodging band:")
band = ranking.lodging_band([(300, 900)] * 4, 3)
ok("band = 45–65 % of what is left after $50/day meals and $80 transport",
   band == {"low": round((300 - 150 - 80) * 0.45), "high": round((900 - 150 - 80) * 0.65), "nights": 3}, band)
ok("blank ceilings are ignored, not zeroed",
   ranking.lodging_band([(500, 2000), (0, 0), (None, None)], 5) == ranking.lodging_band([(500, 2000)], 5))
ok("no ceiling at all -> no band", ranking.lodging_band([(0, 0)] * 3, 5) is None)
ok("a budget too small to mean anything -> no band", ranking.lodging_band([(100, 240)] * 4, 5) is None)
ok("zero nights -> no band", ranking.lodging_band([(300, 900)], 0) is None)
ok("city share scales with nights",
   ranking.band_for_nights(band, 1) == {"low": round(band["low"] / 3), "high": round(band["high"] / 3), "nights": 1})
ok("the check reads the same meals/transport constants the band is built from",
   ai_service.ranking.MEALS_PER_DAY == 50 and ai_service.ranking.LOCAL_TRANSPORT_FLAT == 80
   and not hasattr(ai_service, "MEALS_PER_DAY"))
ok("a splurge is allowed with a band or with no budget at all, never on a budget too small for a band",
   ranking.splurge_allowed(prefs_for({})) is True
   and ranking.splurge_allowed(prefs_for({}, budget=(100, 240))) is False
   and ranking.splurge_allowed({"lodging_band": None, "budget_range": {"min": 0, "max": 0}}) is True)

# ---------------------------------------------------------------------------
# 2. Single-base wide search: 12 stays, lanes, reasons, top-ups
# ---------------------------------------------------------------------------
print("single-base wide search:")
reset_calls()
VOTES = {"boutique hotel": 3, "airbnb/vacation rental": 1}
PREFS = prefs_for(VOTES)
report = {}
stays = search_rome(VOTES, PREFS, report=report)
no_surprises("(single-base)")
ok("12 options", len(stays) == 12, len(stays))
ok("every option carries a lane from the lane list",
   all(o.get("lane") in ranking.STAY_LANES for o in stays), [o.get("lane") for o in stays])
ok("no hotel appears twice", len(by_hotel(stays)) == len(stays))
ok("every option has reasons, a supplier and a sandbox flag",
   all(o["reasons"] and o["supplier"] == "liteapi" and o["sandbox"] is True for o in stays))
ok("why = lane · strongest reason", all(o["why"].startswith(o["lane"] + " · ") for o in stays),
   [o["why"] for o in stays][:3])
ok("every reason carries a short chip label (<= 18 chars)",
   all(0 < len(r["short"]) <= 18 for o in stays for r in o["reasons"]))
ok("no chip is its sentence cut inside a word", not any(cut_mid_word(r) for o in stays for r in o["reasons"]),
   [r for o in stays for r in o["reasons"] if cut_mid_word(r)])
ok("ratings are /10 and say so", all(o["rating_scale"] == "10" for o in stays if o["rating10"] is not None))
ok("match scores are 0–100", all(0 <= o["match_score"] <= 100 for o in stays), [o["match_score"] for o in stays])
ok("the existing option fields survive",
   all({"name", "type", "area", "price_per_night", "total_per_person", "rating", "review_count",
        "amenities", "why", "image", "live_price", "liteapi_hotel_id", "official_website"} <= set(o) for o in stays))

chosen = by_hotel(stays)
types = {hid: ALL_HOTELS[hid]["hotelTypeId"] for hid in chosen}
lanes = [o["lane"] for o in stays]
ok("the lane sequence follows the claim order, every lane truthful",
   lanes == ["Best match", "Your voted style", "Best value", "Top rated", "Best match", "Best match", "Best match",
             "Closest to the center", "Splurge", "Budget pick", "Also fits", "Also fits"], lanes)
winners = {o["lane"]: o["liteapi_hotel_id"] for o in stays if o["lane"] in ("Top rated", "Splurge", "Budget pick")}
ok("the top Best match (a core boutique match), the best-rated hotel, the Splurge and the cheapest are pinned",
   stays[0]["liteapi_hotel_id"] == "lp-r05"
   and winners == {"Top rated": "lp-r14", "Splurge": "lp-r16", "Budget pick": "lp-r01"}, (stays[0]["liteapi_hotel_id"], winners))
ok("the boutique vote is represented by a core match (B&B / guest house / inn)",
   any(t in ranking.VOTE_TYPE_IDS["boutique hotel"][0] for t in types.values()), types)
ok("the apartment-or-villa vote is represented by a core match",
   any(t in ranking.VOTE_TYPE_IDS["airbnb/vacation rental"][0] for t in types.values()), types)
ok("the Budget pick really is the cheapest thing on the board",
   chosen["lp-r01"]["total_per_person"] == min(o["total_per_person"] for o in stays))
splurge = chosen["lp-r16"]
ok("the Splurge is pricier than everything else on the board and above the priced median",
   splurge["total_per_person"] == max(o["total_per_person"] for o in stays)
   and splurge["total_per_person"] > statistics.median(p / 4 for p in RATES["prices"].values()), splurge["total_per_person"])

top_ups = [c for c in CALLS["lite_rates"] if c.get("hotelTypeIds")]
ok("two under-served voted styles got one filtered rates top-up each",
   len(top_ups) == 2 and report["top_ups"] == 2, (len(top_ups), report))
ok("the villa top-up asked for the apartment-or-villa core type ids",
   any(set(c["hotelTypeIds"]) == ranking.VOTE_TYPE_IDS["airbnb/vacation rental"][0] for c in top_ups))
ok("villas priced by the top-up got their content fetched by id",
   any("lp-v01" in (c.get("hotelIds") or "") for c in CALLS["lite_list"]), CALLS["lite_list"])
ok("the wide pass is one rates call by city with the documented knobs",
   any(c.get("cityName") == "Rome" and c.get("countryCode") == "IT" and c.get("maxRatesPerHotel") == 1
       and c.get("timeout") == 10 and c.get("limit") == 150 for c in CALLS["lite_rates"]))
ok("the search report says what it saw",
   report["listed"] == 30 and report["priced"] >= 27 and report["batches_failed"] == 0
   and report["truncated"] is False and report["mode"] == "city" and "duffel" not in report, report)
ok("no official-website lookups at board time", CALLS["places"] == [])
ok("the facilities catalog was fetched once", len(CALLS["lite_facilities"]) == 1)

low, high = band["low"], band["high"]
facilities_map = hotel_service.facility_names()


def scored(hid, prefs=PREFS):
    """One fixture hotel through the same record + scoring path the board uses."""
    rec = hotel_service._stay_record(ALL_HOTELS[hid], RATES["prices"][hid], 4, 3, CENTER, facilities_map)
    return ranking.score_stay(rec, prefs)


texts = [r["text"] for r in scored("lp-r10")["reasons"]]  # Hotel Forum Roma: 1180 / 4 travelers
ok("budget reason prints the per-person total against the band",
   f"{money(1180 / 4)} per person for 3 nights — inside your {money(low)}–{money(high)} stay budget" in texts, texts)
ok("rating reason = score/10 from N reviews", "8.7/10 from 1,240 guest reviews" in texts, texts)
ok("location reason = walking minutes at 80 m/min", f"{walk_minutes('lp-r10')} min walk from the center" in texts, texts)
ok("a plain hotel is 'close to' the boutique vote, not a core match",
   "Close to the boutique hotel style you asked for" in texts, texts)
eden = chosen["lp-r14"]
texts = [r["text"] for r in eden["reasons"]]
ok("Eden ($650 pp, 1.49x the ceiling) is over the ceiling, numerically",
   f"{money(650)} per person for 3 nights — {money(650 - high)} per person over the group's ceiling" in texts, texts)
ok("Eden's rating reason", "Top-rated: 9.4/10 from 1,800 guest reviews" in texts, texts)
stexts = [r["text"] for r in splurge["reasons"]]
ok("the Splurge leads with the over-ceiling line, computed from its own price",
   stexts[0] == f"{money(splurge['total_per_person'] - high)} per person over the group's ceiling"
   and all("inside your" not in t for t in stexts), stexts)
ok("vibe reason comes from facilities, not the name",
   "Spa & Pool — matches the group's relaxation vibe" in texts, texts)
ok("facilities become amenities on the card", eden["amenities"][:2] == ["Free WiFi", "Swimming pool"])
texts = [r["text"] for r in scored("lp-r28")["reasons"]]  # Inn at the Spanish Steps: 1800 / 4 = 450 — 1.03x the ceiling
ok("a stretch (<= 1.3x) names the overage against the ceiling",
   f"{money(450)} per person for 3 nights — {money(450 - high)} over your {money(high)} stay ceiling" in texts, texts)
texts = [r["text"] for r in scored("lp-r05")["reasons"]]  # B&B Navona Charm: a core boutique match
ok("a core match names the vote count", "Bed & breakfast — the style 3 of 4 of you voted for" in texts, texts)
cheapest = chosen["lp-r01"]
ok("the cheapest hotel ($70 pp) is inside the band",
   f"{money(70)} per person for 3 nights — inside your {money(low)}–{money(high)} stay budget"
   in [r["text"] for r in cheapest["reasons"]], cheapest["reasons"])
ok("no reason invents a per-night framing", all("per night" not in r["text"] for o in stays for r in o["reasons"]))

# ---------------------------------------------------------------------------
# 3. No votes -> no top-ups; three voted styles at the per-city target
# ---------------------------------------------------------------------------
print("votes drive the net:")
reset_calls()
r2 = {}
plain = search_rome({}, prefs_for({}), report=r2)
no_surprises("(no votes)")
ok("no votes -> no top-ups, still a full board", r2["top_ups"] == 0 and len(plain) == 12, (r2, len(plain)))
ok("no votes -> no style reasons", all(r["kind"] != "style" for o in plain for r in o["reasons"]))

reset_calls()
three = {"hostel": 1, "boutique hotel": 1, "4-star hotel": 1}
city_target = ranking.stay_target_for(hotel_service.stay_options_target(per_city=True), three)
ok("per-city target = max(5, voted styles + 3)", city_target == 6, city_target)
r3 = {}
city = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-22", 4, 2, "", three,
                         prefs=prefs_for(three, nights=2), target=city_target, per_city=True, report=r3)
no_surprises("(per city)")
ctypes = [ALL_HOTELS[o["liteapi_hotel_id"]]["hotelTypeId"] for o in city]
ok("per city: six stays", len(city) == 6, len(city))
ok("per city: every voted style appears",
   any(t in ranking.VOTE_TYPE_IDS["hostel"][0] for t in ctypes)
   and any(t in ranking.VOTE_TYPE_IDS["boutique hotel"][0] for t in ctypes)
   and any(t in ranking.VOTE_TYPE_IDS["4-star hotel"][0] for t in ctypes), ctypes)
ok("per city: the candidate pool is capped at 60",
   any(c.get("limit") == 60 for c in CALLS["lite_rates"] if c.get("cityName")))
ok("the top-up decision counts core matches the way the lane does (stars included)",
   hotel_service._core_count(LIST["data"], "4-star hotel")
   == sum(1 for h in LIST["data"] if ranking.is_core_match("4-star hotel", h["hotelTypeId"], "", h.get("stars")))
   and hotel_service._core_count(LIST["data"], "4-star hotel") < sum(1 for h in LIST["data"] if h["hotelTypeId"] in {204, 219, 231}))

# ---------------------------------------------------------------------------
# 4. Fallbacks: empty by-city answer -> id batches; a failing batch; a hang; a throttle
# ---------------------------------------------------------------------------
print("fallbacks:")
reset_calls()
MODES["city"], MODES["batch"] = "empty", "boom"
hotel_service._LITE_RATES_BATCH = 10
r4 = {}
boomed = search_rome(VOTES, PREFS, report=r4)
no_surprises("(boom)")
batched = [c for c in CALLS["lite_rates"] if c.get("hotelIds")]
cores = ranking.VOTE_TYPE_IDS["boutique hotel"][0] | ranking.VOTE_TYPE_IDS["airbnb/vacation rental"][0]
topup_priced = {h["id"] for h in LIST["data"] if h["hotelTypeId"] in cores and h["id"] in RATES["prices"]}
ok("a 204 by-city answer falls back to id batches of 10 for everything the top-ups didn't price",
   r4["mode"] == "ids" and len(batched) >= 2 and all(len(c["hotelIds"]) <= 10 for c in batched)
   and {h for c in batched for h in c["hotelIds"]} == {h["id"] for h in LIST["data"]} - topup_priced,
   (r4, len(batched)))
ok("one failing batch is counted, the others still price", r4["batches_failed"] == 1 and len(boomed) == 12, r4)
ok("the failed batch's hotels are simply absent", "lp-boom" not in by_hotel(boomed))

reset_calls()
arm_hang()
MODES["batch"] = "hang"
os.environ["STAY_SEARCH_DEADLINE_S"] = "1.5"
started = time.monotonic()
r5 = {}
hung = search_rome(VOTES, PREFS, report=r5)
elapsed = time.monotonic() - started
os.environ["STAY_SEARCH_DEADLINE_S"] = "20"
release_hang()
ok("a hung batch cannot hold the board past the deadline",
   elapsed < 1.5 + 0.75 and r5["truncated"] is True, (round(elapsed, 2), r5))
ok("the batches that answered still make the board", len(hung) >= 4, len(hung))
MODES["city"], MODES["batch"] = "normal", "normal"
hotel_service._LITE_RATES_BATCH = 50

reset_calls()
MODES["city"] = "429"
MODES["throttled"].discard("city")
slept = []
hotel_service._sleep = lambda s: slept.append(s)
r429 = {}
throttled = search_rome(VOTES, PREFS, report=r429)
hotel_service._sleep = time.sleep
MODES["city"] = "normal"
no_surprises("(429)")
ok("a throttled by-city rates search is retried once after Retry-After, nothing degrades",
   len(throttled) == 12 and r429["mode"] == "city" and r429["batches_failed"] == 0
   and [s for s in slept if s >= 0.5] == [1.0], (len(throttled), r429, slept[:5]))

# ---------------------------------------------------------------------------
# 5. Thin pricing: one re-price, then the board pads with estimates
# ---------------------------------------------------------------------------
print("thin pricing:")
reset_calls()
MODES["city"], MODES["batch"] = "thin", "thin"
r6 = {}
thin = search_rome(VOTES, PREFS, report=r6)
no_surprises("(thin)")
rerun = [c for c in CALLS["lite_rates"] if c.get("hotelIds")]
ok("two priced hotels trigger one re-price of the best unpriced ones",
   len(rerun) == 1 and 1 < len(rerun[0]["hotelIds"]) <= 50 and "lp-r01" not in rerun[0]["hotelIds"], rerun)
ok("still thin -> the two live stays come back alone", len(thin) == 2 and r6["priced"] == 2, (len(thin), r6))

responses = [{"budget_min": 300, "budget_max": 900, "travel_dates_start": "2026-11-20", "travel_dates_end": "2026-11-23",
              "departure_city": "New York", "vibes": ["relaxation", "food"], "accommodation_type": "boutique hotel",
              "pace": "moderately"} for _ in range(4)]
responses[3]["accommodation_type"] = "airbnb/vacation rental"
board = ai_service.generate_trip_options("Rome, Italy", responses, trip_name="Roma")
no_surprises("(board, thin)")
MODES["city"], MODES["batch"] = "normal", "normal"
live_on_board = [o for o in board["stay"] if o.get("live_price")]
padding = [o for o in board["stay"] if not o.get("live_price")]
ok("the board pads thin live pricing up to the floor of four", len(board["stay"]) >= 4 and len(live_on_board) == 2,
   (len(board["stay"]), len(live_on_board)))
ok("padding is labeled as estimates: supplier sample, lane Also fits, not live",
   padding and all(o["supplier"] == "sample" and o["lane"] == "Also fits" for o in padding), padding[:1])
ok("stay_all_live is False when estimates pad the board", board["stay_all_live"] is False)
ok("estimates say est. on every number they invent, never on the group's vote tally",
   all(r["text"].startswith("est. ") == (r["kind"] != "style")
       for o in padding for r in o["reasons"] if any(ch.isdigit() for ch in r["text"])))
ok("estimates carry no match score", all(o["match_score"] is None for o in padding))
ok("the board carries the schema version, prefs and band for the UI",
   board["schema_version"] == 2 and board["group_prefs"]["accommodation_votes"] == {"boutique hotel": 3, "airbnb/vacation rental": 1}
   and board["lodging_band"] == PREFS["lodging_band"] and board["generated_at"],
   {k: board.get(k) for k in ("schema_version", "group_prefs", "lodging_band")})
ok("the search report is keyed by town on a single-base trip too",
   list(board["stay_search"]) == ["Rome"] and board["stay_search"]["Rome"]["priced"] == 2, board["stay_search"])
ok("provenance names the suppliers actually on the board",
   board["stay_source"] == "liteapi" and board["flights_source"] == "duffel"
   and board["flight_suppliers"] == ["duffel"] and board["stay_suppliers"] == ["liteapi", "sample"],
   (board["stay_source"], board["flights_source"], board["flight_suppliers"], board["stay_suppliers"]))

live_one = [{"name": "Hotel Forum Roma", "live_price": True, "supplier": "liteapi", "lane": "Best match",
             "reasons": [], "total_per_person": 295, "type": "4-star hotel", "rating": 8.7, "amenities": []}]
ests = [{"name": "Hotel Forum Rome", "type": "4-star hotel", "total_per_person": 300, "rating": 4.4, "amenities": [], "why": "x"},
        {"name": "Casa Fantini", "type": "apartment", "total_per_person": 250, "rating": 4.6, "amenities": [], "why": "y"}]
padded = ai_service._pad_with_estimates(live_one, ests, PREFS, 3, 4)
ok("padding never repeats a property already priced live",
   [o["name"] for o in padded] == ["Hotel Forum Roma", "Casa Fantini"] and padded[1]["live_price"] is False,
   [o["name"] for o in padded])

# ---------------------------------------------------------------------------
# 6. A tiny budget: no budget talk, no Splurge lane at all
# ---------------------------------------------------------------------------
print("tiny budget:")
reset_calls()
tiny = search_rome(VOTES, prefs_for(VOTES, budget=(100, 240)))
no_surprises("(tiny)")
ok("no band -> no budget reasons at all", all(r["kind"] != "budget" for o in tiny for r in o["reasons"]))
ok("a budget too small for a band never produces a Splurge",
   all(o["lane"] != "Splurge" and all("splurge" not in r["text"].casefold() for r in o["reasons"]) for o in tiny),
   [o["lane"] for o in tiny])
ok("match scores need three known dimensions (style, rating, location, vibe still count)",
   all(o["match_score"] is not None for o in tiny))

# ---------------------------------------------------------------------------
# 7. Sample menus get the same lanes and reasons
# ---------------------------------------------------------------------------
print("sample menus:")
sample = ai_service._sample_trip_options("Rome, Italy", {**PREFS, "travel_dates": {"start": "2026-11-20", "end": "2026-11-23"}})
labeled = ranking.annotate_stays(sample["stay"], PREFS, 3, "sample")
ok("eight sample stays, each laned", len(labeled) == 8 and all(o["lane"] in ranking.STAY_LANES for o in labeled), len(labeled))
ok("sample ratings are normalized to /10 and flagged as a 5-scale",
   all(o["rating10"] == round(o["rating"] * 2, 1) and o["rating_scale"] == "5" for o in labeled))
numbered = [r for o in labeled for r in o["reasons"] if any(ch.isdigit() for ch in r["text"])]
ok("sample numbers are estimates — est. on every price line, never on the group's vote tally",
   all(o["match_score"] is None for o in labeled)
   and all(r["text"].startswith("est. ") == (r["kind"] != "style") for r in numbered)
   and any(r["kind"] == "style" for r in numbered), numbered)
villa = next(o for o in labeled if o["type"] == "airbnb/vacation rental")
ok("a stay typed with a vote gets the vote's chip form, and its tally reads bare",
   next(r for r in villa["reasons"] if r["kind"] == "style")
   == {"kind": "style", "text": "Airbnb/vacation rental — the style 1 of 4 of you voted for", "short": "Vacation rental"},
   villa["reasons"])
ok("no sample chip is cut inside a word", not any(cut_mid_word(r) for o in labeled for r in o["reasons"]),
   [r for o in labeled for r in o["reasons"] if cut_mid_word(r)])
sample_flights = ranking.annotate_flights(
    ai_service._sample_flights_for_city("Boston", "Rome, Italy", {"start": "2026-11-20", "end": "2026-11-23"}),
    "moderately", "Rome", "sample")
flight_numbered = [r for f in sample_flights for r in f["reasons"] if any(ch.isdigit() for ch in r["text"])]
ok("sample flights still say est. on every fare, time and duration — lane lines included",
   {r["kind"] for r in flight_numbered} == {"lane", "price"} and all(r["text"].startswith("est. ") for r in flight_numbered),
   flight_numbered)
ok("no sample flight chip is cut inside a word", not any(cut_mid_word(r) for f in sample_flights for r in f["reasons"]))
voted = ranking.annotate_stays(sample["stay"], prefs_for({"hostel": 2, "luxury resort": 1}), 3, "sample")
ok("a hostel vote puts the sample hostel up front, and the resort vote is covered too",
   any(o["lane"] in ("Best match", "Your voted style") and "hostel" in o["type"] for o in voted[:3])
   and any("resort" in o["type"] and o["lane"] in ("Best match", "Your voted style", "Splurge") for o in voted), [(o["type"], o["lane"]) for o in voted])
sample_splurge = next((o for o in labeled if o["lane"] == "Splurge"), None)
ok("a sample Splurge leads with an estimated numeric line",
   sample_splurge is None or sample_splurge["reasons"][0]["text"].startswith("est. $"), sample_splurge and sample_splurge["reasons"])

# ---------------------------------------------------------------------------
# 8. Pure ranking rules — truthful lanes, honest scores, whole-word vibes
# ---------------------------------------------------------------------------
print("ranking rules:")


def item(key, price, rank, rating=8.0, reviews=100, dist=1.0, vote=None, core=False):
    return {"key": key, "name": key, "rank_score": rank, "per_person_total": price, "rating10": rating,
            "review_count": reviews, "distance_km": dist, "style_vote": vote, "style_core": core,
            "reasons": [{"kind": "rating", "text": f"{rating}/10", "short": "r"}]}


BAND = {"low": 50, "high": 300, "nights": 3}
pool = [item("a", 100, 90, 8.5), item("b", 130, 80), item("c", 160, 70, 8.8), item("e", 200, 40, 7.0, reviews=10, dist=4.0),
        item("f", 380, 20, 8.6, reviews=50, dist=6.0)]
picks = {p["key"]: p["lane"] for p in ranking.pick_stay_lanes(pool, 8, {}, BAND, best_match_k=1)}
ok("Budget pick is skipped when the cheapest hotel already won another lane",
   picks == {"a": "Best match", "b": "Best value", "c": "Top rated", "e": "Closest to the center", "f": "Splurge"}, picks)
lanes8 = ranking.pick_stay_lanes(pool, 8, {}, BAND, best_match_k=1)
ok("the Splurge leads with the over-ceiling line, the rest of its reasons intact",
   [r["text"] for r in next(p for p in lanes8 if p["lane"] == "Splurge")["reasons"]] == ["$80 per person over the group's ceiling", "8.6/10"]
   and pool[4]["reasons"][0]["kind"] == "rating")
poor = [item("a", 100, 30, 6.0, reviews=5, dist=5.0), item("b", 130, 90), item("c", 160, 70, 8.8),
        item("d", 420, 60, 9.0, reviews=300, dist=3.0), item("e", 200, 40, 7.0, reviews=10, dist=4.0)]
picks = {p["key"]: p["lane"] for p in ranking.pick_stay_lanes(poor, 8, {}, BAND, best_match_k=1)}
ok("Budget pick goes to the cheapest hotel when nothing else claimed it",
   picks == {"b": "Best match", "c": "Best value", "d": "Top rated", "e": "Closest to the center", "a": "Budget pick"}, picks)
picks = {p["key"]: p["lane"] for p in ranking.pick_stay_lanes(pool + [item("d", 420, 60, 9.0, reviews=300, dist=3.0)], 8, {}, BAND, best_match_k=1)}
ok("no Splurge unless it is pricier than everything already on the board",
   picks["d"] == "Top rated" and picks["f"] == "Also fits" and "Splurge" not in picks.values(), picks)
picks = {p["key"]: p["lane"] for p in ranking.pick_stay_lanes(pool, 8, {}, None, best_match_k=1, allow_splurge=False)}
ok("a budget too small for a band disables the Splurge lane", picks["f"] == "Also fits" and "Splurge" not in picks.values(), picks)
picks = {p["key"]: p["lane"] for p in ranking.pick_stay_lanes(pool, 8, {}, None, best_match_k=1)}
ok("with no budget at all the priciest well-rated place above the median is the Splurge", picks["f"] == "Splurge", picks)

base = {"name": "X", "type_label": "hotel", "per_person_total": 300, "nights": 3, "distance_km": 1.0, "facilities": ["Free WiFi"]}
s0 = ranking.score_stay({**base, "rating10": 9.0, "review_count": 0}, PREFS)
s15 = ranking.score_stay({**base, "rating10": 9.0, "review_count": 15}, PREFS)
s40 = ranking.score_stay({**base, "rating10": 9.0, "review_count": 40}, PREFS)
ok("a 9.0 with no reviews ranks below a 9.0 with forty",
   s0["scores"]["rating"] < s40["scores"]["rating"] and s0["rank_score"] < s40["rank_score"], (s0["scores"], s40["scores"]))
ok("no printed rating line without twenty reviews; forty earn the Top-rated line",
   all(r["kind"] != "rating" for r in s0["reasons"] + s15["reasons"])
   and any(r["text"] == "Top-rated: 9.0/10 from 40 guest reviews" for r in s40["reasons"]), s40["reasons"])
VPREFS = prefs_for(VOTES, vibes=("relaxation", "nature"))  # no central-location fallback for either vibe
v = ranking.score_stay({**base, "rating10": 8.0, "review_count": 50, "facilities": ["Spacious rooms near Liverpool Street"]}, VPREFS)
ok("vibes match whole words: 'spacious' is not a spa, 'Liverpool' is not a pool",
   all(r["kind"] != "vibe" for r in v["reasons"]) and v["scores"]["vibe"] == 0.5, v["reasons"])
v2 = ranking.score_stay({**base, "rating10": 8.0, "review_count": 50, "facilities": ["Swimming pool"], "description": "Spa on site"}, VPREFS)
ok("facilities are read before the description",
   next(r["text"] for r in v2["reasons"] if r["kind"] == "vibe") == "Pool — matches the group's relaxation vibe", v2["reasons"])
v3 = ranking.score_stay({**base, "rating10": 8.0, "review_count": 50, "facilities": [], "description": "Rooftop pool and a small spa."}, VPREFS)
ok("the description counts when the facilities say nothing",
   next(r["text"] for r in v3["reasons"] if r["kind"] == "vibe") == "Spa & Pool — matches the group's relaxation vibe", v3["reasons"])
b0 = ranking.score_stay({**base, "rating10": 8.0, "review_count": 50}, {**PREFS, "lodging_band": {"low": 0, "high": 436, "nights": 3}})
ok("a $0 floor is never printed — the verdict is phrased against the ceiling",
   any(r["text"] == "$300 per person for 3 nights — inside your $436 stay budget" for r in b0["reasons"]), b0["reasons"])
ok("a guest house is not a whole-group home; a country house or villa is",
   not ranking.is_home_type(None, "guest house") and not ranking.is_home_type(None, "Bed & Breakfast")
   and ranking.is_home_type(None, "country house") and ranking.is_home_type(None, "villa") and not ranking.is_home_type(None, "hotel"))
ok("a star rating names a hotel's class, never a villa's",
   ranking.star_label("hotel", 4) == "4-star hotel" and ranking.star_label("villa", 4) == "villa")
ok("every accommodation vote has a fixed chip form, whatever its casing",
   {v: ranking.short_label(v) for v in ranking.VOTE_TYPE_IDS}
   == {"hostel": "Hostel", "boutique hotel": "Boutique hotel", "4-star hotel": "4-star hotel",
       "luxury resort": "Luxury resort", "airbnb/vacation rental": "Vacation rental"}
   and ranking.short_label("Airbnb/vacation rental") == "Vacation rental")
ok("a long chip is cut back to its last whole word — a slash counts — and marked with …",
   [ranking.short_label(t) for t in ("Beachfront & Sea view", "Mountain view & Lake", "Countryside/vineyard estate",
                                     "Bed & breakfast farmhouse")]
   == ["Beachfront & Sea…", "Mountain view…", "Countryside…", "Bed & breakfast…"])
ok("a chip that fits is left alone: no … unless something was cut",
   ranking.short_label("Apartment or villa") == "Apartment or villa" and ranking.short_label("Lands 08:30") == "Lands 08:30")
overflow = ranking.score_stay({"name": "Podere", "type_label": "agriturismofarmhousevilla", "per_person_total": 300,
                               "nights": 3}, prefs_for({"airbnb/vacation rental": 1}), "ai")
ok("a label whose first word overflows any chip falls back to the vote's chip, never a broken word",
   ranking.short_label("Agriturismofarmhousevilla") == ""
   and next(r["short"] for r in overflow["reasons"] if r["kind"] == "style") == "Vacation rental", overflow["reasons"])
ai_bb = ranking.score_stay({"name": "Casa Roma", "type_label": "bed & breakfast", "per_person_total": 300, "nights": 3,
                            "rating10": 9.0, "review_count": 120, "distance_km": 0.8},
                           prefs_for({"boutique hotel": 2, "hostel": 1}, group=3), "ai")
ok("an AI stay: the vote tally is real (no est.); its price, score and distance are estimates (est.)",
   [(r["kind"], r["text"].startswith("est. ")) for r in ai_bb["reasons"]]
   == [("style", False), ("budget", True), ("rating", True), ("location", True)]
   and ai_bb["reasons"][0]["text"] == "Bed & breakfast — the style 2 of 3 of you voted for", ai_bb["reasons"])


def fact(key, price, minutes, stops=1, carrier="XX"):
    return {"key": key, "price": price, "minutes": minutes, "stops": stops, "out_depart": 600, "out_arrive": 900,
            "out_arrive_date": "2026-11-21", "back_depart": 1000, "checked_bag": False, "carry_on": True,
            "single_carrier": True, "carrier": carrier, "dest_city": "Rome", "live": True}


def roles(facts):
    return {facts[p["index"]]["key"]: p["role"] for p in ranking.pick_flight_roles(facts, 9)}


r = roles([fact("A", 300, 600), fact("B", 320, 900), fact("C", 500, 580)])
ok("Fastest goes only to an itinerary no slower than anything on the board", r["A"] == "cheapest" and r["C"] == "fastest"
   and "balanced" not in r.values(), r)
r = roles([fact("A", 300, 600), fact("B", 320, 900), fact("C", 500, 700)])
ok("when the cheapest is also the fastest, nothing else is stamped Fastest or Best balance",
   r["A"] == "cheapest" and "fastest" not in r.values() and "balanced" not in r.values(), r)
r = roles([fact("A", 300, 900), fact("B", 320, 620), fact("C", 500, 600)])
ok("Best balance never goes to an itinerary both pricier and slower than a pick",
   r == {"A": "cheapest", "C": "fastest", "B": "balanced"}, r)

# ---------------------------------------------------------------------------
# 9. The LiteAPI pacer: a rate with a one-token burst, never a concurrency cap
# ---------------------------------------------------------------------------
print("throttle:")
saved_rate = os.environ.pop("LITEAPI_RATE_PER_S")
ok("sandbox keys pace at 4/s by default", hotel_service._lite_rate_per_s() == 4.0)
os.environ["LITEAPI_API_KEY"] = "prod_x"
ok("production keys pace at 8/s by default", hotel_service._lite_rate_per_s() == 8.0)
os.environ["LITEAPI_API_KEY"] = "sand_test_key"
clock = {"t": 100.0}
slept = []


def fake_sleep(s):
    slept.append(round(s, 3))
    clock["t"] += s


hotel_service._clock, hotel_service._sleep = (lambda: clock["t"]), fake_sleep
hotel_service._bucket["rate"] = 0.0  # a fresh bucket
hotel_service._take_token()
hotel_service._take_token()
ok("the bucket holds one token: the second call waits a full 1/4 s interval", slept == [0.25], slept)
try:
    hotel_service._take_token(deadline_at=clock["t"] + 0.1)
    raised = False
except TimeoutError:
    raised = True
ok("a wait that would overrun the search deadline raises instead of queueing", raised and slept == [0.25])
clock["t"] += 10
hotel_service._take_token()
hotel_service._take_token()
ok("tokens never pile up while idle: after ten quiet seconds the burst is still one call", slept == [0.25, 0.25], slept)
hotel_service._clock, hotel_service._sleep = time.monotonic, time.sleep
os.environ["LITEAPI_RATE_PER_S"] = saved_rate
hotel_service._bucket["rate"] = 0.0

# ---------------------------------------------------------------------------
# 10. Flights: Duffel only — places, roles, pace-aware, re-verified, bounded
# ---------------------------------------------------------------------------
print("flights:")
reset_calls()
flight_service._airport_lookup.cache_clear()
flights = fly(pace="moderately")
no_surprises("(flights)")
lanes = [f["lane"] for f in flights]
ok("eight itineraries, eight distinct lanes", len(flights) == 8 and len(set(lanes)) == 8, lanes)
ok("places resolve through Duffel's suggestions, one per city",
   CALLS["duffel_places"] == ["New York", "Rome"], CALLS["duffel_places"])
ok("one Duffel offer request: the metro codes both ways and the party of four",
   len(CALLS["duffel_offers"]) == 1
   and CALLS["duffel_offers"][0]["data"]["slices"] == [
       {"origin": "NYC", "destination": "ROM", "departure_date": "2026-11-20"},
       {"origin": "ROM", "destination": "NYC", "departure_date": "2026-11-25"}]
   and len(CALLS["duffel_offers"][0]["data"]["passengers"]) == 4, CALLS["duffel_offers"])
ok("'Rome, Italy' resolves to the Italian metro, never the namesake in New York state",
   flight_service._resolve("Rome, Italy") == ("ROM", ("ROM", "FCO", "CIA")), flight_service._resolve("Rome, Italy"))
ok("a metro's airports all count as that city", set(flight_service._resolve("New York")[1]) == {"NYC", "JFK", "EWR", "LGA"},
   flight_service._resolve("New York"))
ok("a city without a metro code resolves to its airport", flight_service._resolve("Florence, Italy") == ("FLR", ("FLR",)),
   flight_service._resolve("Florence, Italy"))
ok("the lookups are cached per process", CALLS["duffel_places"] == ["New York", "Rome", "Florence"], CALLS["duffel_places"])
duffel_pp = {o["id"]: float(o["total_amount"]) / len(o["passengers"]) for o in DUFFEL_OFFERS["data"]["offers"]}
ok("Duffel offers are priced for the whole party of four; the board divides by the passengers",
   all(len(o["passengers"]) == 4 for o in DUFFEL_OFFERS["data"]["offers"]) and duffel_pp["off_j01"] == 378)
ok("fixture pool: 16 Duffel offers", len(duffel_pp) == 16, len(duffel_pp))
median = statistics.median(duffel_pp.values())
cheapest = next(f for f in flights if f["lane"] == "Cheapest")
ok("the cheapest card is the cheapest offer, carrying its Duffel offer id",
   cheapest["est_price_per_person"] == 378 and cheapest["supplier"] == "duffel"
   and cheapest["duffel_offer_id"] == "off_j01" and "offer_ids" not in cheapest, cheapest)
ok("the cheapest reason = median gap over the pool",
   cheapest["reasons"][0]["text"] == f"Cheapest live fare — ${int(median - 378)} under the median option",
   cheapest["reasons"])
ok("why = the lane reason only; bags live in reasons",
   cheapest["why"] == cheapest["reasons"][0]["text"] and any(r["kind"] == "bags" for r in cheapest["reasons"]))
ok("every option: reasons[0] is its lane reason, sandbox is flagged (a test token), currency is set",
   all(f["reasons"][0]["kind"] == "lane" and f["sandbox"] is True and f["currency"] == "USD" for f in flights))
LANE_RE = {
    "Cheapest": ("Cheapest live fare",), "Nonstop": ("Nonstop both ways",), "Fastest": ("Fastest routing",),
    "Best balance": ("Within $", "Same price as the cheapest"), "Bags included": ("Checked bag included",),
    "Early arrival": ("Lands ",), "No early start": ("Departs ",), "Late return": ("Home flight at ",),
    "One airline": (" both ways — one check-in",),
}
ok("each lane reason reads as its lane", all(any(p in f["reasons"][0]["text"] for p in LANE_RE[f["lane"]]) for f in flights),
   [(f["lane"], f["reasons"][0]["text"]) for f in flights])
by_lane = {f["lane"]: f for f in flights}
minutes_of = {f["lane"]: ranking.parse_duration_minutes(f["duration"]) + ranking.parse_duration_minutes(f["return_duration"])
              for f in flights}
if "Fastest" in by_lane:
    ok("the Fastest card is literally the fastest on the board", minutes_of["Fastest"] == min(minutes_of.values()), minutes_of)
if "Best balance" in by_lane:
    bal = by_lane["Best balance"]
    ok("the Best balance card is not both pricier and slower than another card",
       not any(bal["est_price_per_person"] >= f["est_price_per_person"] and minutes_of["Best balance"] >= minutes_of[f["lane"]]
               for f in flights if f is not bal), minutes_of)
early = next(f for f in flights if f["lane"] == "Early arrival")
ok("early arrival = the earliest local landing, with its date",
   early["arrive"] == "2026-11-21 06:30"
   and early["reasons"][0]["text"] == "Lands 06:30 on Nov 21 — most of day one in Rome", early["reasons"])
ok("the price gap reason is relative to the cheapest fare",
   all(f"${f['est_price_per_person'] - 378} more than the cheapest fare" in [r["text"] for r in f["reasons"]]
       for f in flights if f["lane"] != "Cheapest"))
ok("near-duplicates never earn a slot", all(f["duffel_offer_id"] != "off_j02" for f in flights))
ok("match scores and roles are present", all(0 < f["match_score"] <= 100 and f["role"] for f in flights))
ok("the board's flight source is duffel", ai_service._live_source_label(flights, "duffel") == "duffel"
   and ranking.summarize_suppliers(flights) == ["duffel"])
offers_by_id = {o["id"]: o for o in DUFFEL_OFFERS["data"]["offers"]}
ok("every card is bookable: it carries its offer id, and the booking engine re-finds the same itinerary from it",
   all(f["duffel_offer_id"] in offers_by_id and f["duffel_offer_ids"] == [f["duffel_offer_id"]]
       and booking_service._board_signature(f) == duffel_booking.itinerary_signature(offers_by_id[f["duffel_offer_id"]])
       and [sl["departure_date"] for sl in booking_service._slices_from_flight(f)] == ["2026-11-20", "2026-11-25"]
       for f in flights), [(f["lane"], f["duffel_offer_id"]) for f in flights])

reset_calls()
busy = fly(pace="very")
ok("a very busy group sees Early arrival claimed second", [f["lane"] for f in busy][:2] == ["Cheapest", "Early arrival"],
   [f["lane"] for f in busy])
relaxed = fly(pace="slightly")
ok("a relaxed group sees No early start claimed second", [f["lane"] for f in relaxed][:2] == ["Cheapest", "No early start"],
   [f["lane"] for f in relaxed])

reset_calls()
MODES["duffel_currency"] = {"off_j01": "GBP"}
gbp = fly()
MODES["duffel_currency"] = "USD"
no_surprises("(gbp)")
gbp_cheapest = next(f for f in gbp if f["lane"] == "Cheapest")
ok("an offer in another currency is left off the board — no silent FX; the USD offers still rank",
   len(CALLS["duffel_offers"]) == 1 and all(f["duffel_offer_id"] != "off_j01" for f in gbp)
   and gbp_cheapest["duffel_offer_id"] == "off_j02" and gbp_cheapest["est_price_per_person"] == 386, gbp_cheapest)

reset_calls()
os.environ["DUFFEL_ACCESS_TOKEN"], MODES["duffel_live"] = "duffel_live_token", True
live = fly()
os.environ["DUFFEL_ACCESS_TOKEN"], MODES["duffel_live"] = "duffel_test_token", False
ok("live-mode offers are not flagged as test inventory", len(live) == 8 and all(f["sandbox"] is False for f in live),
   [f["sandbox"] for f in live])

reset_calls()
MODES["duffel"] = "empty"
empty = fly()
MODES["duffel"] = "normal"
ok("when Duffel answers nothing there are no live flights (the AI menu takes the city)",
   empty == [] and len(CALLS["duffel_offers"]) == 1, empty)

reset_calls()
MODES["duffel"] = "429"
MODES["throttled"].discard("duffel")
retried = fly()
MODES["duffel"] = "normal"
ok("a Duffel 429 is retried once after ratelimit-reset",
   len(CALLS["duffel_offers"]) == 2 and len(retried) == 8, len(CALLS["duffel_offers"]))

reset_calls()
arm_hang()
MODES["duffel"] = "hang"
flight_service._DUFFEL_TIMEOUT = 0.5
started = time.monotonic()
bounded = fly()
elapsed = time.monotonic() - started
flight_service._DUFFEL_TIMEOUT = duffel_service.FLIGHT_HTTP_TIMEOUT
MODES["duffel"] = "normal"
release_hang()
ok("a Duffel search that trickles past its bound is abandoned; the city falls back to the AI menu",
   elapsed < 1.5 and bounded == [], (round(elapsed, 2), len(bounded)))

reset_calls()
fresh = live_flight_details(cheapest)
ok("a live option re-verifies through Duffel, per person over four passengers",
   fresh["source"] == "duffel" and fresh["price_fresh"] is True and fresh["price_per_person"] == 372
   and len(CALLS["duffel_offer_get"]) == 1 and CALLS["duffel_offer_get"][0].endswith("/off_j01"), fresh)
reset_calls()
naive = live_flight_details(dict(cheapest, expires_at="2099-01-01T00:00:00"))
ok("a naive expires_at is read as UTC, not a crash", naive["price_fresh"] is True and len(CALLS["duffel_offer_get"]) == 1, naive)
odd = live_flight_details(dict(cheapest, expires_at="not a date"))
ok("an unparseable expiry still renders the stored facts", odd["source"] == "duffel" and odd["price_fresh"] is False and odd["segments"], odd)
reset_calls()
legacy = live_flight_details(dict(cheapest, supplier="liteapi", duffel_offer_id="", duffel_offer_ids=[]))
ok("a live fare an older board stored from another supplier shows the board's facts, re-verified nowhere",
   legacy == {"kind": "live", "source": "board"} and CALLS["duffel_offer_get"] == [], legacy)
no_surprises("(flight details)")

# ---------------------------------------------------------------------------
# 11. Hung flight searches never starve a stay search
# ---------------------------------------------------------------------------
print("flights under load:")
reset_calls()
arm_hang()
MODES["duffel"] = "hang"
flyers = [threading.Thread(target=fly, daemon=True) for _ in range(3)]
for t in flyers:
    t.start()
time.sleep(0.3)  # the three Duffel flight searches are now hanging inside the fake
os.environ["STAY_SEARCH_DEADLINE_S"] = "2"
started = time.monotonic()
r11 = {}
under_load = search_rome(VOTES, PREFS, report=r11)
elapsed = time.monotonic() - started
os.environ["STAY_SEARCH_DEADLINE_S"] = "20"
MODES["duffel"] = "normal"
release_hang()
for t in flyers:
    t.join(timeout=20)
ok("three hung flight searches do not hold up a stay search",
   len(under_load) == 12 and r11["truncated"] is False and elapsed < 2.0, (len(under_load), r11, round(elapsed, 2)))
no_surprises("(under load)")

# ---------------------------------------------------------------------------
# 12. Open-jaw: one multi-slice request; paired one-ways only when needed
# ---------------------------------------------------------------------------
print("open-jaw:")
reset_calls()
jaw = fly(return_from_city="Florence, Italy", pace="moderately")
no_surprises("(open-jaw)")
ok("the open jaw is asked as one Duffel request: into Rome, home from Florence",
   CALLS["duffel_offers"][0]["data"]["slices"] == [
       {"origin": "NYC", "destination": "ROM", "departure_date": "2026-11-20"},
       {"origin": "FLR", "destination": "NYC", "departure_date": "2026-11-25"}], CALLS["duffel_offers"][:1])
ok("no airline sold it as one ticket -> two Duffel one-way searches",
   len(CALLS["duffel_offers"]) == 3 and [len(c["data"]["slices"]) for c in CALLS["duffel_offers"][1:]] == [1, 1],
   len(CALLS["duffel_offers"]))
ok("at least six distinct lanes from the pairing", len(jaw) >= 6 and len({f["lane"] for f in jaw}) == len(jaw),
   [f["lane"] for f in jaw])
ok("every pair says it is two one-way tickets the travelers book themselves, home from Florence",
   all(flight_service.PAIR_NOTE in f["why"] and "you book these yourself" in f["why"]
       and f["return_route"].startswith("FLR") for f in jaw), [f["why"] for f in jaw])
ok("every pair carries both legs' Duffel offer ids and is flagged as separate tickets",
   all(len(f["duffel_offer_ids"]) == 2 and f["duffel_offer_id"] == "" and f["supplier"] == "duffel"
       and f["separate_tickets"] is True for f in jaw),
   [f["duffel_offer_ids"] for f in jaw])
late = next((f for f in jaw if f["lane"] == "Late return"), None)
ok("the late-return pair really leaves Florence after 15:00", late is not None and late["return"] >= "2026-11-25 15:00", late)
reset_calls()
fresh = live_flight_details(jaw[0])
ok("a pair re-verifies both legs and quotes their sum", fresh["price_fresh"] is True
   and fresh["price_per_person"] == 744 and len(CALLS["duffel_offer_get"]) == 2, fresh)

reset_calls()
MODES["open_jaw"] = "sold"
sold = fly(return_from_city="Florence, Italy", pace="moderately")
MODES["open_jaw"] = "none"
no_surprises("(open-jaw sold)")
ok("an open jaw sold as one ticket is used as is — no one-way searches",
   len(CALLS["duffel_offers"]) == 1 and sold and all(
       f["return_route"].startswith("FLR") and "two one-way" not in f["why"] and f["duffel_offer_id"]
       and f["separate_tickets"] is False for f in sold),
   [(f["lane"], f["return_route"]) for f in sold])

reset_calls()
arm_hang()
MODES["duffel"] = "hang"
flight_service._DUFFEL_TIMEOUT = 0.5
timed_out = fly(return_from_city="Florence, Italy", pace="moderately")
flight_service._DUFFEL_TIMEOUT = duffel_service.FLIGHT_HTTP_TIMEOUT
MODES["duffel"] = "normal"
release_hang()
ok("an open-jaw search that never answered is not swapped for one-way pairs — the AI menu covers it",
   timed_out == [] and len(CALLS["duffel_offers"]) == 1, (len(timed_out), len(CALLS["duffel_offers"])))

reset_calls()
nowhere = fly(return_from_city="Positano, Italy", pace="moderately")
ok("a return city with no airport match prices nothing — never a plain round trip passed off as the open jaw",
   nowhere == [] and CALLS["duffel_offers"] == [] and "Positano" in CALLS["duffel_places"], (len(nowhere), CALLS["duffel_places"]))
reset_calls()
lost_home = fly(return_to_city="Atlantis")
ok("a home city that doesn't resolve prices nothing either — never a return to the wrong home",
   lost_home == [] and CALLS["duffel_offers"] == [], len(lost_home))
no_surprises("(open-jaw guards)")

# Boards saved before flights went Duffel-only still hold LiteAPI fares
from backend.routers.trips import _retire_legacy_flights  # noqa: E402
old_board = {"flights_sandbox": True, "flights_source": "liteapi", "flights_all_live": True, "flights": {
    "New York": [dict(cheapest, supplier="liteapi", airline="Nuitee Air", sandbox=True, duffel_offer_id="",
                      duffel_offer_ids=[]),
                 dict(cheapest, id="f-old2", airline="Nuitee Air", sandbox=True, duffel_offer_id="",
                      duffel_offer_ids=[], supplier=None)],
    "Boston": [{"airline": "Delta", "live_price": False, "est_price_per_person": 500}]}}
shown = _retire_legacy_flights(old_board)
ny = shown["flights"]["New York"]
ok("a stored LiteAPI fare (Nuitee Air) loses LIVE PRICE and the test-carrier flag; the board says it is legacy",
   shown["flights_legacy"] is True and shown["flights_sandbox"] is False and shown["flights_all_live"] is False
   and "flights_source" not in shown
   and all(o["live_price"] is False and o["sandbox"] is False and o["earlier_search"] == "liteapi"
           and o["segments"]["out"] for o in ny), (shown.get("flights_source"), ny[0].get("earlier_search")))
ok("the relabel is response-only — the stored board is untouched",
   old_board["flights"]["New York"][0]["live_price"] is True and old_board["flights_source"] == "liteapi")
mixed = _retire_legacy_flights({"flights": {"New York": [ny[0] | {"live_price": True, "supplier": "liteapi"}, cheapest]}})
ok("a Duffel fare on the same board stays live and keeps the board's Duffel provenance",
   mixed["flights"]["New York"][1] is cheapest and mixed["flights_source"] == "duffel"
   and mixed["flights_sandbox"] is True and mixed["flights_legacy"] is True)
fresh_board = {"flights": {"New York": [cheapest]}, "flights_source": "duffel"}
ok("a Duffel-only board passes through unchanged", _retire_legacy_flights(fresh_board) is fresh_board)

# ---------------------------------------------------------------------------
# 13. No keys, no calls
# ---------------------------------------------------------------------------
print("no keys:")
reset_calls()
flight_service._airport_lookup.cache_clear()
os.environ["LITEAPI_API_KEY"], os.environ["DUFFEL_ACCESS_TOKEN"] = "", ""
ok("no LiteAPI key -> no stays, no calls",
   search_rome(VOTES, PREFS) == [] and CALLS["lite_list"] == [] and CALLS["lite_rates"] == [])
ok("no keys -> no flights, no calls", fly() == [] and CALLS["duffel_places"] == [] and CALLS["duffel_offers"] == [])
os.environ["LITEAPI_API_KEY"] = "sand_test_key"
ok("a LiteAPI key alone searches no flights at all", not flight_service.flights_available() and fly() == []
   and CALLS["duffel_places"] == [] and CALLS["duffel_offers"] == [] and CALLS["forbidden"] == [])
os.environ["DUFFEL_ACCESS_TOKEN"] = "duffel_test_token"
no_surprises("(no keys)")

# ---------------------------------------------------------------------------
# 14. Stays are LiteAPI's alone — no env var brings Duffel stays back
# ---------------------------------------------------------------------------
print("stays are liteapi-only:")
reset_calls()
os.environ["DUFFEL_STAYS"] = "1"  # the retired flag: must change nothing
r14 = {}
lite_only = search_rome(VOTES, PREFS, target=40, report=r14)
os.environ.pop("DUFFEL_STAYS")
no_surprises("(liteapi-only stays)")
ok("with a Duffel token set (and the old flag), every stay is a bookable LiteAPI hotel",
   lite_only and all(o["supplier"] == "liteapi" and o["liteapi_hotel_id"] for o in lite_only)
   and "duffel" not in r14, (ranking.summarize_suppliers(lite_only), r14))
ok("no Duffel stays search exists to wire", not hasattr(duffel_service, "search_stays")
   and not hasattr(hotel_service, "duffel_search_stays"))

# ---------------------------------------------------------------------------
# 15. Multi-city board end to end (sample mode): per-city sets, provenance
# ---------------------------------------------------------------------------
print("multi-city board:")
reset_calls()
multi = [{"budget_min": 300, "budget_max": 900, "travel_dates_start": "2026-11-20", "travel_dates_end": "2026-11-26",
          "departure_city": "New York", "vibes": ["culture", "food"], "accommodation_type": acc,
          "trip_style": "multi-city", "multi_city_picks": ["Florence, Italy", "Venice, Italy"], "pace": "very"}
         for acc in ("hostel", "boutique hotel", "4-star hotel")]
board = ai_service.generate_trip_options("Rome, Italy", multi, trip_name="Grand tour")
no_surprises("(multi-city)")
towns = [c["town"] for c in board["route"]["cities"]]
ok("a three-city route", len(towns) == 3, towns)
per_city = {t: [o for o in board["stay"] if (o.get("city") or "").split(",")[0].strip() == t] for t in towns}
ok("every city gets max(5, voted styles + 3) = 6 stays", all(len(v) == 6 for v in per_city.values()),
   {t: len(v) for t, v in per_city.items()})
ok("every city's set carries all three voted styles",
   all(any(ALL_HOTELS[o["liteapi_hotel_id"]]["hotelTypeId"] in ranking.VOTE_TYPE_IDS[v][0] for o in opts)
       for opts in per_city.values() for v in ("hostel", "boutique hotel", "4-star hotel")))
ok("city budget reasons use the city's nights",
   all(" for 2 nights — " in r["text"] for opts in per_city.values() for o in opts for r in o["reasons"] if r["kind"] == "budget"))
ok("the search report is per town", set(board["stay_search"]) == set(towns), board["stay_search"].keys())
ok("provenance: suppliers listed, sources named, sandbox flights flagged",
   board["stay_suppliers"] == ["liteapi"] and board["flight_suppliers"] == ["duffel"]
   and board["stay_source"] == "liteapi" and board["flights_source"] == "duffel"
   and board["flights_sandbox"] is True and board["stay_all_live"] is True, (board["stay_suppliers"], board["flight_suppliers"], towns, CALLS["duffel_places"], [c["data"]["slices"] for c in CALLS["duffel_offers"]]))
ny_flights = board["flights"]["New York"]
ok("flights per departure city: laned, with the pace order, flying home from the route's last city (Venice)",
   len(ny_flights) >= 6 and ny_flights[1]["lane"] == "Early arrival"
   and all(f["return_route"].startswith("VCE") for f in ny_flights),
   [(f["lane"], f["return_route"]) for f in ny_flights])
ok("option ids are assigned after ranking", all(o["id"].startswith("stay-") for o in board["stay"]))
ok("three travelers from New York: the city's flight link searches three adults",
   all("passengers=adults:3&" in f["link"] for f in board["flights"]["New York"]), board["flights"]["New York"][0]["link"])
split = ai_service._aggregate_preferences([
    {"departure_city": city, "budget_min": 300, "budget_max": 900, "travel_dates_start": "2026-11-20",
     "travel_dates_end": "2026-11-23"} for city in ("New York", "new york", "Boston")])
linked = ai_service._attach_option_links(
    {"dates": {"start": "2026-11-20", "end": "2026-11-23"}, "stay": [{"name": "Hotel Artemide"}],
     "flights": {"New York": [{"airline": "A"}, {"airline": "B"}], "Boston": [{"airline": "C"}],
                 "Your city": [{"airline": "D"}]}},
    split, "Rome, Italy")
ok("each departure city's flight link searches its own travelers, not the whole group (unknown city: one)",
   split["travelers_by_city"] == {"New York": 2, "Boston": 1}
   and all("passengers=adults:2&" in f["link"] for f in linked["flights"]["New York"])
   and "passengers=adults:1&" in linked["flights"]["Boston"][0]["link"]
   and "passengers=adults:1&" in linked["flights"]["Your city"][0]["link"],
   {city: [f["link"] for f in opts] for city, opts in linked["flights"].items()})
ok("a stay link still searches for the whole group",
   "adults=3" in linked["stay"][0]["links"]["airbnb"] and "group_adults=3" in linked["stay"][0]["links"]["booking"],
   linked["stay"][0]["links"])

# ---------------------------------------------------------------------------
# 16. The API: lighter board payload, media not frozen when throttled, a page that never waits
# ---------------------------------------------------------------------------
print("api:")
signup = client.post("/auth/signup", json={"email": "wide@example.com", "password": "secret123", "name": "Wide"}).json()
HEADERS = {"Authorization": f"Bearer {signup['token']}"}
trip_id = client.post("/trips", headers=HEADERS, json={"name": "Wide test", "destination": "Rome, Italy"}).json()["id"]
options_json = {
    "destination": "Rome, Italy", "country": "Italy", "dates": {"start": "2026-11-20", "end": "2026-11-23"},
    "source": "ai", "schema_version": 2,
    "stay": [
        {"id": "stay-1", "name": "Hotel Artemide", "liteapi_hotel_id": "lp-r11", "price_per_night": 440,
         "total_per_person": 330, "lane": "Best match", "reasons": [], "supplier": "liteapi",
         "details": {"kind": "live", "source": "liteapi", "description": "x" * 5000, "address": "Via Nazionale",
                     "links_checked": True}},
        {"id": "stay-2", "name": "Hotel Forum Roma", "liteapi_hotel_id": "lp-r10", "price_per_night": 390,
         "total_per_person": 295, "lane": "Top rated", "reasons": [], "supplier": "liteapi"},
    ],
    "visits": [{"id": "visit-1", "name": "Colosseum", "cost_per_person": 20,
                "details": {"kind": "live", "source": "places", "address": "Piazza del Colosseo"}}],
    "events": [],
    "flights": {"New York": [{"id": "fl-1", "airline": "TestAir", "est_price_per_person": 500, "live_price": False}]},
    "transport": {},
}
with db.get_conn() as conn:
    conn.execute("UPDATE trips SET options = ?, status = 'curating' WHERE id = ?", (json.dumps(options_json), trip_id))
res = client.get(f"/trips/{trip_id}/options", headers=HEADERS).json()
stay1 = res["options"]["stay"][0]
ok("cached detail sheets ship as a stub", stay1["details"] == {"kind": "cached", "source": "liteapi", "cached": True}, stay1["details"])
ok("visits are stubbed too, flights untouched",
   res["options"]["visits"][0]["details"]["kind"] == "cached" and res["options"]["flights"]["New York"][0] == options_json["flights"]["New York"][0])
with db.get_conn(readonly=True) as conn:
    stored = json.loads(conn.execute("SELECT options FROM trips WHERE id = ?", (trip_id,)).fetchone()["options"])
ok("storage keeps the full sheet", stored["stay"][0]["details"]["kind"] == "live" and len(stored["stay"][0]["details"]["description"]) == 5000)

reset_calls()
arm_hang()
MODES["rates_hang_ids"] = {"lp-r11"}
saved_wait = hotel_service._QUICK_REFRESH_WAIT_S
hotel_service._QUICK_REFRESH_WAIT_S = 0.5
started = time.monotonic()
res = client.get(f"/trips/{trip_id}/option-details/stay-1", headers=HEADERS).json()
elapsed = time.monotonic() - started
hotel_service._QUICK_REFRESH_WAIT_S = saved_wait
MODES["rates_hang_ids"] = set()
release_hang()
ok("a hung room-rate re-quote never holds the option page: the cached sheet is served unchanged",
   elapsed < 1.5 and res["details"]["kind"] == "live" and len(res["details"]["description"]) == 5000
   and "room_offers" not in res["details"] and len(CALLS["lite_rates"]) == 1, (round(elapsed, 2), list(res["details"])))
reset_calls()
res = client.get(f"/trips/{trip_id}/option-details/stay-1", headers=HEADERS).json()
ok("a quick re-quote that answers refreshes the room table on the cached sheet",
   res["details"]["kind"] == "live" and res["details"].get("room_offers") and len(CALLS["lite_rates"]) == 1
   and CALLS["lite_rates"][0]["hotelIds"] == ["lp-r11"], list(res["details"]))

reset_calls()
MODES["gallery_429"] = True
hotel_service._sleep = lambda s: None  # skip the retry backoff on the throttled gallery
res = client.get(f"/trips/{trip_id}/stay-media", headers=HEADERS).json()
hotel_service._sleep = time.sleep
with db.get_conn(readonly=True) as conn:
    frozen = conn.execute("SELECT stay_media FROM trips WHERE id = ?", (trip_id,)).fetchone()["stay_media"]
ok("a throttled gallery fetch returns what it has but is not frozen on the trip",
   res["complete"] is False and frozen is None and "stay-2" in res["media"], (res.get("complete"), frozen, list(res["media"])))
MODES["gallery_429"] = False
reset_calls()
res = client.get(f"/trips/{trip_id}/stay-media", headers=HEADERS).json()
with db.get_conn(readonly=True) as conn:
    frozen = conn.execute("SELECT stay_media FROM trips WHERE id = ?", (trip_id,)).fetchone()["stay_media"]
ok("a complete fetch is cached on the trip", res["complete"] is True and frozen is not None and set(res["media"]) == {"stay-1", "stay-2"})
ok("the hotel page fetched for the gallery is memoized: the retry asked only for the throttled hotel",
   [c["hotelId"] for c in CALLS["gallery"]] == ["lp-r11"], CALLS["gallery"])
reset_calls()
page = hotel_service.real_option_details("stay", options_json["stay"][1], {
    "destination": "Rome, Italy", "check_in": "2026-11-20", "check_out": "2026-11-23", "group_size": 4, "nights": 3})
ok("the property page reuses the gallery's memoized /data/hotel fetch",
   CALLS["gallery"] == [] and page["source"] == "liteapi" and page.get("room_offers"), (CALLS["gallery"], page.get("source")))
no_surprises("(api)")

ok("not one LiteAPI flight or Duffel stays call in the whole run", not EVER_FORBIDDEN, EVER_FORBIDDEN)
print(f"\nall {passed} checks passed")
