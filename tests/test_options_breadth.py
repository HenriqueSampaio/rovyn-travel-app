"""Wide, explained stay & flight options — docs/OPTIONS-BREADTH-SPEC.md.

Runs with plain python (no pytest needed):

    /Users/henriquesampaio/Rovyn/Rovyn/.venv/bin/python tests/test_options_breadth.py

Isolated like tests/test_maps_and_links.py: env pinned before backend
imports, a temp SQLite + map cache, and a fake urllib layer that RECORDS every
outbound call and raises on anything unstubbed — the services' catch-alls
would otherwise turn an unstubbed call into [] silently, so every scenario
also asserts CALLS["unexpected"] is empty. Fixtures come from
tests/fixtures/*.json; every number asserted below is a documented formula
over those fixture fields.
"""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
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
    "DUFFEL_STAYS": "",
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
from backend.services import ai_service, duffel_service, hotel_service, option_ranking as ranking  # noqa: E402
from backend.services.flight_service import live_flight_details, real_flight_options  # noqa: E402
from backend.services.hotel_service import real_stay_options  # noqa: E402

client = TestClient(backend.main.app)


def load(name):
    return json.loads((FIX / name).read_text())


LIST = load("liteapi_hotels_list.json")
VILLAS = load("liteapi_hotels_villas.json")
FACILITIES = load("liteapi_facilities.json")
RATES = load("liteapi_rates_wide.json")
FLIGHTS = load("liteapi_flights_rates.json")
HOME = load("liteapi_flights_home.json")
AIRPORTS = load("liteapi_airports.json")
DUFFEL_OFFERS = load("duffel_offers.json")
DUFFEL_STAYS = load("duffel_stays.json")
ALL_HOTELS = {h["id"]: h for h in LIST["data"] + VILLAS["data"]}
CENTER = (41.8933, 12.4829)
GEOCODES = {"rome, italy": CENTER, "florence, italy": (43.7700, 11.2577), "venice, italy": (45.4404, 12.3160)}

# ---------------------------------------------------------------------------
# Fake network: LiteAPI, Duffel and Google, every call recorded
# ---------------------------------------------------------------------------

MODES = {
    "city": "normal",      # normal | empty (204) | thin (two hotels)
    "batch": "normal",     # normal | boom (400 on the lp-boom batch) | hang | thin
    "duffel_currency": "USD",
    "gallery_429": False,
}
HANG_SECONDS = 4.0
CALLS: dict[str, list] = {}


def reset_calls():
    for key in ("lite_list", "lite_rates", "lite_facilities", "flights", "airports", "verify",
                "duffel_offers", "duffel_offer_get", "duffel_stays", "geocode", "places",
                "gallery", "reviews", "routes", "unexpected"):
        CALLS[key] = []


reset_calls()


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


def _one_way_out():
    """The outbound halves of the round trips, priced at 55 % — what the
    open-jaw fallback's one-way search answers."""
    out = []
    for j in FLIGHTS["data"][0]["journeys"]:
        jj = json.loads(json.dumps(j))
        jj["segments"] = [s for s in j["segments"] if s["direction"] == "OUTBOUND"]
        jj["legDurations"] = j["legDurations"][:1]
        rt = j["cheapestOffer"]["pricing"]["display"]["perPassenger"]["adult"]["total"]
        jj["cheapestOffer"]["pricing"] = {"display": {"currency": "USD",
                                                      "perPassenger": {"adult": {"total": round(rt * 0.55)}}}}
        jj["journeyKey"] = j["journeyKey"] + "-ow"
        out.append(jj)
    return {"data": [{"journeys": out}]}


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
            if MODES["batch"] == "hang" and "lp-boom" in ids:
                time.sleep(HANG_SECONDS)
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
        ids = [h["id"] for h in LIST["data"]][: body.get("limit", 200)]
        if MODES["city"] == "thin":
            ids = ids[:2]
        return _json(_rates_response(ids))
    if url.startswith("https://api.liteapi.travel/v3.0/data/flights/airports"):
        CALLS["airports"].append(qs.get("q"))
        return _json(AIRPORTS.get((qs.get("q") or "").lower(), {"data": []}))
    if url.startswith("https://api.liteapi.travel/v3.0/flights/rates"):
        CALLS["flights"].append(body)
        legs = body.get("legs") or []
        if len(legs) == 1 and legs[0]["origin"] == "FLR":
            return _json(HOME)
        if len(legs) == 1:
            return _json(_one_way_out())
        return _json(FLIGHTS)
    if url.startswith("https://api.liteapi.travel/v3.0/flights/verify"):
        CALLS["verify"].append(body)
        return _json({"data": {"pricing": {"display": {"perPassenger": {"adult": {"total": 377}}}}}})
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
    if url.startswith("https://api.duffel.com/air/offer_requests"):
        CALLS["duffel_offers"].append(body)
        payload = json.loads(json.dumps(DUFFEL_OFFERS))
        for o in payload["data"]["offers"]:
            o["total_currency"] = MODES["duffel_currency"]
        return _json(payload)
    if url.startswith("https://api.duffel.com/air/offers/"):
        CALLS["duffel_offer_get"].append(url)
        return _json({"data": {"id": url.rsplit("/", 1)[1], "total_amount": "372.00", "total_currency": "USD",
                               "passengers": [{"id": "pas_1"}], "expires_at": "2099-01-01T00:00:00Z"}})
    if url.startswith("https://api.duffel.com/stays/search"):
        CALLS["duffel_stays"].append(body)
        return _json(DUFFEL_STAYS)
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
ok("a budget too small to mean anything -> no band", ranking.lodging_band([(100, 200)] * 4, 5) is None)
ok("zero nights -> no band", ranking.lodging_band([(300, 900)], 0) is None)
ok("city share scales with nights",
   ranking.band_for_nights(band, 1) == {"low": round(band["low"] / 3), "high": round(band["high"] / 3), "nights": 1})

# ---------------------------------------------------------------------------
# 2. Single-base wide search: 12 stays, lanes, reasons, top-ups
# ---------------------------------------------------------------------------
print("single-base wide search:")
reset_calls()
VOTES = {"boutique hotel": 3, "airbnb/vacation rental": 1}
PREFS = prefs_for(VOTES)
report = {}
stays = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES,
                          prefs=PREFS, target=12, report=report)
no_surprises("(single-base)")
ok("12 options", len(stays) == 12, len(stays))
ok("every option carries a lane from the lane list",
   all(o.get("lane") in ranking.STAY_LANES for o in stays), [o.get("lane") for o in stays])
ok("the first option is the best match", stays[0]["lane"] == "Best match")
ok("no hotel appears twice", len(by_hotel(stays)) == len(stays))
ok("every option has reasons, a supplier and a sandbox flag",
   all(o["reasons"] and o["supplier"] == "liteapi" and o["sandbox"] is True for o in stays))
ok("why = lane · strongest reason", all(o["why"].startswith(o["lane"] + " · ") for o in stays),
   [o["why"] for o in stays][:3])
ok("every reason carries a short chip label (<= 18 chars)",
   all(0 < len(r["short"]) <= 18 for o in stays for r in o["reasons"]))
ok("ratings are /10 and say so", all(o["rating_scale"] == "10" for o in stays if o["rating10"] is not None))
ok("match scores are 0–100", all(0 <= o["match_score"] <= 100 for o in stays), [o["match_score"] for o in stays])
ok("the existing option fields survive",
   all({"name", "type", "area", "price_per_night", "total_per_person", "rating", "review_count",
        "amenities", "why", "image", "live_price", "liteapi_hotel_id", "official_website"} <= set(o) for o in stays))

chosen = by_hotel(stays)
types = {hid: ALL_HOTELS[hid]["hotelTypeId"] for hid in chosen}
ok("the boutique vote is represented by a core match (B&B / guest house / inn)",
   any(t in ranking.VOTE_TYPE_IDS["boutique hotel"][0] for t in types.values()), types)
ok("the apartment-or-villa vote is represented by a core match",
   any(t in ranking.VOTE_TYPE_IDS["airbnb/vacation rental"][0] for t in types.values()), types)
ok("the cheapest priced hotel is on the board", "lp-r01" in chosen, sorted(chosen))
ok("the best-rated priced hotel is on the board", "lp-r14" in chosen, sorted(chosen))
ok("lane sequence follows the claim order",
   [o["lane"] for o in stays][:1] == ["Best match"]
   and stays[1]["lane"] in ("Your voted style", "Best value", "Top rated", "Best match"))

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
   and report["truncated"] is False and report["mode"] == "city", report)
ok("no official-website lookups at board time", CALLS["places"] == [])
ok("the facilities catalog was fetched once", len(CALLS["lite_facilities"]) == 1)

low, high = band["low"], band["high"]
if "lp-r10" in chosen:  # Hotel Forum Roma: 1180 / 4 travelers
    forum = chosen["lp-r10"]
    texts = [r["text"] for r in forum["reasons"]]
    ok("budget reason prints the per-person total against the band",
       f"{money(1180 / 4)} per person for 3 nights — inside your {money(low)}–{money(high)} stay budget" in texts, texts)
    ok("rating reason = score/10 from N reviews", "8.7/10 from 1,240 guest reviews" in texts, texts)
    ok("location reason = walking minutes at 80 m/min",
       f"{walk_minutes('lp-r10')} min walk from the center" in texts, texts)
    ok("a plain hotel is 'close to' the boutique vote, not a core match",
       "Close to the boutique hotel style you asked for" in texts, texts)
eden = chosen.get("lp-r14")
texts = [r["text"] for r in eden["reasons"]]
ok("Eden ($650 pp, 1.49x the ceiling) is a splurge, numerically",
   f"{money(650)} per person for 3 nights — {money(650 - high)} per person over the group's ceiling" in texts
   or (eden["lane"] == "Splurge" and f"{money(650 - high)} per person over the group's ceiling" in texts), texts)
ok("Eden's rating reason", "Top-rated: 9.4/10 from 1,800 guest reviews" in texts, texts)
ok("vibe reason comes from facilities, not the name",
   "Spa & Pool — matches the group's relaxation vibe" in texts, texts)
ok("facilities become amenities on the card", eden["amenities"][:2] == ["Free WiFi", "Swimming pool"])
if "lp-r28" in chosen:  # Inn at the Spanish Steps: 1800 / 4 = 450 — 1.03x the ceiling
    texts = [r["text"] for r in chosen["lp-r28"]["reasons"]]
    ok("a stretch (<= 1.3x) names the overage against the ceiling",
       f"{money(450)} per person for 3 nights — {money(450 - high)} over your {money(high)} stay ceiling" in texts, texts)
if "lp-r05" in chosen:  # B&B Navona Charm: a core boutique match
    texts = [r["text"] for r in chosen["lp-r05"]["reasons"]]
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
plain = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", {},
                          prefs=prefs_for({}), target=12, report=r2)
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

# ---------------------------------------------------------------------------
# 4. Fallbacks: empty by-city answer -> id batches; a failing batch; a hang
# ---------------------------------------------------------------------------
print("fallbacks:")
reset_calls()
MODES["city"], MODES["batch"] = "empty", "boom"
hotel_service._LITE_RATES_BATCH = 10
r4 = {}
boomed = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES,
                           prefs=PREFS, target=12, report=r4)
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
MODES["batch"] = "hang"
os.environ["STAY_SEARCH_DEADLINE_S"] = "1.5"
started = time.monotonic()
r5 = {}
hung = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES,
                         prefs=PREFS, target=12, report=r5)
elapsed = time.monotonic() - started
os.environ["STAY_SEARCH_DEADLINE_S"] = "20"
ok("a hung batch cannot hold the board past the deadline",
   elapsed < HANG_SECONDS - 0.5 and r5["truncated"] is True, (round(elapsed, 2), r5))
ok("the batches that answered still make the board", len(hung) >= 4, len(hung))
MODES["city"], MODES["batch"] = "normal", "normal"
hotel_service._LITE_RATES_BATCH = 50
time.sleep(HANG_SECONDS)  # let the hung fake thread finish before the next scenario counts calls

# ---------------------------------------------------------------------------
# 5. Thin pricing: one re-price, then the board pads with estimates
# ---------------------------------------------------------------------------
print("thin pricing:")
reset_calls()
MODES["city"], MODES["batch"] = "thin", "thin"
r6 = {}
thin = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES,
                         prefs=PREFS, target=12, report=r6)
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
ok("estimates say est. on every reason that prints a number",
   all(r["text"].startswith("est. ") for o in padding for r in o["reasons"] if any(ch.isdigit() for ch in r["text"])))
ok("estimates carry no match score", all(o["match_score"] is None for o in padding))
ok("the board carries the schema version, prefs and band for the UI",
   board["schema_version"] == 2 and board["group_prefs"]["accommodation_votes"] == {"boutique hotel": 3, "airbnb/vacation rental": 1}
   and board["lodging_band"] == PREFS["lodging_band"] and board["generated_at"] and board["stay_search"]["priced"] == 2,
   {k: board.get(k) for k in ("schema_version", "group_prefs", "lodging_band", "stay_search")})

# ---------------------------------------------------------------------------
# 6. A tiny budget: no budget talk, no splurge among the cheapest third
# ---------------------------------------------------------------------------
print("tiny budget:")
reset_calls()
tiny = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES,
                         prefs=prefs_for(VOTES, budget=(100, 240)), target=12, report={})
no_surprises("(tiny)")
ok("no band -> no budget reasons at all", all(r["kind"] != "budget" for o in tiny for r in o["reasons"]))
prices = sorted(o["total_per_person"] for o in tiny)
cut = prices[len(prices) // 3]
ok("nothing in the cheapest third is called a splurge",
   all(not (o["lane"] == "Splurge" or any("splurge" in r["text"].casefold() for r in o["reasons"]))
       for o in tiny if o["total_per_person"] <= cut))
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
ok("sample reasons are estimates", all(o["match_score"] is None for o in labeled)
   and all(r["text"].startswith("est. ") for o in labeled for r in o["reasons"] if any(ch.isdigit() for ch in r["text"])))
voted = ranking.annotate_stays(sample["stay"], prefs_for({"hostel": 2, "luxury resort": 1}), 3, "sample")
ok("a hostel vote puts the sample hostel up front, and the resort vote is covered too",
   any(o["lane"] in ("Best match", "Your voted style") and "hostel" in o["type"] for o in voted[:3])
   and any("resort" in o["type"] and o["lane"] in ("Best match", "Your voted style", "Splurge") for o in voted), [(o["type"], o["lane"]) for o in voted])

# ---------------------------------------------------------------------------
# 8. Flights: eight roles, LiteAPI + Duffel merged, pace-aware, re-verified
# ---------------------------------------------------------------------------
print("flights:")
reset_calls()
flights = real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", travelers=4, pace="moderately")
no_surprises("(flights)")
lanes = [f["lane"] for f in flights]
ok("eight itineraries, eight distinct lanes", len(flights) == 8 and len(set(lanes)) == 8, lanes)
ok("both suppliers were searched, once each",
   len(CALLS["flights"]) == 1 and len(CALLS["duffel_offers"]) == 1, (len(CALLS["flights"]), len(CALLS["duffel_offers"])))
ok("Duffel was asked for the same slices and party",
   CALLS["duffel_offers"][0]["data"]["slices"][0] == {"origin": "NYC", "destination": "FCO", "departure_date": "2026-11-20"}
   and len(CALLS["duffel_offers"][0]["data"]["passengers"]) == 4, CALLS["duffel_offers"][0])
cheapest = next(f for f in flights if f["lane"] == "Cheapest")
ok("the identical itinerary from both suppliers appears once — the cheaper Duffel copy",
   cheapest["est_price_per_person"] == 378 and cheapest["supplier"] == "duffel"
   and cheapest["duffel_offer_id"] == "off_dup_j01" and cheapest["offer_ids"] == [], cheapest)
prices = sorted([378, 386, 820, 905, 520, 610, 650, 560, 590, 640, 700, 760, 1180, 395, 870, 705])
median = (prices[7] + prices[8]) / 2
ok("the cheapest reason = median gap over the merged pool",
   cheapest["reasons"][0]["text"] == f"Cheapest live fare — ${int(median - 378)} under the median option",
   cheapest["reasons"])
ok("why = the lane reason only; bags live in reasons",
   cheapest["why"] == cheapest["reasons"][0]["text"] and any(r["kind"] == "bags" for r in cheapest["reasons"]))
ok("every option: reasons[0] is its lane reason, sandbox is flagged, currency is set",
   all(f["reasons"][0]["kind"] == "lane" and f["sandbox"] is True and f["currency"] == "USD" for f in flights))
LANE_RE = {
    "Cheapest": "Cheapest live fare", "Nonstop": "Nonstop both ways", "Fastest": "Fastest routing",
    "Best balance": "Within $", "Bags included": "Checked bag included", "Early arrival": "Lands ",
    "No early start": "Departs ", "Late return": "Home flight at ", "One airline": " both ways — one check-in",
}
ok("each lane reason reads as its lane", all(LANE_RE[f["lane"]] in f["reasons"][0]["text"] for f in flights),
   [(f["lane"], f["reasons"][0]["text"]) for f in flights])
early = next(f for f in flights if f["lane"] == "Early arrival")
ok("early arrival = the earliest local landing, with its date",
   early["arrive"] == "2026-11-21 06:30"
   and early["reasons"][0]["text"] == "Lands 06:30 on Nov 21 — most of day one in Rome", early["reasons"])
ok("the price gap reason is relative to the cheapest fare",
   all(f"${f['est_price_per_person'] - 378} more than the cheapest fare" in [r["text"] for r in f["reasons"]]
       for f in flights if f["lane"] != "Cheapest"))
ok("near-duplicates never earn a slot", all(f["journey_key"] != "j02" for f in flights))
ok("match scores and roles are present", all(0 < f["match_score"] <= 100 and f["role"] for f in flights))

reset_calls()
busy = real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", travelers=4, pace="very")
ok("a very busy group sees Early arrival claimed second", [f["lane"] for f in busy][:2] == ["Cheapest", "Early arrival"],
   [f["lane"] for f in busy])
relaxed = real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", travelers=4, pace="slightly")
ok("a relaxed group sees No early start claimed second", [f["lane"] for f in relaxed][:2] == ["Cheapest", "No early start"],
   [f["lane"] for f in relaxed])

reset_calls()
MODES["duffel_currency"] = "GBP"
gbp = real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", travelers=4)
MODES["duffel_currency"] = "USD"
gbp_cheapest = next(f for f in gbp if f["lane"] == "Cheapest")
ok("a GBP Duffel copy never replaces the USD LiteAPI fare",
   gbp_cheapest["supplier"] == "liteapi" and gbp_cheapest["est_price_per_person"] == 380, gbp_cheapest)
ok("non-USD offers are left off the board entirely", all(f["supplier"] == "liteapi" for f in gbp))

reset_calls()
fresh = live_flight_details(cheapest)
ok("a Duffel option re-verifies through Duffel, not LiteAPI",
   fresh["source"] == "duffel" and fresh["price_fresh"] is True and fresh["price_per_person"] == 372
   and len(CALLS["duffel_offer_get"]) == 1 and CALLS["verify"] == [], fresh)
lite_opt = next(f for f in flights if f["supplier"] == "liteapi")
reset_calls()
fresh = live_flight_details(lite_opt)
ok("a LiteAPI option re-verifies through /flights/verify",
   fresh["source"] == "liteapi" and fresh["price_fresh"] is True and len(CALLS["verify"]) == len(lite_opt["offer_ids"]), fresh)

# ---------------------------------------------------------------------------
# 9. Open-jaw: paired one-ways earn their lanes
# ---------------------------------------------------------------------------
print("open-jaw:")
reset_calls()
jaw = real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", travelers=4,
                          return_from_city="Florence, Italy", pace="moderately")
no_surprises("(open-jaw)")
ok("the provider ignored the open jaw -> two one-way searches", len(CALLS["flights"]) == 3, len(CALLS["flights"]))
ok("at least six distinct lanes from the pairing", len(jaw) >= 6 and len({f["lane"] for f in jaw}) == len(jaw),
   [f["lane"] for f in jaw])
ok("every pair says it is two one-way fares and flies home from Florence",
   all("Priced as two one-way fares" in f["why"] and f["return_route"].startswith("FLR") for f in jaw))
late = next((f for f in jaw if f["lane"] == "Late return"), None)
ok("the late-return pair really leaves Florence after 15:00", late is not None and late["return"] >= "2026-11-25 15:00", late)

# ---------------------------------------------------------------------------
# 10. No keys, no calls
# ---------------------------------------------------------------------------
print("no keys:")
reset_calls()
os.environ["LITEAPI_API_KEY"], os.environ["DUFFEL_ACCESS_TOKEN"] = "", ""
ok("no LiteAPI key -> no stays, no calls",
   real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES, prefs=PREFS) == []
   and CALLS["lite_list"] == [] and CALLS["lite_rates"] == [])
ok("no keys -> no flights, no calls",
   real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", 4) == []
   and CALLS["flights"] == [] and CALLS["duffel_offers"] == [])
os.environ["LITEAPI_API_KEY"] = "sand_test_key"
reset_calls()
only_lite = real_flight_options("New York", "Rome, Italy", "2026-11-20", "2026-11-25", 4)
ok("no Duffel token -> LiteAPI alone, no Duffel call",
   len(only_lite) == 8 and CALLS["duffel_offers"] == [] and all(f["supplier"] == "liteapi" for f in only_lite))
os.environ["DUFFEL_ACCESS_TOKEN"] = "duffel_test_token"

# ---------------------------------------------------------------------------
# 11. Duffel stays: mapped and deduped, wired only behind DUFFEL_STAYS=1
# ---------------------------------------------------------------------------
print("duffel stays:")
reset_calls()
recs = duffel_service.search_stays(CENTER[0], CENTER[1], "2026-11-20", "2026-11-23", 2, 4)
ok("search maps USD results and drops the EUR one",
   [r["name"] for r in recs] == ["Hotel Forum Rome", "Trastevere Loft Apartments"], [r["name"] for r in recs])
loft = recs[1]
ok("a Duffel record carries price, score, facilities and its own detail sheet",
   loft["price_total"] == 960.0 and loft["rating10"] == 8.6 and loft["facilities"] == ["Free WiFi", "Washer"]
   and loft["type_label"] == "apartment" and loft["details"]["source"] == "duffel" and len(loft["details"]["photos"]) == 2, loft)
reset_calls()
not_wired = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES, prefs=PREFS, target=12, report={})
ok("without DUFFEL_STAYS the board never calls Duffel stays",
   CALLS["duffel_stays"] == [] and all(o["supplier"] == "liteapi" for o in not_wired))
os.environ["DUFFEL_STAYS"] = "1"
reset_calls()
r11 = {}
wired = real_stay_options("Rome, Italy", "2026-11-20", "2026-11-23", 4, 3, "", VOTES, prefs=PREFS, target=40, report=r11)
os.environ["DUFFEL_STAYS"] = ""
no_surprises("(duffel stays)")
duffel_opts = [o for o in wired if o["supplier"] == "duffel"]
ok("with the flag, the unique Duffel apartment joins the pool, unbookable and detailed",
   len(CALLS["duffel_stays"]) == 1 and r11["duffel"] == 1 and [o["name"] for o in duffel_opts] == ["Trastevere Loft Apartments"]
   and duffel_opts[0]["bookable"] is False and "liteapi_hotel_id" not in duffel_opts[0]
   and duffel_opts[0]["duffel_details"]["source"] == "duffel" and duffel_opts[0]["live_price"] is True, duffel_opts)
ok("the Duffel copy of a LiteAPI hotel is dropped (name + 150 m)", all(o["name"] != "Hotel Forum Rome" for o in wired))
ok("the Duffel stay's option page comes from its own payload",
   hotel_service.real_option_details("stay", duffel_opts[0], {"destination": "Rome, Italy"})["source"] == "duffel")

# ---------------------------------------------------------------------------
# 12. Multi-city board end to end (sample mode): per-city sets, provenance
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
ok("provenance: suppliers listed, sandbox flights flagged",
   board["stay_suppliers"] == ["liteapi"] and set(board["flight_suppliers"]) == {"liteapi", "duffel"}
   and board["flights_sandbox"] is True and board["stay_all_live"] is True, (board["stay_suppliers"], board["flight_suppliers"]))
ok("flights per departure city: eight, laned, with the pace order",
   len(board["flights"]["New York"]) == 8 and board["flights"]["New York"][1]["lane"] == "Early arrival")
ok("option ids are assigned after ranking", all(o["id"].startswith("stay-") for o in board["stay"]))

# ---------------------------------------------------------------------------
# 13. The API: lighter board payload, media not frozen when throttled
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
         "details": {"kind": "live", "source": "liteapi", "description": "x" * 5000, "address": "Via Nazionale"}},
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
MODES["gallery_429"] = True
res = client.get(f"/trips/{trip_id}/stay-media", headers=HEADERS).json()
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
no_surprises("(api)")

print(f"\nall {passed} checks passed")
