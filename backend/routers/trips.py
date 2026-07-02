import json
import random
import string
import uuid
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import get_conn
from backend.models import CreateTripRequest, JoinTripRequest, QuestionnaireData, VoteRequest
from backend.services.ai_service import ai_available, generate_itinerary, get_destination_recommendations

router = APIRouter()


def _generate_invite_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_trip(conn, trip_id: str):
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found.")
    return trip


def _require_member(conn, trip_id: str, user_id: str):
    row = conn.execute(
        "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip_id, user_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=403, detail="You are not a member of this trip.")


def _questionnaire_rows(conn, trip_id: str) -> list[dict]:
    rows = conn.execute("SELECT data FROM questionnaires WHERE trip_id = ?", (trip_id,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def _status_payload(conn, trip, user_id: str) -> dict:
    members = conn.execute(
        """SELECT u.id, u.name,
                  EXISTS(SELECT 1 FROM questionnaires q WHERE q.trip_id = t.trip_id AND q.user_id = u.id) AS submitted,
                  EXISTS(SELECT 1 FROM votes v WHERE v.trip_id = t.trip_id AND v.user_id = u.id) AS voted
           FROM trip_members t JOIN users u ON u.id = t.user_id
           WHERE t.trip_id = ? ORDER BY t.joined_at""",
        (trip["id"],),
    ).fetchall()
    votes = conn.execute(
        "SELECT user_id, destination_name FROM votes WHERE trip_id = ?", (trip["id"],)
    ).fetchall()
    tallies = Counter(v["destination_name"] for v in votes)
    my_vote = next((v["destination_name"] for v in votes if v["user_id"] == user_id), None)

    total = len(members)
    submitted = sum(1 for m in members if m["submitted"])
    me = next((m for m in members if m["id"] == user_id), None)

    return {
        "id": trip["id"],
        "name": trip["name"],
        "invite_code": trip["invite_code"],
        "status": trip["status"],
        "leader_id": trip["leader_id"],
        "is_leader": user_id == trip["leader_id"],
        "members": [
            {
                "user_id": m["id"],
                "name": m["name"],
                "is_leader": m["id"] == trip["leader_id"],
                "submitted": bool(m["submitted"]),
                "voted": bool(m["voted"]),
            }
            for m in members
        ],
        "total_members": total,
        "submitted_count": submitted,
        "all_submitted": total > 0 and submitted == total,
        "me": {"submitted": bool(me and me["submitted"]), "vote": my_vote},
        "has_recommendations": trip["recommendations"] is not None,
        "recommendations_generating": bool(trip["recommendations_generating"]),
        "itinerary_generating": bool(trip["itinerary_generating"]),
        "votes_cast": len(votes),
        "all_voted": total > 0 and len(votes) == total,
        "vote_tallies": dict(tallies),
        "selected_destination": trip["selected_destination"],
        "has_itinerary": trip["itinerary"] is not None,
    }


# ---------------------------------------------------------------------------
# Trip CRUD
# ---------------------------------------------------------------------------

@router.get("")
def my_trips(user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT tr.id, tr.name, tr.invite_code, tr.status, tr.selected_destination, tr.leader_id,
                      (SELECT COUNT(*) FROM trip_members m WHERE m.trip_id = tr.id) AS member_count
               FROM trips tr JOIN trip_members tm ON tm.trip_id = tr.id
               WHERE tm.user_id = ? ORDER BY tr.created_at DESC""",
            (user["id"],),
        ).fetchall()
    return {
        "trips": [
            {
                "id": r["id"],
                "name": r["name"],
                "invite_code": r["invite_code"],
                "status": r["status"],
                "selected_destination": r["selected_destination"],
                "is_leader": r["leader_id"] == user["id"],
                "member_count": r["member_count"],
            }
            for r in rows
        ]
    }


@router.post("")
def create_trip(req: CreateTripRequest, user: dict = Depends(get_current_user)):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Give your trip a name.")
    trip_id = str(uuid.uuid4())
    with get_conn() as conn:
        code = _generate_invite_code()
        while conn.execute("SELECT 1 FROM trips WHERE invite_code = ?", (code,)).fetchone():
            code = _generate_invite_code()
        conn.execute(
            "INSERT INTO trips (id, name, invite_code, leader_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (trip_id, req.name.strip(), code, user["id"], _now()),
        )
        conn.execute(
            "INSERT INTO trip_members (trip_id, user_id, joined_at) VALUES (?, ?, ?)",
            (trip_id, user["id"], _now()),
        )
    return {"id": trip_id, "invite_code": code, "name": req.name.strip()}


@router.post("/join")
def join_trip(req: JoinTripRequest, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = conn.execute(
            "SELECT * FROM trips WHERE invite_code = ?", (req.invite_code.strip().upper(),)
        ).fetchone()
        if trip is None:
            raise HTTPException(status_code=404, detail="Trip not found — check the invite code.")
        already = conn.execute(
            "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip["id"], user["id"])
        ).fetchone()
        if already:
            return {"id": trip["id"], "name": trip["name"], "already_member": True}
        if trip["status"] != "waiting":
            raise HTTPException(
                status_code=400,
                detail="This trip has already moved on to voting — ask the leader to start a new one.",
            )
        conn.execute(
            "INSERT OR IGNORE INTO trip_members (trip_id, user_id, joined_at) VALUES (?, ?, ?)",
            (trip["id"], user["id"], _now()),
        )
    return {"id": trip["id"], "name": trip["name"], "already_member": False}


@router.get("/{trip_id}")
def trip_status(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        return _status_payload(conn, trip, user["id"])


# ---------------------------------------------------------------------------
# Questionnaire
# ---------------------------------------------------------------------------

@router.post("/{trip_id}/questionnaire")
def submit_questionnaire(trip_id: str, data: QuestionnaireData, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["status"] != "waiting":
            raise HTTPException(status_code=400, detail="Questionnaires are closed — the trip is already voting.")
        conn.execute(
            """INSERT INTO questionnaires (trip_id, user_id, data, submitted_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(trip_id, user_id) DO UPDATE SET data = excluded.data, submitted_at = excluded.submitted_at""",
            (trip_id, user["id"], json.dumps(data.model_dump()), _now()),
        )
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) c FROM questionnaires WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
    return {"success": True, "all_submitted": done == total}


# ---------------------------------------------------------------------------
# Recommendations & voting
# ---------------------------------------------------------------------------

@router.get("/{trip_id}/recommendations")
def read_recommendations(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
    return {
        "recommendations": json.loads(trip["recommendations"]) if trip["recommendations"] else None,
        "generating": bool(trip["recommendations_generating"]),
        "ai_available": ai_available(),
    }


@router.post("/{trip_id}/recommendations")
def create_recommendations(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["recommendations"]:
            return {"recommendations": json.loads(trip["recommendations"])}
        if user["id"] != trip["leader_id"]:
            raise HTTPException(status_code=403, detail="Only the trip leader can generate recommendations.")
        if trip["recommendations_generating"]:
            raise HTTPException(status_code=409, detail="Recommendations are already being generated.")
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        responses = _questionnaire_rows(conn, trip_id)
        if total == 0 or len(responses) < total:
            raise HTTPException(status_code=400, detail="Not everyone has submitted their questionnaire yet.")
        conn.execute("UPDATE trips SET recommendations_generating = 1 WHERE id = ?", (trip_id,))

    try:
        recommendations = get_destination_recommendations(responses)
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("UPDATE trips SET recommendations_generating = 0 WHERE id = ?", (trip_id,))
        raise HTTPException(status_code=502, detail=f"Recommendation generation failed: {exc}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE trips SET recommendations = ?, recommendations_generating = 0, status = 'voting' WHERE id = ?",
            (json.dumps(recommendations), trip_id),
        )
    return {"recommendations": recommendations}


@router.post("/{trip_id}/vote")
def cast_vote(trip_id: str, req: VoteRequest, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["recommendations"]:
            raise HTTPException(status_code=400, detail="Recommendations haven't been generated yet.")
        if trip["selected_destination"]:
            raise HTTPException(status_code=400, detail="Voting has closed — a destination was already selected.")

        recommendations = json.loads(trip["recommendations"])
        valid_names = {rec["name"] for rec in recommendations}
        if req.destination_name not in valid_names:
            raise HTTPException(status_code=400, detail="That destination is not one of the recommendations.")

        conn.execute(
            """INSERT INTO votes (trip_id, user_id, destination_name) VALUES (?, ?, ?)
               ON CONFLICT(trip_id, user_id) DO UPDATE SET destination_name = excluded.destination_name""",
            (trip_id, user["id"], req.destination_name),
        )

        votes = conn.execute("SELECT destination_name FROM votes WHERE trip_id = ?", (trip_id,)).fetchall()
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]

        winner = None
        if len(votes) == total:
            tallies = Counter(v["destination_name"] for v in votes)
            top = max(tallies.values())
            tied = [name for name, count in tallies.items() if count == top]
            # Tie-break: highest AI match score
            winner = max(
                tied,
                key=lambda name: next(
                    (rec.get("match_score", 0) for rec in recommendations if rec["name"] == name), 0
                ),
            )
            conn.execute(
                "UPDATE trips SET selected_destination = ?, status = 'selected' WHERE id = ?",
                (winner, trip_id),
            )

    return {"success": True, "winner": winner}


# ---------------------------------------------------------------------------
# Itinerary & booking hub
# ---------------------------------------------------------------------------

@router.get("/{trip_id}/itinerary")
def read_itinerary(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
    return {
        "itinerary": json.loads(trip["itinerary"]) if trip["itinerary"] else None,
        "generating": bool(trip["itinerary_generating"]),
        "destination": trip["selected_destination"],
    }


@router.post("/{trip_id}/itinerary")
def create_itinerary(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["itinerary"]:
            return {"itinerary": json.loads(trip["itinerary"])}
        if user["id"] != trip["leader_id"]:
            raise HTTPException(status_code=403, detail="Only the trip leader can build the itinerary.")
        if not trip["selected_destination"]:
            raise HTTPException(status_code=400, detail="No destination selected yet — voting must finish first.")
        if trip["itinerary_generating"]:
            raise HTTPException(status_code=409, detail="The itinerary is already being generated.")
        responses = _questionnaire_rows(conn, trip_id)
        destination = trip["selected_destination"]
        conn.execute("UPDATE trips SET itinerary_generating = 1 WHERE id = ?", (trip_id,))

    try:
        itinerary = generate_itinerary(destination, responses)
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("UPDATE trips SET itinerary_generating = 0 WHERE id = ?", (trip_id,))
        raise HTTPException(status_code=502, detail=f"Itinerary generation failed: {exc}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE trips SET itinerary = ?, itinerary_generating = 0 WHERE id = ?",
            (json.dumps(itinerary), trip_id),
        )
    return {"itinerary": itinerary}


@router.get("/{trip_id}/hub")
def booking_hub(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
    if not trip["itinerary"]:
        raise HTTPException(status_code=400, detail="No itinerary yet — generate one first.")

    itinerary = json.loads(trip["itinerary"])
    links = itinerary.get("booking_links", {})

    bookable_activities = []
    for day in itinerary.get("days", []):
        for period in ("morning", "afternoon", "evening"):
            act = day.get(period) or {}
            if act.get("bookable"):
                bookable_activities.append({
                    "day": day.get("day"),
                    "date": day.get("date"),
                    "activity": act.get("activity"),
                    "cost_per_person": act.get("cost_per_person", 0),
                })

    return {
        "destination": itinerary.get("destination"),
        "dates": itinerary.get("dates", {}),
        "group_size": total,
        "flights": links.get("flights", {}),
        "stays": links.get("stays", {}),
        "activities": links.get("activities", {}),
        "bookable_activities": bookable_activities,
        "hotel": itinerary.get("hotel", {}),
        "total_cost_per_person": itinerary.get("total_cost_per_person", 0),
        "note": "Links open pre-filled searches with your dates and group size. Direct in-app checkout requires partner API access (Expedia Rapid, Booking.com Connect) — planned next step.",
    }
