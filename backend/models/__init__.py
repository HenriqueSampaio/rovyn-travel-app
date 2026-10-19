from pydantic import BaseModel, EmailStr, Field


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
    solo: bool = False     # one traveler — no invite link, no joining


class JoinTripRequest(BaseModel):
    invite_code: str


class QuestionnaireData(BaseModel):
    budget_min: int
    budget_max: int
    travel_dates_start: str
    travel_dates_end: str
    departure_city: str = ""
    return_city: str = ""  # where the traveler flies home to; "" = same as departure
    trip_style: str = "no-preference"  # "single-city" | "multi-city" | "no-preference"
    multi_city_picks: list[str] = Field(default_factory=list)  # cities this traveler wants on a multi-city route
    vibes: list[str]
    accommodation_type: str
    pace: str = "moderately"  # "very" | "moderately" | "slightly" (busy)
    special_requirements: str = ""


class VoteRequest(BaseModel):
    destination_name: str


class AddRouteStopRequest(BaseModel):
    destination_name: str  # which recommendation's route gains the stop
    city: str


class StarRequest(BaseModel):
    category: str
    option_id: str
    starred: bool


class CurationRequest(BaseModel):
    category: str
    status: str  # "done" | "no_preference" | "reset"


class RouteNightsRequest(BaseModel):
    nights: dict[str, int]  # town → nights, covering every city on the route


class RouteLockRequest(BaseModel):
    locked: bool


class EditRequest(BaseModel):
    request: str


class ReorderRequest(BaseModel):
    order: list[int]  # new sequence of current day numbers, e.g. [2, 1, 3]


class ArrangeSlotsRequest(BaseModel):
    day: int
    order: list[str]  # slot names by their NEW position, e.g. ["afternoon", "morning", "evening"]


class SlotEditRequest(BaseModel):
    day: int
    slot: str            # "morning" | "afternoon" | "evening"
    action: str          # "delete" | "replace" | "move_up" | "move_down"
    replacement_id: str = ""  # option id from the planning board pool, for "replace"


class PayRequest(BaseModel):
    # "self_book": traveler books every item directly with sellers (Phase A);
    # "full": one payment and Rovyn books everything — arrives with Phase B.
    mode: str = "full"


class BookingConfirmRequest(BaseModel):
    confirmation: str = Field(max_length=200)
    note: str = Field(default="", max_length=500)


class TravelerProfileRequest(BaseModel):
    # What airlines and hotels need to issue a ticket or hold a room — nothing more.
    given_name: str = Field(min_length=1, max_length=80)
    family_name: str = Field(min_length=1, max_length=80)
    born_on: str = ""   # YYYY-MM-DD, required when a flight is booked for this traveler
    gender: str = ""    # "m" | "f" (the airline schema)
    title: str = ""     # "mr" | "ms" | "mrs" | "miss"
    phone: str = ""     # E.164, e.g. +14155550123


class ConfirmCheckoutRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)


class StartCheckoutRequest(BaseModel):
    # Required when the locked check contains a non-refundable item that is
    # booked only after the cards are captured.
    acknowledge_non_refundable: bool = False


class PostMediaItem(BaseModel):
    url: str          # /media/... path returned by the upload endpoint
    media_type: str   # "image" | "video"


class CreatePostRequest(BaseModel):
    caption: str = ""
    media: list[PostMediaItem]
    trip_id: str = ""      # optional — associate a Rovyn trip with the post
    vibes: list[str] = []  # feed-matching tags; defaults from the trip if empty


class CommentRequest(BaseModel):
    text: str


class LikeRequest(BaseModel):
    liked: bool


class SaveRequest(BaseModel):
    saved: bool


class CopyCheckRequest(BaseModel):
    start_date: str  # YYYY-MM-DD — when the copier wants to travel
    end_date: str


class CopyTripRequest(BaseModel):
    name: str = ""
    start_date: str
    end_date: str
    # The adapted plan returned by copy-check; the copier reviewed and accepted it.
    adapted_plan: dict
