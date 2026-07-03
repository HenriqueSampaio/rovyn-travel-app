"""Phase A booking checklist: the approved plan becomes actionable.

Rovyn doesn't move travel money in Phase A — instead, every item of the final
plan (flights per departure city, the stay, each visit and event) becomes a
checklist entry with a deep link to its seller. Travelers book on the seller's
site, then record the confirmation (PNR, hotel code, voucher number) here so
the whole group sees real booking progress. This is also the permanent
"I'll book it myself" mode for things that can never be auto-booked (Airbnb,
event tickets).
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import get_conn
from backend.models import BookingConfirmRequest
from backend.services import email_service, links

router = APIRouter()

_BOOKABLE_STATUSES = ("approved", "booked")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_member_trip(conn, trip_id: str, user_id: str):
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found.")
    member = conn.execute(
        "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip_id, user_id)
    ).fetchone()
    if member is None:
        raise HTTPException(status_code=403, detail="You are not a member of this trip.")
    return trip


def _checklist_from_plan(trip) -> list[dict]:
    """Deterministic checklist items derived from the final plan."""
    plan = json.loads(trip["final_plan"])
    destination = plan.get("destination", "")
    dates = plan.get("dates", {})
    start, end = dates.get("start", ""), dates.get("end", "")
    group = len(plan.get("flight_cost_by_city", {})) or 1  # placeholder, refined below

    items = []
    for city, f in (plan.get("flights") or {}).items():
        items.append({
            "item_key": f"flight:{city}",
            "category": "flight",
            "scope": f"city:{city}",
            "title": f"Round-trip flight from {city} — {f.get('airline', '')}".strip(" —"),
            "link": f.get("link") or links.expedia_flights(city, destination, start, end, 2),
            "details": f,
        })

    stay = plan.get("stay") or {}
    if stay.get("name"):
        items.append({
            "item_key": "stay",
            "category": "stay",
            "scope": "group",
            "title": f"Stay — {stay['name']}",
            "link": stay.get("official_website")
                    or (stay.get("links") or {}).get("booking")
                    or links.booking_stays(destination, start, end, max(group, 2)),
            "details": stay,
        })

    for v in plan.get("visits") or []:
        items.append({
            "item_key": f"visit:{v.get('id') or v.get('name')}",
            "category": "visit",
            "scope": "group",
            "title": v.get("name", "Visit"),
            "link": (v.get("links") or {}).get("getyourguide")
                    or links.viator_tours(destination),
            "details": v,
        })

    for e in plan.get("events") or []:
        items.append({
            "item_key": f"event:{e.get('id') or e.get('name')}",
            "category": "event",
            "scope": "group",
            "title": e.get("name", "Event"),
            "link": (e.get("links") or {}).get("getyourguide")
                    or links.viator_tours(destination),
            "details": e,
        })
    return items


def _materialize(conn, trip) -> None:
    """Idempotently create checklist rows; plan revisions only add new items."""
    for item in _checklist_from_plan(trip):
        conn.execute(
            """INSERT OR IGNORE INTO bookings
               (trip_id, item_key, category, scope, title, provider, link, details, created_at)
               VALUES (?, ?, ?, ?, ?, 'self', ?, ?, ?)""",
            (trip["id"], item["item_key"], item["category"], item["scope"],
             item["title"], item["link"], json.dumps(item["details"]), _now()),
        )


@router.get("/{trip_id}/bookings")
def list_bookings(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_member_trip(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="No final plan yet.")
        if trip["status"] not in _BOOKABLE_STATUSES:
            raise HTTPException(status_code=400, detail="The booking checklist opens once everyone approves the plan.")
        _materialize(conn, trip)
        rows = conn.execute(
            """SELECT b.*, u.name AS booked_by_name
               FROM bookings b LEFT JOIN users u ON u.id = b.booked_by
               WHERE b.trip_id = ? ORDER BY b.created_at, b.item_key""",
            (trip_id,),
        ).fetchall()
        my_q = conn.execute(
            "SELECT data FROM questionnaires WHERE trip_id = ? AND user_id = ?",
            (trip_id, user["id"]),
        ).fetchone()

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
        "confirmation": r["confirmation"],
        "note": r["note"],
        "booked_by": r["booked_by_name"],
        "booked_at": r["booked_at"],
        "details": json.loads(r["details"]) if r["details"] else {},
        "mine": r["scope"] == "group" or r["scope"].casefold() == f"city:{my_city}".casefold(),
    } for r in rows]

    booked = sum(1 for i in items if i["status"] == "booked")
    return {
        "items": items,
        "total": len(items),
        "booked": booked,
        "my_departure_city": my_city,
    }


@router.post("/{trip_id}/bookings/{item_key:path}/confirm")
def confirm_booking(trip_id: str, item_key: str, req: BookingConfirmRequest,
                    user: dict = Depends(get_current_user)):
    confirmation = req.confirmation.strip()
    if not confirmation:
        raise HTTPException(status_code=400, detail="Paste the confirmation code or reference you received.")
    with get_conn() as conn:
        trip = _load_member_trip(conn, trip_id, user["id"])
        row = conn.execute(
            "SELECT * FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That checklist item doesn't exist.")
        conn.execute(
            """UPDATE bookings SET status = 'booked', confirmation = ?, note = ?,
               booked_by = ?, booked_at = ? WHERE trip_id = ? AND item_key = ?""",
            (confirmation, req.note.strip(), user["id"], _now(), trip_id, item_key),
        )

    email_result = email_service.send_email(
        user["email"],
        f"Booking recorded — {row['title']}",
        f"""
        <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#23312B">
          <h1 style="font-weight:400">Rovyn</h1>
          <p>Hi {user['name']}, your booking is recorded on <strong>{trip['name']}</strong>:</p>
          <p style="font-size:18px"><strong>{row['title']}</strong><br>
             Confirmation: <strong>{confirmation}</strong></p>
          <p style="color:#7C877F;font-size:13px">The whole group can now see this item as booked.</p>
        </div>
        """,
    )
    return {"success": True, "email": email_result}


@router.post("/{trip_id}/bookings/{item_key:path}/unbook")
def unbook(trip_id: str, item_key: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        _load_member_trip(conn, trip_id, user["id"])
        row = conn.execute(
            "SELECT 1 FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That checklist item doesn't exist.")
        conn.execute(
            """UPDATE bookings SET status = 'to_book', confirmation = NULL, note = NULL,
               booked_by = NULL, booked_at = NULL WHERE trip_id = ? AND item_key = ?""",
            (trip_id, item_key),
        )
    return {"success": True}
