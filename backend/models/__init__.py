from pydantic import BaseModel, EmailStr


class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class CreateTripRequest(BaseModel):
    name: str
    destination: str = ""  # optional — set it to skip destination voting


class JoinTripRequest(BaseModel):
    invite_code: str


class QuestionnaireData(BaseModel):
    budget_min: int
    budget_max: int
    travel_dates_start: str
    travel_dates_end: str
    departure_city: str = ""
    trip_style: str = "no-preference"  # "single-city" | "multi-city" | "no-preference"
    vibes: list[str]
    accommodation_type: str
    pace: str = "moderately"  # "very" | "moderately" | "slightly" (busy)
    special_requirements: str = ""


class VoteRequest(BaseModel):
    destination_name: str


class StarRequest(BaseModel):
    category: str
    option_id: str
    starred: bool


class CurationRequest(BaseModel):
    category: str
    status: str  # "done" | "no_preference" | "reset"


class EditRequest(BaseModel):
    request: str


class ReorderRequest(BaseModel):
    order: list[int]  # new sequence of current day numbers, e.g. [2, 1, 3]


class SlotEditRequest(BaseModel):
    day: int
    slot: str            # "morning" | "afternoon" | "evening"
    action: str          # "delete" | "replace" | "move_up" | "move_down"
    replacement_id: str = ""  # option id from the planning board pool, for "replace"
