"""In-app payment & auto-booking endpoints — the group checkout.

/trips/{id}/checkout*   the traveler-facing flow (state, profile, session,
                        confirm, release, abandon, retry, cancel a booking)
/webhooks/stripe        Stripe events (signature-verified; 500 on handler
                        failure so Stripe retries; unroutable → dead letter)
/webhooks/liteapi       LiteAPI booking events (shared-token-verified)

Authorization mirrors the rest of the trip API: members read and act on their
own share; the leader abandons/retries; the leader or the payer cancels a
booking. The engine lives in backend/services/booking_service.py.
"""
import hmac
import json
import os
import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.auth import get_current_user
from backend.db import get_conn
from backend.models import ConfirmCheckoutRequest, StartCheckoutRequest, TravelerProfileRequest
from backend.routers.trips import _member_trip
from backend.services import booking_service, duffel_booking, payment_service

router = APIRouter()
webhooks_router = APIRouter()

_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _raise(exc: booking_service.BookingError):
    raise HTTPException(status_code=exc.status or 400, detail=str(exc))


@router.get("/{trip_id}/checkout")
def read_checkout(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        trip = _member_trip(conn, trip_id, user["id"])
    try:
        return booking_service.checkout_state(trip, user)
    except booking_service.BookingError as exc:
        _raise(exc)


@router.post("/{trip_id}/checkout/profile")
def save_profile(trip_id: str, req: TravelerProfileRequest, user: dict = Depends(get_current_user)):
    if not req.given_name.strip() or not req.family_name.strip():
        raise HTTPException(status_code=400, detail="First and last name are required.")
    with get_conn() as conn:
        _member_trip(conn, trip_id, user["id"])
        born = req.born_on.strip()
        if born:
            try:
                if date.fromisoformat(born) >= date.today():
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=400, detail="Date of birth must be a past date (YYYY-MM-DD).")
        phone = req.phone.strip().replace(" ", "").replace("-", "")
        if phone and not _PHONE_RE.match(phone):
            raise HTTPException(status_code=400, detail="Phone must be in international format, e.g. +14155550123.")
        gender = req.gender.strip().lower()
        if gender and gender not in ("m", "f"):
            raise HTTPException(status_code=400, detail="Gender must be m or f (the airline schema).")
        title = req.title.strip().lower()
        if title and title not in ("mr", "ms", "mrs", "miss"):
            raise HTTPException(status_code=400, detail="Title must be mr, ms, mrs or miss.")
    # Once this traveler's share is in, the fields the booking needs cannot be blanked.
    try:
        booking_service.assert_profile_change_allowed(trip_id, user, {
            "given_name": req.given_name.strip(), "family_name": req.family_name.strip(), "born_on": born,
            "gender": gender, "title": title, "phone": phone})
    except booking_service.BookingError as exc:
        _raise(exc)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO traveler_profiles (user_id, given_name, family_name, born_on, gender, title, phone, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET given_name = excluded.given_name, family_name = excluded.family_name,
                 born_on = excluded.born_on, gender = excluded.gender, title = excluded.title, phone = excluded.phone,
                 updated_at = excluded.updated_at""",
            (user["id"], req.given_name.strip(), req.family_name.strip(), born or None, gender or None,
             title or None, phone or None, booking_service._now()),
        )
    return {"success": True}


@router.post("/{trip_id}/checkout/lock")
def lock_checkout(trip_id: str, user: dict = Depends(get_current_user)):
    """Verify and lock live prices so the traveler sees their share first."""
    with get_conn(readonly=True) as conn:
        trip = _member_trip(conn, trip_id, user["id"])
        ok, reason = booking_service.availability(conn, trip)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    try:
        return booking_service.lock_prices(trip_id, user)
    except booking_service.BookingError as exc:
        _raise(exc)
    except duffel_booking.DuffelError as exc:
        raise HTTPException(status_code=502, detail=f"The airline did not answer: {exc}")


@router.post("/{trip_id}/checkout/session")
def start_checkout(trip_id: str, req: StartCheckoutRequest | None = None, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        trip = _member_trip(conn, trip_id, user["id"])
        ok, reason = booking_service.availability(conn, trip)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    try:
        return booking_service.start_session(trip_id, user, acknowledge=bool(req and req.acknowledge_non_refundable))
    except booking_service.BookingError as exc:
        _raise(exc)
    except payment_service.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe did not accept the checkout: {exc}")
    except duffel_booking.DuffelError as exc:
        raise HTTPException(status_code=502, detail=f"The airline did not answer: {exc}")


@router.post("/{trip_id}/checkout/confirm")
def confirm_checkout(trip_id: str, req: ConfirmCheckoutRequest, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        _member_trip(conn, trip_id, user["id"])
    try:
        return booking_service.confirm_session(trip_id, user, req.session_id.strip())
    except booking_service.BookingError as exc:
        _raise(exc)
    except payment_service.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Could not confirm with Stripe: {exc}")


@router.post("/{trip_id}/checkout/release")
def release_checkout(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        _member_trip(conn, trip_id, user["id"])
    try:
        return booking_service.release_authorization(trip_id, user)
    except booking_service.BookingError as exc:
        _raise(exc)


def _leader_only(conn, trip_id: str, user: dict):
    trip = _member_trip(conn, trip_id, user["id"])
    if trip["leader_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the trip leader can do that.")
    return trip


@router.post("/{trip_id}/checkout/abandon")
def abandon_checkout(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        _leader_only(conn, trip_id, user)
    try:
        return booking_service.abandon(trip_id, user)
    except booking_service.BookingError as exc:
        _raise(exc)


@router.post("/{trip_id}/checkout/retry")
def retry_checkout(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        trip = _leader_only(conn, trip_id, user)
        # Same gate as lock and session: once a traveler has chosen to book it
        # themselves, the group books item by item and no new round may open.
        ok, reason = booking_service.availability(conn, trip)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    try:
        return booking_service.retry(trip_id, user)
    except booking_service.BookingError as exc:
        _raise(exc)


@router.post("/{trip_id}/bookings/{item_key:path}/cancel")
def cancel_rovyn_booking(trip_id: str, item_key: str, user: dict = Depends(get_current_user)):
    with get_conn(readonly=True) as conn:
        _member_trip(conn, trip_id, user["id"])
    try:
        return booking_service.cancel_booking(trip_id, item_key, user)
    except booking_service.BookingError as exc:
        _raise(exc)
    except (payment_service.StripeError, duffel_booking.DuffelError) as exc:
        raise HTTPException(status_code=502, detail=f"The supplier did not accept the cancellation: {exc}")


@webhooks_router.post("/stripe")
async def stripe_webhook(request: Request):
    secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise HTTPException(status_code=400, detail="STRIPE_WEBHOOK_SECRET is not configured.")
    body = await request.body()
    if not payment_service.verify_webhook_signature(body, request.headers.get("stripe-signature", ""), secret):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
    try:
        event = json.loads(body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed event.")
    try:
        return booking_service.handle_stripe_event(event)
    except Exception as exc:  # 500 → Stripe retries; the event row stays unprocessed
        raise HTTPException(status_code=500, detail=f"webhook handler failed: {exc}")


@webhooks_router.post("/liteapi")
async def liteapi_webhook(request: Request):
    token = (os.environ.get("LITEAPI_WEBHOOK_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="LITEAPI_WEBHOOK_TOKEN is not configured.")
    sent = (request.headers.get("authorization") or "").strip()
    if sent.lower().startswith("bearer "):
        sent = sent[7:].strip()
    if not hmac.compare_digest(sent.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="Bad webhook token.")
    try:
        payload = json.loads((await request.body()).decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Malformed event.")
    try:
        return booking_service.handle_liteapi_event(payload)
    except booking_service.BookingError as exc:
        raise HTTPException(status_code=exc.status or 400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"webhook handler failed: {exc}")
