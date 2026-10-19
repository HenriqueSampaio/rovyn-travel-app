"""Phase A booking checklist: the approved plan becomes actionable.

Rovyn doesn't move travel money in Phase A — instead, every item of the final
plan (flights per departure city, the stay, each visit and event) becomes a
checklist entry with a deep link to its seller. Travelers book on the seller's
site, then record the confirmation (PNR, hotel code, voucher number) here so
the whole group sees real booking progress. This is also the permanent
"I'll book it myself" mode for things that can never be auto-booked (Airbnb,
event tickets).

The checklist is RECONCILED with the plan on every read: plan revisions update
titles/links, remove unbooked items that left the plan, and flag already-booked
items that left the plan as stale ("cancel or keep?") instead of hiding them.

It shares its items with the group checkout (services/booking_service.py),
so the two must never book the same thing twice: an item the open round is
charging cannot be recorded as booked by hand, and an item a traveler already
booked here is never charged by Rovyn.
"""
import html
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import fetch_many, get_conn
from backend.models import BookingConfirmRequest
from backend.routers.trips import _member_trip
from backend.services import booking_service, email_service
from backend.services.checklist import REAL_STATUSES, sync_checklist

router = APIRouter()

_BOOKABLE_STATUSES = ("approved", "booked")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_bookable(trip):
    if not trip["final_plan"]:
        raise HTTPException(status_code=400, detail="No final plan yet.")
    if trip["status"] not in _BOOKABLE_STATUSES:
        raise HTTPException(status_code=400, detail="The booking checklist opens once everyone approves the plan.")


@router.get("/{trip_id}/bookings")
def list_bookings(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _member_trip(conn, trip_id, user["id"])
        _require_bookable(trip)
        sync_checklist(conn, trip)
        rows, my_q_rows = fetch_many(conn, [
            ("""SELECT b.*, u.name AS booked_by_name
               FROM bookings b LEFT JOIN users u ON u.id = b.booked_by
               WHERE b.trip_id = ? ORDER BY b.created_at, b.item_key""", (trip_id,)),
            ("SELECT data FROM questionnaires WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])),
        ])
        my_q = my_q_rows[0] if my_q_rows else None
        # What the open group checkout is buying — the page offers no "Mark as booked" for these.
        charged_keys = booking_service.charged_item_keys(conn, trip_id)

    my_city = ""
    if my_q:
        my_city = json.loads(my_q["data"]).get("departure_city", "").strip()

    items = [{
        "item_key": r["item_key"],
        "category": r["category"],
        "scope": r["scope"],
        "title": r["title"],
        "link": r["link"],
        "status": r["status"],
        "stale": bool(r["stale"]),
        "confirmation": r["confirmation"],
        "note": r["note"],
        "booked_by": r["booked_by_name"],
        "booked_at": r["booked_at"],
        "provider": r["provider"],
        "provider_booking_id": r["provider_booking_id"],
        "documents": json.loads(r["documents"]) if r["documents"] else {},
        "amount_cents": r["amount_cents"],
        "payer_id": r["payer_id"],
        "cancelled_at": r["cancelled_at"],
        "details": json.loads(r["details"]) if r["details"] else {},
        "mine": r["scope"] == "group" or r["scope"].casefold() == f"city:{my_city}".casefold(),
    } for r in rows]

    active = [i for i in items if not i["stale"]]
    return {
        "items": items,
        "total": len(active),
        "booked": sum(1 for i in active if i["status"] in REAL_STATUSES),
        "my_departure_city": my_city,
        "charged_keys": charged_keys,
    }


@router.post("/{trip_id}/bookings/{item_key:path}/confirm")
def confirm_booking(trip_id: str, item_key: str, req: BookingConfirmRequest,
                    user: dict = Depends(get_current_user)):
    confirmation = req.confirmation.strip()
    if not confirmation:
        raise HTTPException(status_code=400, detail="Paste the confirmation code or reference you received.")
    with get_conn() as conn:
        trip = _member_trip(conn, trip_id, user["id"])
        _require_bookable(trip)
        row = conn.execute(
            "SELECT * FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That checklist item doesn't exist.")
        if row["provider"] != "self":
            raise HTTPException(status_code=409, detail="Rovyn booked this item — cancel it from the checkout instead of editing it by hand.")
        if item_key in booking_service.charged_item_keys(conn, trip_id):
            # Recording it would let Rovyn buy the same item a second time when the round settles.
            raise HTTPException(status_code=409, detail="Rovyn is booking this in the group checkout — recording your own "
                                                        "booking too would book it twice. To book it yourself instead, the "
                                                        "leader must abandon the group checkout first.")
        conn.execute(
            """UPDATE bookings SET status = 'booked', confirmation = ?, note = ?,
               booked_by = ?, booked_at = ? WHERE trip_id = ? AND item_key = ?""",
            (confirmation, req.note.strip(), user["id"], _now(), trip_id, item_key),
        )

    esc = html.escape
    email_result = email_service.send_email(
        user["email"],
        f"Booking recorded — {row['title']}",
        f"""
        <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#23312B">
          <h1 style="font-weight:400">Rovyn</h1>
          <p>Hi {esc(user['name'])}, your booking is recorded on <strong>{esc(trip['name'])}</strong>:</p>
          <p style="font-size:18px"><strong>{esc(row['title'])}</strong><br>
             Confirmation: <strong>{esc(confirmation)}</strong></p>
          <p style="color:#7C877F;font-size:13px">The whole group can now see this item as booked.</p>
        </div>
        """,
    )
    return {"success": True, "email": email_result}


@router.post("/{trip_id}/bookings/{item_key:path}/unbook")
def unbook(trip_id: str, item_key: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _member_trip(conn, trip_id, user["id"])
        _require_bookable(trip)
        row = conn.execute(
            "SELECT stale, provider FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That checklist item doesn't exist.")
        if row["provider"] != "self":
            raise HTTPException(status_code=409, detail="Rovyn booked this item — cancel it from the checkout (POST /bookings/{key}/cancel) instead.")
        if row["stale"]:
            # A stale row only exists because it was booked; unbooking resolves it
            conn.execute("DELETE FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key))
        else:
            conn.execute(
                """UPDATE bookings SET status = 'to_book', confirmation = NULL, note = NULL,
                   booked_by = NULL, booked_at = NULL WHERE trip_id = ? AND item_key = ?""",
                (trip_id, item_key),
            )
    return {"success": True}
