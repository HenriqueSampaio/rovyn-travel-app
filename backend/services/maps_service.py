"""Rovyn-styled map images via the Google Maps Static API, proxied server-side.

The frontend never talks to Google: trip-scoped backend endpoints build the
Static Maps URL (markers, route path, Rovyn's editorial map style), fetch the
PNG with the server's GOOGLE_PLACES_API_KEY and stream it back. Because a
Static Maps image is a pure function of its URL, every image is cached on disk
(and in memory) forever — one Google call per distinct map, ever.

City/venue coordinates come from Google Places Text Search, cached the same
way (a city's coordinates never change): a module-level dict backed by a JSON
file next to the image cache.

Everything is best-effort: no key, a network hiccup or an unknown place means
None / an empty answer — maps are an enhancement and must never break planning.
"""
import hashlib
import json
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_STATIC_BASE = "https://maps.googleapis.com/maps/api/staticmap"

# Editorial base tuned to Rovyn's palette (pine / clay / cream): cream land,
# pine-tinted water, white roads with no labels, POI and transit clutter off.
_MAP_STYLES = (
    "feature:landscape|element:geometry|color:0xF0EADC",
    "feature:water|element:geometry|color:0xBFD2C8",
    "feature:water|element:labels.text.fill|color:0x8FA69B",
    "feature:poi|visibility:off",
    "feature:poi.park|element:geometry|color:0xDFE4CC|visibility:on",
    "feature:transit|visibility:off",
    "feature:road|element:geometry|color:0xFFFFFF",
    "feature:road|element:labels|visibility:off",
    "feature:road.highway|element:geometry|color:0xEFE7D2",
    "feature:administrative|element:geometry.stroke|color:0xBBB29F",
    "feature:administrative|element:labels.text.fill|color:0x45534C",
    "feature:administrative.land_parcel|visibility:off",
    "feature:administrative.neighborhood|visibility:off",
    "element:labels.text.stroke|color:0xF6F2E8",
    "element:labels.icon|visibility:off",
)
_PINE = "0x2E5D4F"
_CLAY = "0xBE6B3F"
_CLAY_PATH = "0xBE6B3FB3"  # clay at ~70% — the route line

_MARKER_LABELS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

_geocode_lock = threading.Lock()
_geocode_cache: dict[str, tuple[float, float]] | None = None

_image_lock = threading.Lock()
_image_cache: dict[str, bytes] = {}
_IMAGE_CACHE_MAX = 48  # ~50–80 KB each; the disk cache is the real store


def maps_available() -> bool:
    return bool(os.environ.get("GOOGLE_PLACES_API_KEY"))


def _cache_dir() -> Path:
    """Map cache lives next to the SQLite file (backend/data/, gitignored) —
    resolved at call time so tests that repoint the DB get their own cache."""
    from backend import db
    path = Path(db._DB_PATH).parent / "map_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Geocoding — Places Text Search, cached forever
# ---------------------------------------------------------------------------

def _geocode_file() -> Path:
    return _cache_dir() / "geocode.json"


def _load_geocode_cache() -> dict:
    global _geocode_cache
    if _geocode_cache is None:
        try:
            raw = json.loads(_geocode_file().read_text())
            _geocode_cache = {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
        except Exception:
            _geocode_cache = {}
    return _geocode_cache


def _remember_geocode(query: str, coords: tuple[float, float]) -> None:
    cache = _load_geocode_cache()
    cache[query] = coords
    try:
        _geocode_file().write_text(json.dumps({k: list(v) for k, v in cache.items()}))
    except Exception:
        pass  # the in-memory cache still holds it for this process


# The Geocoding API sells coordinates ~6x cheaper than a Places Text Search,
# but it may not be enabled on the project yet — remember a denial for the
# life of the process and fall back to Places without re-asking every call.
_geocoding_api_denied = False


def _geocode_via_geocoding_api(query: str, key: str) -> tuple[float, float] | None:
    global _geocoding_api_denied
    if _geocoding_api_denied:
        return None
    url = ("https://maps.googleapis.com/maps/api/geocode/json?"
           + urllib.parse.urlencode({"address": query, "key": key}))
    try:
        with urllib.request.urlopen(url, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
    except Exception:
        return None
    if data.get("status") == "REQUEST_DENIED":
        _geocoding_api_denied = True
        return None
    loc = ((data.get("results") or [{}])[0].get("geometry") or {}).get("location") or {}
    if loc.get("lat") is None:
        return None
    return (float(loc["lat"]), float(loc["lng"]))


def _geocode_via_places(query: str, key: str) -> tuple[float, float] | None:
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps({"textQuery": query}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": "places.location",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        loc = (data.get("places") or [{}])[0].get("location") or {}
        if loc.get("latitude") is None:
            return None
        return (float(loc["latitude"]), float(loc["longitude"]))
    except Exception:
        return None


def geocode_place(query: str) -> tuple[float, float] | None:
    """(lat, lng) for a free-text place ("Florence, Italy", "Louvre, Paris").
    Cached permanently — coordinates of a named place don't move. Tries the
    cheap Geocoding API first, falls back to Places Text Search when that API
    isn't enabled on the key."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    query = (query or "").strip()
    if not key or not query:
        return None
    cache_key = query.casefold()
    with _geocode_lock:
        cached = _load_geocode_cache().get(cache_key)
    if cached:
        return cached
    coords = _geocode_via_geocoding_api(query, key) or _geocode_via_places(query, key)
    if coords:
        with _geocode_lock:
            _remember_geocode(cache_key, coords)
    return coords


# ---------------------------------------------------------------------------
# Routes API — real travel times and route lines, cached forever
# ---------------------------------------------------------------------------

_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_routes_lock = threading.Lock()
_routes_cache: dict[str, dict | None] | None = None
_routes_api_denied = False  # not enabled on the key — remember per process


def _routes_file() -> Path:
    return _cache_dir() / "routes.json"


def _load_routes_cache() -> dict:
    global _routes_cache
    if _routes_cache is None:
        try:
            _routes_cache = json.loads(_routes_file().read_text())
        except Exception:
            _routes_cache = {}
    return _routes_cache


def _remember_route(key: str, value: dict | None) -> None:
    cache = _load_routes_cache()
    cache[key] = value
    try:
        _routes_file().write_text(json.dumps(cache))
    except Exception:
        pass


def _minutes_label(minutes: int) -> str:
    h, m = divmod(max(int(minutes), 1), 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def compute_route(origin: tuple[float, float], dest: tuple[float, float],
                  mode: str = "TRANSIT") -> dict | None:
    """One computed route between two coordinates:
    {"minutes": 322, "label": "5h 22m", "km": 261.4, "polyline": "enc…"}.

    Planning-grade, so answers cache on disk forever, keyed by ~110m-rounded
    coordinates + mode; "no route exists" (e.g. no transit between islands)
    caches as None too. An API denial (Routes API not enabled on the key) is
    NOT cached — it flips a process flag and everything degrades quietly."""
    global _routes_api_denied
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key or _routes_api_denied or not origin or not dest:
        return None
    cache_key = (f"{origin[0]:.3f},{origin[1]:.3f}->{dest[0]:.3f},{dest[1]:.3f}"
                 f"|{mode.upper()}")
    with _routes_lock:
        cache = _load_routes_cache()
        if cache_key in cache:
            return cache[cache_key]
    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}},
        "travelMode": mode.upper(),
        "polylineQuality": "OVERVIEW",
    }
    try:
        req = urllib.request.Request(
            _ROUTES_URL,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask":
                    "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            _routes_api_denied = True
        return None
    except Exception:
        return None
    routes = data.get("routes") or []
    if not routes:
        with _routes_lock:
            _remember_route(cache_key, None)
        return None
    top = routes[0]
    try:
        minutes = round(int(str(top.get("duration", "0s")).rstrip("s")) / 60)
    except (TypeError, ValueError):
        minutes = 0
    result = {
        "minutes": minutes,
        "label": _minutes_label(minutes),
        "km": round((top.get("distanceMeters") or 0) / 1000, 1),
        "polyline": (top.get("polyline") or {}).get("encodedPolyline") or "",
    }
    if not minutes:
        result = None
    with _routes_lock:
        _remember_route(cache_key, result)
    return result


def leg_travel_facts(from_place: str, to_place: str) -> dict | None:
    """Real inter-city travel times for one route leg: transit and driving,
    e.g. {"transit": "5h 22m", "transit_min": 322, "drive": "3h 05m",
    "drive_min": 185, "km": 261.4}. None when nothing could be computed."""
    a, b = geocode_place(from_place), geocode_place(to_place)
    if not (a and b):
        return None
    facts: dict = {}
    transit = compute_route(a, b, "TRANSIT")
    if transit:
        facts["transit"], facts["transit_min"] = transit["label"], transit["minutes"]
        facts["km"] = transit["km"]
    drive = compute_route(a, b, "DRIVE")
    if drive:
        facts["drive"], facts["drive_min"] = drive["label"], drive["minutes"]
        facts.setdefault("km", drive["km"])
    return facts or None


def travel_from_center(lat: float, lng: float, place: str) -> str | None:
    """One quiet sentence locating a venue relative to its city's center:
    "12 min walk from the center" / "~25 min by transit from the center"."""
    if lat is None or lng is None:
        return None
    center = geocode_place(place)
    if not center:
        return None
    walk = compute_route(center, (lat, lng), "WALK")
    if walk and walk["minutes"] <= 35:
        return f"{walk['minutes']} min walk from the center"
    transit = compute_route(center, (lat, lng), "TRANSIT")
    if transit and transit["minutes"]:
        return f"~{transit['label']} by transit from the center"
    drive = compute_route(center, (lat, lng), "DRIVE")
    if drive and drive["minutes"]:
        return f"~{drive['label']} by car from the center"
    if walk:  # a long but computable walk beats silence
        return f"~{walk['label']} on foot from the center"
    return None


# ---------------------------------------------------------------------------
# Static map images — deterministic URL -> PNG, cached forever
# ---------------------------------------------------------------------------

def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _static_map_png(params: list[tuple[str, str]]) -> bytes | None:
    """Fetch one styled Static Maps image; memory -> disk -> Google."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        return None
    for style in _MAP_STYLES:
        params.append(("style", style))
    query = urllib.parse.urlencode(params, safe=":,|.@")
    url = f"{_STATIC_BASE}?{query}&key={key}"
    # Cache key excludes the API key: a rotated key must not orphan the cache
    digest = hashlib.sha1(query.encode()).hexdigest()

    with _image_lock:
        cached = _image_cache.get(digest)
    if cached:
        return cached
    cache_path = _cache_dir() / f"{digest}.png"
    try:
        png = cache_path.read_bytes()
    except OSError:
        png = None
    if png is None:
        try:
            with urllib.request.urlopen(url, timeout=15, context=_SSL_CTX) as res:
                png = res.read()
        except Exception:
            return None
        if not png or not png.startswith(b"\x89PNG"):
            return None  # an error body, not a map — don't cache it
        try:
            cache_path.write_bytes(png)
        except OSError:
            pass
    with _image_lock:
        if len(_image_cache) >= _IMAGE_CACHE_MAX:
            _image_cache.pop(next(iter(_image_cache)))
        _image_cache[digest] = png
    return png


def _base_params(w, h) -> list[tuple[str, str]]:
    width = _clamp(w, 120, 640, 640)
    height = _clamp(h, 120, 640, 320)
    return [("size", f"{width}x{height}"), ("scale", "2"),
            ("format", "png"), ("language", "en")]


# Static Maps rejects URLs beyond 16,384 chars; leave headroom for styles+key
_URL_BUDGET = 15000


def _leg_paths(points: list[tuple[str, float, float]]) -> list[str]:
    """One path param per consecutive pair: the real transit/driving polyline
    when the Routes API can draw it, a geodesic straight line otherwise. If
    the polylines together would blow the URL budget, fall back to one plain
    geodesic path — a straight 'trip shape' beats no map."""
    real = []
    for (_, alat, alng), (_, blat, blng) in zip(points, points[1:]):
        route = (compute_route((alat, alng), (blat, blng), "TRANSIT")
                 or compute_route((alat, alng), (blat, blng), "DRIVE"))
        poly = (route or {}).get("polyline")
        if poly:
            real.append(f"color:{_CLAY_PATH}|weight:3|enc:{poly}")
        else:
            real.append(f"color:{_CLAY_PATH}|weight:3|geodesic:true|"
                        f"{alat:.5f},{alng:.5f}|{blat:.5f},{blng:.5f}")
    if sum(len(urllib.parse.quote(p)) for p in real) <= _URL_BUDGET:
        return real
    straight = "|".join(f"{lat:.5f},{lng:.5f}" for _, lat, lng in points)
    return [f"color:{_CLAY_PATH}|weight:3|geodesic:true|{straight}"]


def route_map_png(points: list[tuple[str, float, float]], w=640, h=320) -> bytes | None:
    """The trip at a glance: numbered pins in route order joined by the real
    inter-city path (Routes API) or a clay geodesic line; a single point
    becomes one destination pin at city zoom."""
    points = [(label, lat, lng) for label, lat, lng in points
              if lat is not None and lng is not None][:len(_MARKER_LABELS)]
    if not points:
        return None
    params = _base_params(w, h)
    if len(points) == 1:
        _, lat, lng = points[0]
        params.append(("zoom", "11"))
        params.append(("markers", f"color:{_PINE}|{lat:.5f},{lng:.5f}"))
    else:
        for i, (_, lat, lng) in enumerate(points):
            params.append(("markers", f"color:{_PINE}|label:{_MARKER_LABELS[i]}|{lat:.5f},{lng:.5f}"))
        for path in _leg_paths(points):
            params.append(("path", path))
    return _static_map_png(params)


def option_map_png(lat: float, lng: float, w=640, h=300, zoom=15) -> bytes | None:
    """Where one venue sits: a single clay pin at street zoom."""
    if lat is None or lng is None:
        return None
    params = _base_params(w, h)
    params.append(("zoom", str(_clamp(zoom, 3, 18, 15))))
    params.append(("markers", f"color:{_CLAY}|{lat:.5f},{lng:.5f}"))
    return _static_map_png(params)
