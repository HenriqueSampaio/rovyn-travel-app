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


def image_for(query: str, orientation: str = "landscape") -> dict | None:
    """Best Pexels photo for a query, or None (no key / no match / API error)."""
    key = os.environ.get("PEXELS_API_KEY")
    query = (query or "").strip()
    if not key or not query:
        return None
    cache_key = (query.lower(), orientation)
    if cache_key in _cache:
        return _cache[cache_key]

    params = urllib.parse.urlencode({"query": query, "per_page": 1, "orientation": orientation})
    req = urllib.request.Request(
        f"https://api.pexels.com/v1/search?{params}",
        headers={
            "Authorization": key,
            # Cloudflare rejects urllib's default agent with 403 (code 1010)
            "User-Agent": "Rovyn/1.0 (group trip planner)",
        },
    )
    result = None
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as res:
            data = json.loads(res.read().decode())
        photos = data.get("photos") or []
        if photos:
            p = photos[0]
            src = p.get("src", {})
            result = {
                "url": src.get("large"),        # ~940px — heroes
                "thumb": src.get("medium"),     # ~350px — list thumbnails
                "alt": p.get("alt") or query,
                "credit": p.get("photographer", ""),
                "credit_url": p.get("photographer_url", ""),
            }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None  # never let a photo break trip planning; uncached so it can retry

    _cache[cache_key] = result
    return result


def first_image(*queries: str) -> dict | None:
    """Try queries in order until one returns a photo."""
    for q in queries:
        img = image_for(q)
        if img:
            return img
    return None
