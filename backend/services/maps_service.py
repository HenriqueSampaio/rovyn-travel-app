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


def geocode_place(query: str) -> tuple[float, float] | None:
    """(lat, lng) for a free-text place ("Florence, Italy", "Louvre, Paris").
    Cached permanently — coordinates of a named place don't move."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    query = (query or "").strip()
    if not key or not query:
        return None
    cache_key = query.casefold()
    with _geocode_lock:
        cached = _load_geocode_cache().get(cache_key)
    if cached:
        return cached
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
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is None or lng is None:
            return None
        coords = (float(lat), float(lng))
        with _geocode_lock:
            _remember_geocode(cache_key, coords)
        return coords
    except Exception:
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


def route_map_png(points: list[tuple[str, float, float]], w=640, h=320) -> bytes | None:
    """The trip at a glance: numbered pins in route order joined by a clay
    path; a single point becomes one destination pin at city zoom."""
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
        path = "|".join(f"{lat:.5f},{lng:.5f}" for _, lat, lng in points)
        params.append(("path", f"color:{_CLAY_PATH}|weight:3|geodesic:true|{path}"))
    return _static_map_png(params)


def option_map_png(lat: float, lng: float, w=640, h=300, zoom=15) -> bytes | None:
    """Where one venue sits: a single clay pin at street zoom."""
    if lat is None or lng is None:
        return None
    params = _base_params(w, h)
    params.append(("zoom", str(_clamp(zoom, 3, 18, 15))))
    params.append(("markers", f"color:{_CLAY}|{lat:.5f},{lng:.5f}"))
    return _static_map_png(params)
