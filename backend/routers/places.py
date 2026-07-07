"""City autocomplete endpoint for destination / departure-city inputs."""
import os

from fastapi import APIRouter, Depends, Query

from backend.auth import get_current_user
from backend.services.places_service import suggest_cities

router = APIRouter()


@router.get("/suggest")
def suggest(q: str = Query(default="", max_length=100), _user: dict = Depends(get_current_user)):
    return {"suggestions": suggest_cities(q)}


@router.get("/maps-embed-key")
def maps_embed_key(_user: dict = Depends(get_current_user)):
    """The browser key for Google's Maps Embed API — a SEPARATE key from the
    server one, referrer-restricted and embed-only, so exposing it to the
    client is by design (and the Embed API is free). null = static maps only."""
    return {"key": os.environ.get("GOOGLE_MAPS_EMBED_KEY") or None}
