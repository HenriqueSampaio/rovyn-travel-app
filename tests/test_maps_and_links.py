"""In-process tests for the map endpoints and the hotel-link healing path.

Runs with plain python (no pytest needed):

    /Users/henriquesampaio/Rovyn/Rovyn/.venv/bin/python tests/test_maps_and_links.py

Everything is isolated: a temp SQLite DB, a temp map cache next to it, and a
fake urllib layer standing in for Google — no network, no real keys.
"""
import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pin every live-integration env var BEFORE backend imports (backend.main
# calls load_dotenv(), which fills only UNSET vars — presetting wins).
os.environ.update({
    "DATABASE_URL": "",
    "SUPABASE_URL": "",
    "SUPABASE_ANON_KEY": "",
    "ALLOW_LEGACY_AUTH": "1",
    "ANTHROPIC_API_KEY": "",
    "GEMINI_API_KEY": "",
    "LITEAPI_API_KEY": "",
    "PEXELS_API_KEY": "",
    "GOOGLE_PLACES_API_KEY": "test-key",
})

TMP = Path(tempfile.mkdtemp(prefix="rovyn-maps-test-"))
import backend.db as db
db._DB_PATH = TMP / "rovyn.db"
db.init_db()

from fastapi.testclient import TestClient
import backend.main  # noqa: E402  (after db repoint)

client = TestClient(backend.main.app)

# ---------------------------------------------------------------------------
# Fake Google: one urlopen stand-in for Places searchText + Static Maps
# ---------------------------------------------------------------------------

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"rovyn-fake-map" * 40

GEOCODES = {
    "rome, italy": (41.8933, 12.4829),
    "florence, italy": (43.7700, 11.2577),
    "venice, italy": (45.4404, 12.3160),
    "sea breeze rooms, venice, italy": (45.4380, 12.3190),
}

PLACES_RESULTS = {
    # The healing lookup for the LiteAPI stay: same property, branded name
    "hotel miramare venice, italy": {
        "displayName": {"text": "Hotel Miramare — Starhotels"},
        "websiteUri": "https://miramare.example.com",
        "googleMapsUri": "https://maps.google.com/?cid=42",
        "location": {"latitude": 45.4341, "longitude": 12.3388},
    },
    # A lookalike that must be rejected by the name guard
    "casa fantini venice, italy": {
        "displayName": {"text": "Lake Time Boutique"},
        "websiteUri": "https://wrong-hotel.example.com",
        "googleMapsUri": "https://maps.google.com/?cid=13",
        "location": {"latitude": 45.0, "longitude": 12.0},
    },
}

CALLS = {"static": [], "places": []}


class _FakeResponse(io.BytesIO):
    status = 200
    headers = {"Content-Type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(req, timeout=None, context=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if url.startswith("https://maps.googleapis.com/maps/api/staticmap"):
        CALLS["static"].append(url)
        return _FakeResponse(FAKE_PNG)
    if "places.googleapis.com/v1/places:searchText" in url:
        query = json.loads(req.data.decode())["textQuery"].casefold()
        CALLS["places"].append(query)
        if query in GEOCODES:
            lat, lng = GEOCODES[query]
            body = {"places": [{"location": {"latitude": lat, "longitude": lng}}]}
        elif query in PLACES_RESULTS:
            body = {"places": [PLACES_RESULTS[query]]}
        else:
            body = {}
        return _FakeResponse(json.dumps(body).encode())
    raise AssertionError(f"unexpected outbound URL in test: {url}")


urllib.request.urlopen = fake_urlopen  # both services resolve this at call time

# ---------------------------------------------------------------------------
# Fixtures: a user, a trip, a crafted board
# ---------------------------------------------------------------------------

signup = client.post("/auth/signup", json={
    "email": "mapper@example.com", "password": "secret123", "name": "Mapper"}).json()
HEADERS = {"Authorization": f"Bearer {signup['token']}"}

trip_id = client.post("/trips", headers=HEADERS, json={
    "name": "Maps test", "destination": "Italy"}).json()["id"]

OPTIONS = {
    "destination": "Rome",
    "country": "Italy",
    "dates": {"start": "2026-09-10", "end": "2026-09-16"},
    "source": "ai",
    "route": {
        "cities": [
            {"city": "Rome, Italy", "town": "Rome", "nights": 2,
             "start": "2026-09-10", "end": "2026-09-12"},
            {"city": "Florence, Italy", "town": "Florence", "nights": 2,
             "start": "2026-09-12", "end": "2026-09-14"},
            {"city": "Venice, Italy", "town": "Venice", "nights": 2,
             "start": "2026-09-14", "end": "2026-09-16"},
        ],
        "legs": [], "reason": "test route", "duration_days": 6,
    },
    "stay": [
        {"id": "stay-lite", "name": "Hotel Miramare", "city": "Venice, Italy",
         "liteapi_hotel_id": "lp999", "price_per_night": 180, "total_per_person": 260,
         "official_website": None,
         "details": {"kind": "live", "source": "liteapi",
                     "description": "A canalside hotel.", "address": "Riva degli Schiavoni"}},
        {"id": "stay-reject", "name": "Casa Fantini", "city": "Venice, Italy",
         "liteapi_hotel_id": "lp998", "price_per_night": 150, "total_per_person": 220,
         "official_website": None,
         "details": {"kind": "live", "source": "liteapi", "description": "Quiet rooms."}},
        {"id": "stay-plain", "name": "Sea Breeze Rooms", "city": "Venice, Italy",
         "price_per_night": 90, "total_per_person": 140, "official_website": None},
    ],
    "visits": [
        {"id": "visit-cached", "name": "Uffizi Gallery", "city": "Florence, Italy",
         "cost_per_person": 20,
         "details": {"kind": "live", "source": "places", "address": "Piazzale degli Uffizi",
                     "lat": 43.7678, "lng": 11.2553}},
    ],
    "events": [],
    "flights": {"New York": [{"id": "fl-1", "airline": "TestAir", "est_price_per_person": 500}]},
    "transport": {},
}

with db.get_conn() as conn:
    conn.execute("UPDATE trips SET options = ?, status = 'curating' WHERE id = ?",
                 (json.dumps(OPTIONS), trip_id))

passed = 0


def ok(label, cond, extra=""):
    global passed
    assert cond, f"FAIL: {label} {extra}"
    passed += 1
    print(f"  ok - {label}")


# ---------------------------------------------------------------------------
# 1. Route map
# ---------------------------------------------------------------------------
print("route map:")
res = client.get(f"/trips/{trip_id}/map/route", headers=HEADERS)
ok("returns 200 image/png", res.status_code == 200
   and res.headers["content-type"] == "image/png", res.text[:200])
ok("body is the PNG", res.content == FAKE_PNG)
ok("cache-control set", "max-age" in res.headers.get("cache-control", ""))

static_url = CALLS["static"][-1]
ok("three numbered pine pins",
   all(f"markers=color:0x2E5D4F|label:{n}|" in static_url.replace("%7C", "|")
       or f"markers=color%3A0x2E5D4F%7Clabel%3A{n}" in static_url
       for n in "123") or static_url.count("markers=") == 3, static_url)
ok("clay path connects the cities", "path=" in static_url and "geodesic" in static_url)
ok("Rovyn style applied", "style=" in static_url and "0xF0EADC" in static_url)
ok("geocoded all three cities",
   {"rome, italy", "florence, italy", "venice, italy"} <= set(CALLS["places"]))

geocode_file = TMP / "map_cache" / "geocode.json"
ok("geocode disk cache written", geocode_file.exists()
   and len(json.loads(geocode_file.read_text())) >= 3)

n_static, n_places = len(CALLS["static"]), len(CALLS["places"])
res2 = client.get(f"/trips/{trip_id}/map/route", headers=HEADERS)
ok("second call serves from cache (no Google traffic)",
   res2.status_code == 200 and len(CALLS["static"]) == n_static
   and len(CALLS["places"]) == n_places)

# ---------------------------------------------------------------------------
# 2. Option maps
# ---------------------------------------------------------------------------
print("option map:")
res = client.get(f"/trips/{trip_id}/map/option/visit-cached", headers=HEADERS)
ok("uses cached detail coords", res.status_code == 200
   and "43.76780,11.25530" in urllib.parse.unquote(CALLS["static"][-1]),
   (res.status_code, urllib.parse.unquote(CALLS["static"][-1])))
ok("clay pin for the venue", "color:0xBE6B3F" in urllib.parse.unquote(CALLS["static"][-1]))

res = client.get(f"/trips/{trip_id}/map/option/stay-plain", headers=HEADERS)
ok("falls back to geocoding name + city", res.status_code == 200
   and "sea breeze rooms, venice, italy" in CALLS["places"])

res = client.get(f"/trips/{trip_id}/map/option/fl-1", headers=HEADERS)
ok("flights have no location map (404)", res.status_code == 404)

res = client.get(f"/trips/{trip_id}/map/option/nope", headers=HEADERS)
ok("unknown option 404s", res.status_code == 404)

res = client.get(f"/trips/{trip_id}/map/route")
ok("maps require auth", res.status_code in (401, 403))

# ---------------------------------------------------------------------------
# 3. Detail-page healing: old LiteAPI caches gain website + maps link
# ---------------------------------------------------------------------------
print("hotel link healing:")
res = client.get(f"/trips/{trip_id}/option-details/stay-lite", headers=HEADERS)
d = res.json()["details"]
ok("cached liteapi details gain the official site", d.get("website") == "https://miramare.example.com", d)
ok("and the Maps link", d.get("maps_url") == "https://maps.google.com/?cid=42")
ok("and coordinates", d.get("lat") == 45.4341 and d.get("lng") == 12.3388)
ok("liteapi facts kept winning", d.get("description") == "A canalside hotel.")

with db.get_conn(readonly=True) as conn:
    stored = json.loads(conn.execute(
        "SELECT options FROM trips WHERE id = ?", (trip_id,)).fetchone()["options"])
stored_stay = next(o for o in stored["stay"] if o["id"] == "stay-lite")
ok("healed links persisted on the trip",
   stored_stay["details"].get("website") == "https://miramare.example.com"
   and stored_stay["details"].get("links_checked") is True)

n_places = len(CALLS["places"])
client.get(f"/trips/{trip_id}/option-details/stay-lite", headers=HEADERS)
ok("second open does not re-query Places", len(CALLS["places"]) == n_places)

res = client.get(f"/trips/{trip_id}/option-details/stay-reject", headers=HEADERS)
d = res.json()["details"]
ok("mismatched property name drops the link", not d.get("website"), d)
with db.get_conn(readonly=True) as conn:
    stored = json.loads(conn.execute(
        "SELECT options FROM trips WHERE id = ?", (trip_id,)).fetchone()["options"])
stored_stay = next(o for o in stored["stay"] if o["id"] == "stay-reject")
ok("rejection remembered (no re-query forever)",
   stored_stay["details"].get("links_checked") is True)

# ---------------------------------------------------------------------------
# 4. Name guard unit checks
# ---------------------------------------------------------------------------
print("name guard:")
from backend.services.hotel_service import _names_match
ok("rebranding passes", _names_match("Hotel Lutetia", "Mandarin Oriental Lutetia, Paris"))
ok("brand suffix passes", _names_match("Grand Hotel Continental",
                                       "Grand Hotel Continental Siena – Starhotels Collezione"))
ok("different property rejected", not _names_match("Casa Fantini", "Lake Time Boutique"))
ok("generic-word-only overlap rejected", not _names_match("Hotel Roma", "Hotel Excelsior"))

# ---------------------------------------------------------------------------
# 5. Sample mode: no key -> maps 404 politely, planning unaffected
# ---------------------------------------------------------------------------
print("no-key mode:")
os.environ["GOOGLE_PLACES_API_KEY"] = ""
res = client.get(f"/trips/{trip_id}/map/route", headers=HEADERS)
ok("route map 404s without a key", res.status_code == 404)
res = client.get(f"/trips/{trip_id}/map/option/visit-cached", headers=HEADERS)
ok("option map 404s without a key", res.status_code == 404)
res = client.get(f"/trips/{trip_id}/options", headers=HEADERS)
ok("the board itself still serves", res.status_code == 200
   and len(res.json()["options"]["stay"]) == 3)
os.environ["GOOGLE_PLACES_API_KEY"] = "test-key"

print(f"\nall {passed} checks passed")
