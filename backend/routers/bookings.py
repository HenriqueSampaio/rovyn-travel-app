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
"""
import html
import json
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import fetch_many, get_conn
from backend.models import BookingConfirmRequest
from backend.routers.trips import _member_trip
from backend.services import email_service, links

router = APIRouter()

_BOOKABLE_STATUSES = ("approved", "booked")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_bookable(trip):
    if not trip["final_plan"]:
        raise HTTPException(status_code=400, detail="No final plan yet.")
    if trip["status"] not in _BOOKABLE_STATUSES:
        raise HTTPException(status_code=400, detail="The booking checklist opens once everyone approves the plan.")


def _checklist_from_plan(trip, group_size: int, city_counts: Counter) -> list[dict]:
    """Deterministic checklist items derived from the CURRENT final plan."""
    plan = json.loads(trip["final_plan"])
    destination = plan.get("destination", "")
    dates = plan.get("dates", {})
    start, end = dates.get("start", ""), dates.get("end", "")

    items = []
    for city, f in (plan.get("flights") or {}).items():
        adults = city_counts.get(city.strip().casefold(), 1)
        items.append({
            "item_key": f"flight:{city}",
            "category": "flight",
            "scope": f"city:{city}",
            "title": f"Round-trip flight from {city} — {f.get('airline', '')}".strip(" —"),
            "link": links.safe_url(f.get("link"))
                    or links.expedia_flights(city, destination, start, end, adults),
            "details": f,
        })

    for leg in plan.get("transport") or []:
        title_bits = [p for p in (leg.get("mode"), leg.get("carrier")) if p]
        items.append({
            "item_key": f"transport:{leg.get('leg', '?')}",
            "category": "transport",
            "scope": "group",
            "title": f"{leg.get('leg', 'Intercity leg')} — {' · '.join(title_bits)}".strip(" —"),
            "link": links.safe_url(leg.get("link"))
                    or links.rome2rio(leg.get("from", ""), leg.get("to", "")),
            "details": leg,
        })

    # One stay per route city on multi-city trips; the single stay otherwise.
    # Primary link is the BOOKABLE one (dates + party prefilled, affiliate-
    # tagged); the official website is shown separately in the UI.
    for stay in (plan.get("stays") or ([plan["stay"]] if plan.get("stay") else [])):
        if not stay.get("name"):
            continue
        city = (stay.get("city") or "").strip()
        town = city.split(",")[0].strip()
        items.append({
            "item_key": f"stay:{town + ':' if town else ''}{stay['name']}",
            "category": "stay",
            "scope": "group",
            "title": f"Stay{f' in {town}' if town else ''} — {stay['name']}",
            "link": links.safe_url((stay.get("links") or {}).get("booking"))
                    or links.booking_stays(city or destination,
                                           stay.get("check_in") or start,
                                           stay.get("check_out") or end, group_size),
            "details": stay,
        })

    for cat, plural in (("visit", "visits"), ("event", "events")):
        for idx, item in enumerate(plan.get(plural) or []):
            ident = item.get("id") or item.get("name") or f"item-{idx}"
            items.append({
                "item_key": f"{cat}:{ident}",
                "category": cat,
                "scope": "group",
                "title": item.get("name", cat.capitalize()),
                "link": links.safe_url((item.get("links") or {}).get("getyourguide"))
                        or links.viator_tours(destination),
                "details": item,
            })
    return items


def _sync_checklist(conn, trip) -> None:
    """Reconcile bookings rows with the current plan.

    - new plan items appear;
    - existing items get refreshed title/link/details (a swapped hotel keeps a
      new key, so its old row is handled below);
    - unbooked rows that left the plan are deleted;
    - BOOKED rows that left the plan are kept but flagged stale — the traveler
      already holds a real reservation and must decide to cancel or keep it.
    """
    count_rows, q_rows, existing_rows = fetch_many(conn, [
        ("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip["id"],)),
        ("SELECT data FROM questionnaires WHERE trip_id = ?", (trip["id"],)),
        ("SELECT item_key, status FROM bookings WHERE trip_id = ?", (trip["id"],)),
    ])
    group_size = count_rows[0]["c"] or 1
    city_counts = Counter(
        json.loads(r["data"]).get("departure_city", "").strip().casefold()
        for r in q_rows
        if json.loads(r["data"]).get("departure_city", "").strip()
    )
    current = {i["item_key"]: i for i in _checklist_from_plan(trip, group_size, city_counts)}
    existing = {r["item_key"]: r for r in existing_rows}

    # Batched writes: executemany pipelines the rows in one round trip.
    updates, inserts = [], []
    for key, item in current.items():
        if key in existing:
            updates.append((item["title"], item["link"], json.dumps(item["details"]),
                            item["scope"], trip["id"], key))
        else:
            inserts.append((trip["id"], key, item["category"], item["scope"], item["title"],
                            item["link"], json.dumps(item["details"]), _now()))
    gone = [key for key in existing if key not in current]
    stale = [(trip["id"], k) for k in gone if existing[k]["status"] == "booked"]
    drop = [(trip["id"], k) for k in gone if existing[k]["status"] != "booked"]

    if updates:
        conn.executemany(
            """UPDATE bookings SET title = ?, link = ?, details = ?, scope = ?, stale = 0
               WHERE trip_id = ? AND item_key = ?""",
            updates,
        )
    if inserts:
        conn.executemany(
            """INSERT INTO bookings
               (trip_id, item_key, category, scope, title, provider, link, details, created_at)
               VALUES (?, ?, ?, ?, ?, 'self', ?, ?, ?)""",
            inserts,
        )
    if stale:
        conn.executemany("UPDATE bookings SET stale = 1 WHERE trip_id = ? AND item_key = ?", stale)
    if drop:
        conn.executemany("DELETE FROM bookings WHERE trip_id = ? AND item_key = ?", drop)


@router.get("/{trip_id}/bookings")
def list_bookings(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _member_trip(conn, trip_id, user["id"])
        _require_bookable(trip)
        _sync_checklist(conn, trip)
        rows, my_q_rows = fetch_many(conn, [
            ("""SELECT b.*, u.name AS booked_by_name
               FROM bookings b LEFT JOIN users u ON u.id = b.booked_by
               WHERE b.trip_id = ? ORDER BY b.created_at, b.item_key""", (trip_id,)),
            ("SELECT data FROM questionnaires WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])),
        ])
        my_q = my_q_rows[0] if my_q_rows else None

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
        "details": json.loads(r["details"]) if r["details"] else {},
        "mine": r["scope"] == "group" or r["scope"].casefold() == f"city:{my_city}".casefold(),
    } for r in rows]

    active = [i for i in items if not i["stale"]]
    return {
        "items": items,
        "total": len(active),
        "booked": sum(1 for i in active if i["status"] == "booked"),
        "my_departure_city": my_city,
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
            "SELECT stale FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="That checklist item doesn't exist.")
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
