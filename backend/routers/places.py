"""City autocomplete endpoint for destination / departure-city inputs."""
from fastapi import APIRouter, Depends, Query

from backend.auth import get_current_user
from backend.services.places_service import suggest_cities

router = APIRouter()


@router.get("/suggest")
def suggest(q: str = Query(default="", max_length=100), _user: dict = Depends(get_current_user)):
    return {"suggestions": suggest_cities(q)}
