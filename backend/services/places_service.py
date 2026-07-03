"""City autocomplete for destination / departure-city inputs.

Two layers, so suggestions always work:
- GOOGLE_PLACES_API_KEY set → Google Places Autocomplete (best fuzzy matching,
  restricted to cities), falling back to offline on any error.
- Always available: an offline GeoNames dataset bundled with the repo
  (backend/assets/cities.json.gz — ~156k places with population > 1,000,
  CC-BY licensed, https://www.geonames.org). Prefix-matched on both the local
  name ("São Paulo") and its ASCII form ("Sao Paulo"), ranked by population.
"""
import gzip
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import certifi
import pycountry

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_DATA_PATH = Path(__file__).parent.parent / "assets" / "cities.json.gz"

_cities: list | None = None          # [(name, ascii_lower, name_lower, country_name, population)]
_country_names: dict[str, str] = {}


def _country_name(cc: str) -> str:
    if cc not in _country_names:
        try:
            country = pycountry.countries.get(alpha_2=cc)
            _country_names[cc] = getattr(country, "common_name", None) or country.name if country else cc
        except (LookupError, AttributeError):
            _country_names[cc] = cc
    return _country_names[cc]


def _load() -> list:
    global _cities
    if _cities is None:
        with gzip.open(_DATA_PATH, "rt", encoding="utf-8") as f:
            raw = json.load(f)  # rows sorted by population desc: [name, ascii_or_empty, cc, pop]
        _cities = [
            (name, (ascii_name or name).casefold(), name.casefold(), _country_name(cc), pop)
            for name, ascii_name, cc, pop in raw
        ]
    return _cities


def _offline_suggest(query: str, limit: int) -> list[dict]:
    q = query.strip().casefold()
    if len(q) < 2:
        return []
    exact, prefix, contains = [], [], []
    # Rows are population-sorted, so the first hits per bucket are the best ones
    for name, ascii_lower, name_lower, country, pop in _load():
        if name_lower == q or ascii_lower == q:
            exact.append((name, country))
        elif name_lower.startswith(q) or ascii_lower.startswith(q):
            if len(prefix) < limit:
                prefix.append((name, country))
        elif len(contains) < limit and (q in name_lower or q in ascii_lower):
            contains.append((name, country))
        if len(exact) >= limit:
            break
    picked = (exact + prefix + contains)[:limit]
    return [{"label": f"{name}, {country}", "city": name, "country": country} for name, country in picked]


def _google_suggest(query: str, limit: int) -> list[dict] | None:
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:autocomplete",
            data=json.dumps({
                "input": query,
                "includedPrimaryTypes": ["locality", "administrative_area_level_3"],
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        out = []
        for s in (data.get("suggestions") or [])[:limit]:
            fmt = (s.get("placePrediction") or {}).get("structuredFormat") or {}
            city = (fmt.get("mainText") or {}).get("text", "")
            rest = (fmt.get("secondaryText") or {}).get("text", "")
            country = rest.split(",")[-1].strip() if rest else ""
            if city:
                out.append({"label": f"{city}, {country}" if country else city,
                            "city": city, "country": country})
        return out or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None  # fall back to offline


def suggest_cities(query: str, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if len(query) < 2:
        return []
    return _google_suggest(query, limit) or _offline_suggest(query, limit)
