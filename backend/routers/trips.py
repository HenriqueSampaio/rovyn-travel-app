import json
import random
import re
import string
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import get_current_user
from backend.db import get_conn
from backend.models import (
    ArrangeSlotsRequest, CreateTripRequest, CurationRequest, EditRequest, JoinTripRequest,
    PayRequest, QuestionnaireData, ReorderRequest, SlotEditRequest, StarRequest, VoteRequest,
)
from backend.services.ai_service import (
    CATEGORIES, RevisionNotPossible, ai_available, generate_final_plan, generate_itinerary,
    generate_trip_options, get_destination_recommendations, recommended_picks,
    recompute_plan_costs, revise_final_plan,
)
from backend.services.hotel_service import stay_media_for_options

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


def _decided_destination(trip) -> str | None:
    return trip["selected_destination"] or trip["destination"]


def _status_payload(conn, trip, user_id: str) -> dict:
    members = conn.execute(
        """SELECT u.id, u.name,
                  EXISTS(SELECT 1 FROM questionnaires q WHERE q.trip_id = t.trip_id AND q.user_id = u.id) AS submitted,
                  EXISTS(SELECT 1 FROM votes v WHERE v.trip_id = t.trip_id AND v.user_id = u.id) AS voted,
                  EXISTS(SELECT 1 FROM approvals a WHERE a.trip_id = t.trip_id AND a.user_id = u.id) AS approved,
                  EXISTS(SELECT 1 FROM payments p WHERE p.trip_id = t.trip_id AND p.user_id = u.id) AS paid
           FROM trip_members t JOIN users u ON u.id = t.user_id
           WHERE t.trip_id = ? ORDER BY t.joined_at""",
        (trip["id"],),
    ).fetchall()
    votes = conn.execute(
        "SELECT user_id, destination_name FROM votes WHERE trip_id = ?", (trip["id"],)
    ).fetchall()
    progress_rows = conn.execute(
        "SELECT user_id, category, status FROM category_progress WHERE trip_id = ?", (trip["id"],)
    ).fetchall()
    pay_modes = {
        r["user_id"]: r["mode"] for r in conn.execute(
            "SELECT user_id, mode FROM payments WHERE trip_id = ?", (trip["id"],)
        )
    }
    tallies = Counter(v["destination_name"] for v in votes)
    my_vote = next((v["destination_name"] for v in votes if v["user_id"] == user_id), None)

    progress: dict[str, dict] = {}
    for r in progress_rows:
        progress.setdefault(r["user_id"], {})[r["category"]] = r["status"]

    total = len(members)
    submitted = sum(1 for m in members if m["submitted"])
    approved_count = sum(1 for m in members if m["approved"])
    paid_count = sum(1 for m in members if m["paid"])
    me = next((m for m in members if m["id"] == user_id), None)
    my_progress = progress.get(user_id, {})

    def curated_count(uid: str) -> int:
        return sum(1 for c in CATEGORIES if progress.get(uid, {}).get(c))

    all_curated = total > 0 and all(curated_count(m["id"]) == len(CATEGORIES) for m in members)

    return {
        "id": trip["id"],
        "name": trip["name"],
        "invite_code": trip["invite_code"],
        "status": trip["status"],
        "leader_id": trip["leader_id"],
        "is_leader": user_id == trip["leader_id"],
        "destination_fixed": bool(trip["destination"]),
        "decided_destination": _decided_destination(trip),
        "members": [
            {
                "user_id": m["id"],
                "name": m["name"],
                "is_leader": m["id"] == trip["leader_id"],
                "submitted": bool(m["submitted"]),
                "voted": bool(m["voted"]),
                "curated_count": curated_count(m["id"]),
                "approved": bool(m["approved"]),
                "paid": bool(m["paid"]),
                "booking_mode": pay_modes.get(m["id"]),
            }
            for m in members
        ],
        "total_members": total,
        "submitted_count": submitted,
        "all_submitted": total > 0 and submitted == total,
        "me": {
            "submitted": bool(me and me["submitted"]),
            "vote": my_vote,
            "categories": my_progress,
            "curated_count": curated_count(user_id),
            "approved": bool(me and me["approved"]),
            "paid": bool(me and me["paid"]),
            "booking_mode": pay_modes.get(user_id),
        },
        "has_recommendations": trip["recommendations"] is not None,
        "recommendations_generating": bool(trip["recommendations_generating"]),
        "itinerary_generating": bool(trip["itinerary_generating"]),
        "votes_cast": len(votes),
        "all_voted": total > 0 and len(votes) == total,
        "vote_tallies": dict(tallies),
        "selected_destination": trip["selected_destination"],
        "has_itinerary": trip["itinerary"] is not None,
        "has_options": trip["options"] is not None,
        "options_generating": bool(trip["options_generating"]),
        "all_curated": all_curated,
        "categories_total": len(CATEGORIES),
        "has_final_plan": trip["final_plan"] is not None,
        "final_plan_generating": bool(trip["final_plan_generating"]),
        "approved_count": approved_count,
        "all_approved": total > 0 and approved_count == total,
        "paid_count": paid_count,
        "all_paid": total > 0 and paid_count == total,
    }


# ---------------------------------------------------------------------------
# Trip CRUD
# ---------------------------------------------------------------------------

@router.get("")
def my_trips(user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT tr.id, tr.name, tr.invite_code, tr.status, tr.selected_destination, tr.destination, tr.leader_id,
                      tr.final_plan, tr.options,
                      (SELECT COUNT(*) FROM trip_members m WHERE m.trip_id = tr.id) AS member_count
               FROM trips tr JOIN trip_members tm ON tm.trip_id = tr.id
               WHERE tm.user_id = ? ORDER BY tr.created_at DESC""",
            (user["id"],),
        ).fetchall()

    def trip_dates(r) -> dict:
        for col in ("final_plan", "options"):
            if r[col]:
                d = json.loads(r[col]).get("dates") or {}
                if d.get("start"):
                    return {"start": d.get("start"), "end": d.get("end")}
        return {}

    return {
        "trips": [
            {
                "id": r["id"],
                "name": r["name"],
                "invite_code": r["invite_code"],
                "status": r["status"],
                "selected_destination": r["selected_destination"] or r["destination"],
                "is_leader": r["leader_id"] == user["id"],
                "member_count": r["member_count"],
                "dates": trip_dates(r),
            }
            for r in rows
        ]
    }


@router.post("")
def create_trip(req: CreateTripRequest, user: dict = Depends(get_current_user)):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Give your trip a name.")
    trip_id = str(uuid.uuid4())
    destination = req.destination.strip()
    with get_conn() as conn:
        code = _generate_invite_code()
        while conn.execute("SELECT 1 FROM trips WHERE invite_code = ?", (code,)).fetchone():
            code = _generate_invite_code()
        conn.execute(
            """INSERT INTO trips (id, name, invite_code, leader_id, destination, selected_destination, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (trip_id, req.name.strip(), code, user["id"],
             destination or None, destination or None, _now()),
        )
        conn.execute(
            "INSERT INTO trip_members (trip_id, user_id, joined_at) VALUES (?, ?, ?)",
            (trip_id, user["id"], _now()),
        )
    return {"id": trip_id, "invite_code": code, "name": req.name.strip(), "destination": destination or None}


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
                detail="This trip has already moved past the join stage — ask the leader to start a new one.",
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


_TRIP_CHILD_TABLES = (
    "payments", "approvals", "category_progress", "stars",
    "votes", "questionnaires", "trip_members",
)


@router.delete("/{trip_id}")
def delete_trip(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if user["id"] != trip["leader_id"]:
            raise HTTPException(status_code=403, detail="Only the trip leader can delete a trip — you can leave it instead.")
        for table in _TRIP_CHILD_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE trip_id = ?", (trip_id,))
        # Feed posts keep working off their frozen snapshot; only the live link goes
        conn.execute("UPDATE posts SET trip_id = NULL WHERE trip_id = ?", (trip_id,))
        conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    return {"success": True}


@router.post("/{trip_id}/leave")
def leave_trip(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if user["id"] == trip["leader_id"]:
            raise HTTPException(status_code=400, detail="The leader can't leave — delete the trip instead.")
        paid = conn.execute(
            "SELECT 1 FROM payments WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])
        ).fetchone()
        if paid:
            raise HTTPException(status_code=400, detail="You've already paid your share — the trip is locked in.")
        for table in _TRIP_CHILD_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"]))
    return {"success": True}


# ---------------------------------------------------------------------------
# Questionnaire
# ---------------------------------------------------------------------------

@router.post("/{trip_id}/questionnaire")
def submit_questionnaire(trip_id: str, data: QuestionnaireData, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["status"] != "waiting":
            raise HTTPException(status_code=400, detail="Questionnaires are closed — the trip has moved on.")
        conn.execute(
            """INSERT INTO questionnaires (trip_id, user_id, data, submitted_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(trip_id, user_id) DO UPDATE SET data = excluded.data, submitted_at = excluded.submitted_at""",
            (trip_id, user["id"], json.dumps(data.model_dump()), _now()),
        )
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        done = conn.execute("SELECT COUNT(*) c FROM questionnaires WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
    return {"success": True, "all_submitted": done == total}


@router.get("/{trip_id}/date-votes")
def read_date_votes(trip_id: str, user: dict = Depends(get_current_user)):
    """Per-day tally of the date windows travel mates already submitted —
    feeds the availability heatmap on the questionnaire's date step."""
    with get_conn() as conn:
        _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        rows = conn.execute(
            """SELECT q.user_id, q.data, u.name FROM questionnaires q
               JOIN users u ON u.id = q.user_id WHERE q.trip_id = ?""",
            (trip_id,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)
        ).fetchone()["c"]

    counts: dict[str, int] = {}
    names: dict[str, list] = {}
    for r in rows:
        data = json.loads(r["data"])
        try:
            start = date.fromisoformat(data.get("travel_dates_start", ""))
            end = date.fromisoformat(data.get("travel_dates_end", ""))
        except ValueError:
            continue
        if end < start or (end - start).days > 120:  # ignore nonsense windows
            continue
        label = "You" if r["user_id"] == user["id"] else r["name"]
        day = start
        while day <= end:
            iso = day.isoformat()
            counts[iso] = counts.get(iso, 0) + 1
            names.setdefault(iso, []).append(label)
            day += timedelta(days=1)
    return {"counts": counts, "names": names, "submitted": len(rows), "total_members": total}


# ---------------------------------------------------------------------------
# Recommendations & voting (AI-destination mode)
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
        if trip["destination"]:
            raise HTTPException(status_code=400, detail="This trip already has a destination — head to the planning board.")
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
        recommendations = get_destination_recommendations(responses, trip_name=trip["name"])
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
# Planning board: options, stars, curation progress
# ---------------------------------------------------------------------------

@router.post("/{trip_id}/options")
def create_options(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["options"]:
            return {"options": json.loads(trip["options"])}
        if user["id"] != trip["leader_id"]:
            raise HTTPException(status_code=403, detail="Only the trip leader can open the planning board.")
        if trip["options_generating"]:
            raise HTTPException(status_code=409, detail="The planning board is already being prepared.")
        destination = _decided_destination(trip)
        if not destination:
            raise HTTPException(status_code=400, detail="No destination decided yet.")
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        responses = _questionnaire_rows(conn, trip_id)
        if total == 0 or len(responses) < total:
            raise HTTPException(status_code=400, detail="Not everyone has submitted their questionnaire yet.")
        conn.execute("UPDATE trips SET options_generating = 1 WHERE id = ?", (trip_id,))

    try:
        options = generate_trip_options(destination, responses, trip_name=trip["name"])
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("UPDATE trips SET options_generating = 0 WHERE id = ?", (trip_id,))
        raise HTTPException(status_code=502, detail=f"Planning board generation failed: {exc}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE trips SET options = ?, options_generating = 0, status = 'curating' WHERE id = ?",
            (json.dumps(options), trip_id),
        )
    return {"options": options}


@router.get("/{trip_id}/options")
def read_options(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        stars = conn.execute(
            """SELECT s.user_id, s.category, s.option_id, u.name
               FROM stars s JOIN users u ON u.id = s.user_id WHERE s.trip_id = ?""",
            (trip_id,),
        ).fetchall()
        progress = conn.execute(
            "SELECT user_id, category, status FROM category_progress WHERE trip_id = ?", (trip_id,)
        ).fetchall()
        my_q_row = conn.execute(
            "SELECT data FROM questionnaires WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])
        ).fetchone()

    star_counts = Counter(s["option_id"] for s in stars)
    star_names: dict[str, list] = {}
    for s in stars:
        star_names.setdefault(s["option_id"], []).append(s["name"])
    progress_map: dict[str, dict] = {}
    for r in progress:
        progress_map.setdefault(r["user_id"], {})[r["category"]] = r["status"]

    my_city, my_pace = "", "moderately"
    if my_q_row:
        my_q = json.loads(my_q_row["data"])
        my_city = my_q.get("departure_city", "").strip()
        my_pace = my_q.get("pace") or "moderately"

    options = json.loads(trip["options"]) if trip["options"] else None
    recommended = None
    if options:
        from backend.services.ai_service import _duration_days
        recommended = recommended_picks(_duration_days(options.get("dates", {})), my_pace)

    return {
        "options": options,
        "generating": bool(trip["options_generating"]),
        "ai_available": ai_available(),
        "categories": CATEGORIES,
        "star_counts": dict(star_counts),
        "star_names": star_names,
        "my_stars": [s["option_id"] for s in stars if s["user_id"] == user["id"]],
        "progress": progress_map,
        "my_departure_city": my_city,
        "my_pace": my_pace,
        "recommended": recommended,
    }


@router.get("/{trip_id}/stay-media")
def read_stay_media(trip_id: str, user: dict = Depends(get_current_user)):
    """Photo galleries + guest reviews for the board's stay options.

    Fetched from LiteAPI (real hotels) or Google Places (AI stays) on the
    first request, then cached on the trip — stay options never change once
    the board is generated, so the cache never goes stale."""
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["stay_media"] is not None:
            return {"media": json.loads(trip["stay_media"])}
        if not trip["options"]:
            return {"media": {}}
        options = json.loads(trip["options"])
        destination = options.get("destination") or _decided_destination(trip) or ""

    # Network fetches happen outside the connection — they can take seconds
    media = stay_media_for_options(options.get("stay") or [], destination)

    with get_conn() as conn:
        conn.execute("UPDATE trips SET stay_media = ? WHERE id = ?",
                     (json.dumps(media), trip_id))
    return {"media": media}


def _valid_option_ids(options: dict, category: str) -> set[str]:
    if category == "flights":
        return {o["id"] for city_opts in (options.get("flights") or {}).values() for o in city_opts}
    return {o["id"] for o in options.get(category, [])}


@router.post("/{trip_id}/star")
def toggle_star(trip_id: str, req: StarRequest, user: dict = Depends(get_current_user)):
    if req.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category.")
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["options"]:
            raise HTTPException(status_code=400, detail="The planning board isn't open yet.")
        if trip["final_plan"]:
            raise HTTPException(status_code=400, detail="The final plan is drafted — starring is closed. Use 'Request a change' instead.")
        options = json.loads(trip["options"])
        if req.option_id not in _valid_option_ids(options, req.category):
            raise HTTPException(status_code=400, detail="That option doesn't exist in this category.")
        if req.starred:
            conn.execute(
                "INSERT OR IGNORE INTO stars (trip_id, user_id, category, option_id) VALUES (?, ?, ?, ?)",
                (trip_id, user["id"], req.category, req.option_id),
            )
            # Starring something overrides a previous "I don't mind"
            conn.execute(
                """DELETE FROM category_progress
                   WHERE trip_id = ? AND user_id = ? AND category = ? AND status = 'no_preference'""",
                (trip_id, user["id"], req.category),
            )
        else:
            conn.execute(
                "DELETE FROM stars WHERE trip_id = ? AND user_id = ? AND category = ? AND option_id = ?",
                (trip_id, user["id"], req.category, req.option_id),
            )
        my_stars = [
            r["option_id"] for r in conn.execute(
                "SELECT option_id FROM stars WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])
            )
        ]
    return {"success": True, "my_stars": my_stars}


@router.post("/{trip_id}/curation")
def set_curation(trip_id: str, req: CurationRequest, user: dict = Depends(get_current_user)):
    if req.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category.")
    if req.status not in ("done", "no_preference", "reset"):
        raise HTTPException(status_code=400, detail="Status must be done, no_preference or reset.")
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["options"]:
            raise HTTPException(status_code=400, detail="The planning board isn't open yet.")
        if trip["final_plan"]:
            raise HTTPException(status_code=400, detail="The final plan is drafted — use 'Request a change' instead.")

        if req.status == "reset":
            conn.execute(
                "DELETE FROM category_progress WHERE trip_id = ? AND user_id = ? AND category = ?",
                (trip_id, user["id"], req.category),
            )
        else:
            if req.status == "done":
                starred = conn.execute(
                    "SELECT COUNT(*) c FROM stars WHERE trip_id = ? AND user_id = ? AND category = ?",
                    (trip_id, user["id"], req.category),
                ).fetchone()["c"]
                if starred == 0:
                    raise HTTPException(
                        status_code=400,
                        detail="Star at least one option first — or choose “I don't mind”.",
                    )
            if req.status == "no_preference":
                # "I don't mind" means no picks count for this category
                conn.execute(
                    "DELETE FROM stars WHERE trip_id = ? AND user_id = ? AND category = ?",
                    (trip_id, user["id"], req.category),
                )
            conn.execute(
                """INSERT INTO category_progress (trip_id, user_id, category, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(trip_id, user_id, category)
                   DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at""",
                (trip_id, user["id"], req.category, req.status, _now()),
            )
        trip = _load_trip(conn, trip_id)
        payload = _status_payload(conn, trip, user["id"])
    return {"success": True, "me": payload["me"], "all_curated": payload["all_curated"]}


# ---------------------------------------------------------------------------
# Final plan: synthesis, approval, edits
# ---------------------------------------------------------------------------

def _build_tallies(conn, trip_id: str, options: dict) -> dict:
    stars = conn.execute(
        "SELECT user_id, category, option_id FROM stars WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    progress = conn.execute(
        "SELECT user_id, category, status FROM category_progress WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)
    ).fetchone()["c"]

    tallies = {}
    for cat in CATEGORIES:
        counts = Counter(s["option_id"] for s in stars if s["category"] == cat)
        nopref = sum(1 for p in progress if p["category"] == cat and p["status"] == "no_preference")
        active = sum(1 for p in progress if p["category"] == cat and p["status"] == "done")
        tallies[cat] = {
            "counts": dict(counts),
            "no_preference": nopref,
            "active_voters": active,
            "total_members": total,
        }
    return tallies


@router.post("/{trip_id}/finalize")
def finalize_plan(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["final_plan"]:
            return {"plan": json.loads(trip["final_plan"])}
        if user["id"] != trip["leader_id"]:
            raise HTTPException(status_code=403, detail="Only the trip leader can draft the final plan.")
        if not trip["options"]:
            raise HTTPException(status_code=400, detail="The planning board isn't open yet.")
        if trip["final_plan_generating"]:
            raise HTTPException(status_code=409, detail="The final plan is already being drafted.")
        payload = _status_payload(conn, trip, user["id"])
        if not payload["all_curated"]:
            raise HTTPException(status_code=400, detail="Not everyone has finished starring yet.")
        options = json.loads(trip["options"])
        responses = _questionnaire_rows(conn, trip_id)
        tallies = _build_tallies(conn, trip_id, options)
        destination = _decided_destination(trip)
        conn.execute("UPDATE trips SET final_plan_generating = 1 WHERE id = ?", (trip_id,))

    try:
        plan = generate_final_plan(destination, responses, options, tallies)
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("UPDATE trips SET final_plan_generating = 0 WHERE id = ?", (trip_id,))
        raise HTTPException(status_code=502, detail=f"Final plan generation failed: {exc}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE trips SET final_plan = ?, final_plan_generating = 0, status = 'final_review' WHERE id = ?",
            (json.dumps(plan), trip_id),
        )
    return {"plan": plan}


def _member_checks(conn, trip) -> list[dict]:
    """Everyone pays the same equal share of group costs; flights are priced
    from each traveler's own departure city on top."""
    plan = json.loads(trip["final_plan"])
    costs = plan.get("cost_per_person", {})
    flight_by_city = plan.get("flight_cost_by_city", {})
    avg_flight = round(sum(flight_by_city.values()) / len(flight_by_city)) if flight_by_city else 0

    members = conn.execute(
        """SELECT u.id, u.name, u.email FROM trip_members t JOIN users u ON u.id = t.user_id
           WHERE t.trip_id = ? ORDER BY t.joined_at""",
        (trip["id"],),
    ).fetchall()
    q_rows = conn.execute(
        "SELECT user_id, data FROM questionnaires WHERE trip_id = ?", (trip["id"],)
    ).fetchall()
    city_by_user = {r["user_id"]: json.loads(r["data"]).get("departure_city", "").strip() for r in q_rows}
    payments = {
        r["user_id"]: r for r in conn.execute(
            "SELECT user_id, amount_cents, paid_at FROM payments WHERE trip_id = ?", (trip["id"],)
        )
    }

    shared_items = [
        ("stay", f"Stay — {plan.get('stay', {}).get('name', 'accommodation')} (your equal share)"),
        ("itinerary_activities", f"Itinerary activities — “{plan.get('itinerary', {}).get('title', 'the plan')}”"),
        ("visits", "Starred visits & sights"),
        ("events", "Starred events"),
        ("meals_estimate", "Meals (estimate)"),
        ("transport_estimate", "Local transport (estimate)"),
    ]

    checks = []
    for m in members:
        items = [
            {"label": label, "amount": round(costs.get(key) or 0, 2)}
            for key, label in shared_items
            if (costs.get(key) or 0) > 0
        ]
        city = city_by_user.get(m["id"], "")
        flight = next(
            (round(v, 2) for k, v in flight_by_city.items() if k.casefold() == city.casefold()),
            round(avg_flight, 2),
        )
        if flight > 0:
            items.append({"label": f"Round-trip flight from {city or 'your city'} (estimate)", "amount": flight})
        total = round(sum(i["amount"] for i in items), 2)
        paid = payments.get(m["id"])
        checks.append({
            "user_id": m["id"],
            "name": m["name"],
            "email": m["email"],
            "items": items,
            "total": total,
            "paid": bool(paid),
            "paid_at": paid["paid_at"] if paid else None,
        })
    return checks


@router.get("/{trip_id}/plan")
def read_plan(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        plan = json.loads(trip["final_plan"]) if trip["final_plan"] else None
        checks = _member_checks(conn, trip) if plan else []
        payload = _status_payload(conn, trip, user["id"])
    # Emails are private-ish: only expose your own
    for c in checks:
        if c["user_id"] != user["id"]:
            c.pop("email", None)
    slot_locks = {}
    if plan:
        for d in plan.get("itinerary", {}).get("days", []):
            slot_locks[str(d.get("day"))] = {
                slot: _slot_locked(d.get(slot, "")) for slot in _SLOT_ORDER
            }
    return {
        "plan": plan,
        "generating": bool(trip["final_plan_generating"]),
        "ai_available": ai_available(),
        "status": trip["status"],
        "checks": checks,
        "trip": payload,
        "replacement_pool": _replacement_pool(trip, plan) if plan else [],
        "slot_locks": slot_locks,
    }


def _replacement_pool(trip, plan: dict) -> list[dict]:
    """Planning-board visits/events not currently in the plan — offered as
    replacements when someone deletes an itinerary slot."""
    if not trip["options"]:
        return []
    options = json.loads(trip["options"])
    in_plan = {v.get("id") for v in plan.get("visits", []) + plan.get("events", [])}
    pool = []
    for cat in ("visits", "events"):
        for o in options.get(cat, []):
            if o.get("id") not in in_plan:
                pool.append({
                    "id": o["id"],
                    "category": cat,
                    "name": o.get("name"),
                    "description": o.get("description", ""),
                    "cost_per_person": o.get("cost_per_person", 0),
                    "date_hint": o.get("date_hint", ""),
                    "duration": o.get("duration", ""),
                    "image": o.get("image"),
                })
    return pool


@router.post("/{trip_id}/approve")
def approve_plan(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="There is no final plan to approve yet.")
        if trip["status"] not in ("final_review", "approved"):
            raise HTTPException(status_code=400, detail="Approvals aren't open right now.")
        conn.execute(
            "INSERT OR IGNORE INTO approvals (trip_id, user_id, approved_at) VALUES (?, ?, ?)",
            (trip_id, user["id"], _now()),
        )
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        approved = conn.execute("SELECT COUNT(*) c FROM approvals WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        all_approved = approved == total
        if all_approved and trip["status"] == "final_review":
            conn.execute("UPDATE trips SET status = 'approved' WHERE id = ?", (trip_id,))
    return {"success": True, "all_approved": all_approved}


@router.post("/{trip_id}/edit")
def edit_plan(trip_id: str, req: EditRequest, user: dict = Depends(get_current_user)):
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Describe the change you'd like.")
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="There is no final plan to edit yet.")
        payments = conn.execute("SELECT COUNT(*) c FROM payments WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        if payments > 0:
            raise HTTPException(status_code=400, detail="Payments have started — the plan is locked.")
        if trip["final_plan_generating"]:
            raise HTTPException(status_code=409, detail="Another revision is in progress.")
        plan = json.loads(trip["final_plan"])
        conn.execute("UPDATE trips SET final_plan_generating = 1 WHERE id = ?", (trip_id,))

    try:
        revised = revise_final_plan(plan, req.request.strip(), user["name"])
    except RevisionNotPossible as exc:
        with get_conn() as conn:
            conn.execute("UPDATE trips SET final_plan_generating = 0 WHERE id = ?", (trip_id,))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        with get_conn() as conn:
            conn.execute("UPDATE trips SET final_plan_generating = 0 WHERE id = ?", (trip_id,))
        raise HTTPException(status_code=502, detail=f"The revision failed — please try again. ({exc})")

    with get_conn() as conn:
        # Any change to the plan needs everyone's approval again
        conn.execute("DELETE FROM approvals WHERE trip_id = ?", (trip_id,))
        conn.execute(
            "UPDATE trips SET final_plan = ?, final_plan_generating = 0, status = 'final_review' WHERE id = ?",
            (json.dumps(revised), trip_id),
        )
    return {"plan": revised}


@router.post("/{trip_id}/reorder-days")
def reorder_days(trip_id: str, req: ReorderRequest, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="There is no final plan to edit yet.")
        payments = conn.execute("SELECT COUNT(*) c FROM payments WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        if payments > 0:
            raise HTTPException(status_code=400, detail="Payments have started — the plan is locked.")

        plan = json.loads(trip["final_plan"])
        days = plan.get("itinerary", {}).get("days", [])
        if sorted(req.order) != sorted(d.get("day") for d in days):
            raise HTTPException(status_code=400, detail="The new order must contain each existing day exactly once.")
        # Arrival and departure days are pinned to the calendar
        if len(days) >= 2 and (req.order[0] != days[0].get("day") or req.order[-1] != days[-1].get("day")):
            raise HTTPException(
                status_code=400,
                detail="Arrival and departure days are fixed — only the days in between can be reordered.",
            )

        by_number = {d.get("day"): d for d in days}
        dates_in_sequence = [d.get("date") for d in days]  # calendar stays fixed
        reordered = []
        for position, old_day in enumerate(req.order):
            day = dict(by_number[old_day])
            day["day"] = position + 1
            day["date"] = dates_in_sequence[position]
            reordered.append(day)
        plan["itinerary"]["days"] = reordered
        plan.setdefault("revision_notes", []).append(f"Days reordered by {user['name']}.")

        conn.execute("DELETE FROM approvals WHERE trip_id = ?", (trip_id,))
        conn.execute(
            "UPDATE trips SET final_plan = ?, status = 'final_review' WHERE id = ?",
            (json.dumps(plan), trip_id),
        )
    return {"plan": plan}


_SLOT_ORDER = ["morning", "afternoon", "evening"]
_FREE_SLOT = "Free time — recharge or wander."

# Fixed-schedule moments that can't be moved, replaced or deleted.
_LOCKED_SLOT_RE = re.compile(
    r"check[\s-]?in|check[\s-]?out|arriv|departure|depart\b|airport|flight home|drop bags",
    re.IGNORECASE,
)


def _slot_locked(text: str) -> bool:
    return bool(_LOCKED_SLOT_RE.search(text or ""))


def _slot_item_name(plan: dict, slot_text: str):
    """Find the plan visit/event whose name appears in a slot's text."""
    for cat in ("visits", "events"):
        for item in plan.get(cat, []):
            name = item.get("name", "")
            if name and name in (slot_text or ""):
                return cat, item
    return None, None


@router.post("/{trip_id}/plan/slot")
def edit_slot(trip_id: str, req: SlotEditRequest, user: dict = Depends(get_current_user)):
    if req.slot not in _SLOT_ORDER:
        raise HTTPException(status_code=400, detail="Slot must be morning, afternoon or evening.")
    if req.action not in ("delete", "replace", "move_up", "move_down"):
        raise HTTPException(status_code=400, detail="Unknown action.")
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="There is no final plan to edit yet.")
        payments = conn.execute("SELECT COUNT(*) c FROM payments WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        if payments > 0:
            raise HTTPException(status_code=400, detail="Payments have started — the plan is locked.")

        plan = json.loads(trip["final_plan"])
        days = plan.get("itinerary", {}).get("days", [])
        day = next((d for d in days if d.get("day") == req.day), None)
        if day is None:
            raise HTTPException(status_code=400, detail="That day doesn't exist in the plan.")

        if _slot_locked(day.get(req.slot, "")):
            raise HTTPException(
                status_code=400,
                detail="That's a fixed moment (check-in, check-out, arrival or departure) — it can't be moved or removed.",
            )

        note = ""
        if req.action in ("move_up", "move_down"):
            idx = _SLOT_ORDER.index(req.slot)
            other = idx - 1 if req.action == "move_up" else idx + 1
            if other < 0 or other >= len(_SLOT_ORDER):
                raise HTTPException(status_code=400, detail="Nothing to swap with in that direction.")
            a, b = req.slot, _SLOT_ORDER[other]
            if _slot_locked(day.get(_SLOT_ORDER[other], "")):
                raise HTTPException(status_code=400, detail="Can't swap with a fixed moment like check-in or check-out.")
            day[a], day[b] = day.get(b, ""), day.get(a, "")
            note = f"Day {req.day}: swapped {a} and {b} — {user['name']}."
        else:
            slot_text = day.get(req.slot, "")
            cat, item = _slot_item_name(plan, slot_text)
            if item is not None:
                plan[cat] = [i for i in plan[cat] if i.get("id") != item.get("id") or i.get("name") != item.get("name")]
            if req.action == "delete":
                day[req.slot] = _FREE_SLOT
                note = f"Day {req.day} {req.slot}: removed “{(item or {}).get('name', slot_text) or 'activity'}” — {user['name']}."
            else:  # replace
                pool = {o["id"]: o for o in _replacement_pool(trip, plan)}
                repl = pool.get(req.replacement_id)
                if repl is None:
                    raise HTTPException(status_code=400, detail="That replacement isn't available.")
                day[req.slot] = f"{repl['name']} — {repl['description']}".strip(" —")
                plan.setdefault(repl["category"], []).append(repl)
                note = (f"Day {req.day} {req.slot}: swapped in “{repl['name']}”"
                        + (f" for “{item['name']}”" if item else "") + f" — {user['name']}.")

        plan = recompute_plan_costs(plan)
        plan.setdefault("revision_notes", []).append(note)

        # Any change to the plan needs everyone's approval again
        conn.execute("DELETE FROM approvals WHERE trip_id = ?", (trip_id,))
        conn.execute(
            "UPDATE trips SET final_plan = ?, status = 'final_review' WHERE id = ?",
            (json.dumps(plan), trip_id),
        )
    return {"plan": plan}


@router.post("/{trip_id}/plan/day-slots")
def arrange_day_slots(trip_id: str, req: ArrangeSlotsRequest, user: dict = Depends(get_current_user)):
    """Rearrange one day's morning/afternoon/evening contents in a single move
    (drag-and-drop). `order` lists which OLD slot's content lands in each
    position, e.g. ["afternoon", "morning", "evening"]."""
    if sorted(req.order) != sorted(_SLOT_ORDER):
        raise HTTPException(status_code=400, detail="Order must contain morning, afternoon and evening exactly once.")
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="There is no final plan to edit yet.")
        payments = conn.execute("SELECT COUNT(*) c FROM payments WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        if payments > 0:
            raise HTTPException(status_code=400, detail="Payments have started — the plan is locked.")

        plan = json.loads(trip["final_plan"])
        days = plan.get("itinerary", {}).get("days", [])
        day = next((d for d in days if d.get("day") == req.day), None)
        if day is None:
            raise HTTPException(status_code=400, detail="That day doesn't exist in the plan.")
        if req.order == _SLOT_ORDER:
            return {"plan": plan}  # nothing moved

        # Fixed moments (check-in/out, arrival, departure) must stay put
        for position, source in zip(_SLOT_ORDER, req.order):
            if position != source and (_slot_locked(day.get(source, "")) or _slot_locked(day.get(position, ""))):
                raise HTTPException(
                    status_code=400,
                    detail="Fixed moments like check-in, check-out or your arrival can't be moved — rearrange around them.",
                )

        new_contents = {position: day.get(source, "") for position, source in zip(_SLOT_ORDER, req.order)}
        day.update(new_contents)
        plan.setdefault("revision_notes", []).append(f"Day {req.day}: schedule rearranged — {user['name']}.")

        conn.execute("DELETE FROM approvals WHERE trip_id = ?", (trip_id,))
        conn.execute(
            "UPDATE trips SET final_plan = ?, status = 'final_review' WHERE id = ?",
            (json.dumps(plan), trip_id),
        )
    return {"plan": plan}


# ---------------------------------------------------------------------------
# The check & payment
# ---------------------------------------------------------------------------

@router.get("/{trip_id}/check")
def read_check(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if not trip["final_plan"]:
            raise HTTPException(status_code=400, detail="No final plan yet.")
        checks = _member_checks(conn, trip)
    for c in checks:
        if c["user_id"] != user["id"]:
            c.pop("email", None)
    return {"checks": checks, "currency": "USD", "status": trip["status"]}


@router.post("/{trip_id}/pay")
def pay_share(trip_id: str, req: PayRequest | None = None, user: dict = Depends(get_current_user)):
    """Lock in how this traveler completes the trip.

    Phase A ships 'self_book': the traveler books every item directly with
    sellers and tracks it on the booking checklist — no money moves through
    Rovyn. 'full' (one payment, Rovyn auto-books everything) arrives with
    Phase B and is rejected until then.
    """
    mode = (req.mode if req else "full").strip()
    if mode == "full":
        raise HTTPException(
            status_code=400,
            detail="Paying in full through Rovyn arrives with Phase B — choose “I'll book it myself” for now.",
        )
    if mode != "self_book":
        raise HTTPException(status_code=400, detail="Unknown booking mode.")

    with get_conn() as conn:
        trip = _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        if trip["status"] not in ("approved", "booked"):
            raise HTTPException(status_code=400, detail="Everyone must approve the final plan before booking opens.")
        existing = conn.execute(
            "SELECT 1 FROM payments WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="You've already locked in how you'll book.")

        checks = _member_checks(conn, trip)
        mine = next((c for c in checks if c["user_id"] == user["id"]), None)
        if mine is None:
            raise HTTPException(status_code=400, detail="Could not compute your share.")

        plan = json.loads(trip["final_plan"])
        receipt_no = f"RV-{trip_id[:8].upper()}-{user['id'][:4].upper()}"
        # For self_book this is a commitment record, not a charge: the total is
        # the estimate the traveler pays sellers directly.
        receipt = {
            "receipt_no": receipt_no,
            "trip_name": trip["name"],
            "destination": plan.get("destination", ""),
            "dates": plan.get("dates", {}),
            "items": mine["items"],
            "total": mine["total"],
            "currency": "USD",
            "mode": mode,
            "paid_at": _now(),
            "demo": True,  # no real money moves in the MVP
        }

        total_members = conn.execute(
            "SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip_id,)
        ).fetchone()["c"]

        # No receipt email for self_book — nothing was charged; the booking
        # checklist emails each confirmation as it's recorded instead.
        email_result = {"sent": False, "skipped": True}

        conn.execute(
            """INSERT INTO payments (trip_id, user_id, amount_cents, currency, mode, receipt, email_status, paid_at)
               VALUES (?, ?, 0, 'USD', ?, ?, 'none', ?)""",
            (trip_id, user["id"], mode, json.dumps(receipt), receipt["paid_at"]),
        )
        paid = conn.execute("SELECT COUNT(*) c FROM payments WHERE trip_id = ?", (trip_id,)).fetchone()["c"]
        all_paid = paid == total_members
        if all_paid:
            conn.execute("UPDATE trips SET status = 'booked' WHERE id = ?", (trip_id,))

    return {"success": True, "receipt": receipt, "email": email_result, "all_paid": all_paid, "mode": mode}


@router.get("/{trip_id}/receipt")
def read_receipt(trip_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        _load_trip(conn, trip_id)
        _require_member(conn, trip_id, user["id"])
        row = conn.execute(
            "SELECT receipt, email_status FROM payments WHERE trip_id = ? AND user_id = ?",
            (trip_id, user["id"]),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="You haven't paid yet.")
    return {"receipt": json.loads(row["receipt"]), "email_status": row["email_status"]}


# ---------------------------------------------------------------------------
# Legacy itinerary & booking hub (pre-planning-board flow)
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
