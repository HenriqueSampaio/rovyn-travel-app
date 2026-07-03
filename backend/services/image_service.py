"""Place/activity photos via the Pexels API (stdlib-only, no SDK).

Images are fetched once at generation time and stored inside the trip's
options/plan JSON, so the browser never talks to Pexels, the API key stays
server-side, and page polling costs zero image requests. Without a
PEXELS_API_KEY everything degrades gracefully: image_for() returns None and
the UI simply renders without photos.

Get a free key at https://www.pexels.com/api/ (200 requests/hour).
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

# macOS system Pythons often lack root CAs; certifi's bundle works everywhere
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_cache: dict = {}


def images_available() -> bool:
    return bool(os.environ.get("PEXELS_API_KEY"))


def _photos_for(query: str, orientation: str = "landscape") -> list[dict]:
    """Up to 6 Pexels photos for a query, cached per query. Empty on failure."""
    key = os.environ.get("PEXELS_API_KEY")
    query = (query or "").strip()
    if not key or not query:
        return []
    cache_key = (query.lower(), orientation)
    if cache_key in _cache:
        return _cache[cache_key]

    params = urllib.parse.urlencode({"query": query, "per_page": 6, "orientation": orientation})
    req = urllib.request.Request(
        f"https://api.pexels.com/v1/search?{params}",
        headers={
            "Authorization": key,
            # Cloudflare rejects urllib's default agent with 403 (code 1010)
            "User-Agent": "Rovyn/1.0 (group trip planner)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        photos = [
            {
                "url": (p.get("src") or {}).get("large"),    # ~940px — heroes
                "thumb": (p.get("src") or {}).get("medium"), # ~350px — thumbnails
                "alt": p.get("alt") or query,
                "credit": p.get("photographer", ""),
                "credit_url": p.get("photographer_url", ""),
            }
            for p in data.get("photos") or []
            if (p.get("src") or {}).get("large")
        ]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return []  # never let a photo break trip planning; uncached so it can retry

    _cache[cache_key] = photos
    return photos


def image_for(query: str, orientation: str = "landscape") -> dict | None:
    """Best Pexels photo for a query, or None (no key / no match / API error)."""
    photos = _photos_for(query, orientation)
    return photos[0] if photos else None


def nth_image(query: str, n: int, orientation: str = "landscape") -> dict | None:
    """The n-th photo for a query — lets several items that share one fallback
    query (e.g. four '4-star hotel Porto' stays) get DIFFERENT pictures."""
    photos = _photos_for(query, orientation)
    return photos[n % len(photos)] if photos else None


def first_image(*queries: str) -> dict | None:
    """Try queries in order until one returns a photo."""
    for q in queries:
        img = image_for(q)
        if img:
            return img
    return None
