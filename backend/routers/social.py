import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from backend.auth import get_current_user
from backend.db import get_conn
from backend.models import (
    CommentRequest, CopyCheckRequest, CopyTripRequest, CreatePostRequest, LikeRequest,
)
from backend.services.ai_service import (
    adapt_copied_plan, ai_available, normalize_snapshot,
)

router = APIRouter()

MEDIA_DIR = Path(__file__).parent.parent / "data" / "media"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
_MAX_UPLOAD_BYTES = 60 * 1024 * 1024
_MAX_MEDIA_PER_POST = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Media upload
# ---------------------------------------------------------------------------

@router.post("/media")
def upload_media(file: UploadFile, user: dict = Depends(get_current_user)):
    ext = Path(file.filename or "").suffix.lower()
    if ext in _IMAGE_EXTS:
        media_type = "image"
    elif ext in _VIDEO_EXTS:
        media_type = "video"
    else:
        raise HTTPException(status_code=400, detail="Unsupported file type — use JPG/PNG/WebP/GIF images or MP4/MOV/WebM video.")

    data = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large — keep uploads under 60 MB.")
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty.")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (MEDIA_DIR / name).write_bytes(data)
    return {"url": f"/media/{name}", "media_type": media_type}


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def _trip_snapshot(conn, trip_id: str, user_id: str) -> tuple[dict, str | None]:
    """Snapshot the trip's plan at post time so the shared itinerary never
    changes under the viewer (same principle as payments.receipt)."""
    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found.")
    member = conn.execute(
        "SELECT 1 FROM trip_members WHERE trip_id = ? AND user_id = ?", (trip_id, user_id)
    ).fetchone()
    if member is None:
        raise HTTPException(status_code=403, detail="You can only share trips you're part of.")
    if trip["final_plan"]:
        snapshot = {"kind": "final_plan", "plan": json.loads(trip["final_plan"])}
    elif trip["itinerary"]:
        snapshot = {"kind": "itinerary", "plan": json.loads(trip["itinerary"])}
    else:
        raise HTTPException(
            status_code=400,
            detail="This trip doesn't have a finished itinerary yet — finish planning before sharing it.",
        )
    destination = trip["selected_destination"] or trip["destination"] or snapshot["plan"].get("destination")
    return snapshot, destination


def _trip_top_vibes(conn, trip_id: str, limit: int = 3) -> list[str]:
    rows = conn.execute("SELECT data FROM questionnaires WHERE trip_id = ?", (trip_id,)).fetchall()
    counts = Counter(v for r in rows for v in json.loads(r["data"]).get("vibes", []))
    return [v for v, _ in counts.most_common(limit)]


def _post_type(media_rows: list) -> str:
    if len(media_rows) > 1:
        return "carousel"
    if media_rows and media_rows[0]["media_type"] == "video":
        return "video"
    return "photo"


def _post_payload(conn, post, user_id: str) -> dict:
    media = conn.execute(
        "SELECT media_type, url FROM post_media WHERE post_id = ? ORDER BY position", (post["id"],)
    ).fetchall()
    likes = conn.execute(
        "SELECT COUNT(*) c FROM post_likes WHERE post_id = ?", (post["id"],)
    ).fetchone()["c"]
    liked = conn.execute(
        "SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?", (post["id"], user_id)
    ).fetchone() is not None
    comments = conn.execute(
        "SELECT COUNT(*) c FROM post_comments WHERE post_id = ?", (post["id"],)
    ).fetchone()["c"]
    author = conn.execute("SELECT name FROM users WHERE id = ?", (post["user_id"],)).fetchone()
    return {
        "id": post["id"],
        "author_id": post["user_id"],
        "author_name": author["name"] if author else "Traveler",
        "is_mine": post["user_id"] == user_id,
        "caption": post["caption"],
        "destination": post["destination"],
        "vibes": json.loads(post["vibes"]),
        "has_trip": post["trip_snapshot"] is not None,
        "post_type": _post_type(media),
        "media": [{"media_type": m["media_type"], "url": m["url"]} for m in media],
        "like_count": likes,
        "liked_by_me": liked,
        "comment_count": comments,
        "created_at": post["created_at"],
    }


@router.post("/posts")
def create_post(req: CreatePostRequest, user: dict = Depends(get_current_user)):
    if not req.media:
        raise HTTPException(status_code=400, detail="Add at least one photo or video.")
    if len(req.media) > _MAX_MEDIA_PER_POST:
        raise HTTPException(status_code=400, detail=f"A post can hold at most {_MAX_MEDIA_PER_POST} items.")
    for m in req.media:
        if m.media_type not in ("image", "video"):
            raise HTTPException(status_code=400, detail="Media items must be images or videos.")
        name = Path(m.url).name
        if not m.url.startswith("/media/") or not (MEDIA_DIR / name).exists():
            raise HTTPException(status_code=400, detail="Upload media through Rovyn before posting it.")

    post_id = str(uuid.uuid4())
    with get_conn() as conn:
        snapshot, destination = (None, None)
        vibes = [v.strip() for v in req.vibes if v.strip()]
        if req.trip_id.strip():
            snapshot, destination = _trip_snapshot(conn, req.trip_id.strip(), user["id"])
            if not vibes:
                vibes = _trip_top_vibes(conn, req.trip_id.strip())
        conn.execute(
            """INSERT INTO posts (id, user_id, trip_id, trip_snapshot, caption, destination, vibes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, user["id"], req.trip_id.strip() or None,
             json.dumps(snapshot) if snapshot else None,
             req.caption.strip(), destination, json.dumps(vibes), _now()),
        )
        for i, m in enumerate(req.media):
            conn.execute(
                "INSERT INTO post_media (id, post_id, position, media_type, url) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), post_id, i, m.media_type, m.url),
            )
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return {"post": _post_payload(conn, post, user["id"])}


@router.get("/posts/{post_id}")
def read_post(post_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        return {"post": _post_payload(conn, post, user["id"])}


@router.delete("/posts/{post_id}")
def delete_post(post_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        if post["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="You can only delete your own posts.")
        media = conn.execute("SELECT url FROM post_media WHERE post_id = ?", (post_id,)).fetchall()
        conn.execute("DELETE FROM post_comments WHERE post_id = ?", (post_id,))
        conn.execute("DELETE FROM post_likes WHERE post_id = ?", (post_id,))
        conn.execute("DELETE FROM post_media WHERE post_id = ?", (post_id,))
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    for m in media:
        try:
            (MEDIA_DIR / Path(m["url"]).name).unlink(missing_ok=True)
        except OSError:
            pass
    return {"success": True}


# ---------------------------------------------------------------------------
# Feed — everything is public; ranking is tailored to the viewer's vibes
# ---------------------------------------------------------------------------

def _viewer_vibe_profile(conn, user_id: str) -> Counter:
    rows = conn.execute("SELECT data FROM questionnaires WHERE user_id = ?", (user_id,)).fetchall()
    return Counter(v for r in rows for v in json.loads(r["data"]).get("vibes", []))


@router.get("/feed")
def feed(limit: int = 20, offset: int = 0, user: dict = Depends(get_current_user)):
    limit = max(1, min(limit, 50))
    with get_conn() as conn:
        profile = _viewer_vibe_profile(conn, user["id"])
        posts = conn.execute("SELECT * FROM posts ORDER BY created_at DESC").fetchall()
        payloads = [_post_payload(conn, p, user["id"]) for p in posts]

    now = datetime.now(timezone.utc)

    def score(p: dict) -> float:
        # Preference match dominates; engagement and freshness break the rest.
        vibe_points = sum(profile.get(v, 0) for v in p["vibes"])
        engagement = 0.5 * p["like_count"] + 0.3 * p["comment_count"]
        try:
            age_days = (now - datetime.fromisoformat(p["created_at"])).days
        except ValueError:
            age_days = 0
        freshness = 2.0 if age_days <= 2 else (1.0 if age_days <= 7 else 0.0)
        return 3.0 * vibe_points + engagement + freshness

    payloads.sort(key=lambda p: (score(p), p["created_at"]), reverse=True)
    page = payloads[offset:offset + limit]
    return {
        "posts": page,
        "total": len(payloads),
        "personalized": bool(profile),
        "top_vibes": [v for v, _ in profile.most_common(3)],
    }


# ---------------------------------------------------------------------------
# Likes & comments
# ---------------------------------------------------------------------------

def _require_post(conn, post_id: str):
    post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found.")
    return post


@router.post("/posts/{post_id}/like")
def like_post(post_id: str, req: LikeRequest, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        _require_post(conn, post_id)
        if req.liked:
            conn.execute(
                "INSERT OR IGNORE INTO post_likes (post_id, user_id, liked_at) VALUES (?, ?, ?)",
                (post_id, user["id"], _now()),
            )
        else:
            conn.execute(
                "DELETE FROM post_likes WHERE post_id = ? AND user_id = ?", (post_id, user["id"])
            )
        count = conn.execute(
            "SELECT COUNT(*) c FROM post_likes WHERE post_id = ?", (post_id,)
        ).fetchone()["c"]
    return {"success": True, "like_count": count, "liked_by_me": req.liked}


@router.get("/posts/{post_id}/comments")
def read_comments(post_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        _require_post(conn, post_id)
        rows = conn.execute(
            """SELECT c.id, c.user_id, c.text, c.created_at, u.name
               FROM post_comments c JOIN users u ON u.id = c.user_id
               WHERE c.post_id = ? ORDER BY c.created_at""",
            (post_id,),
        ).fetchall()
    return {
        "comments": [
            {
                "id": r["id"],
                "author_id": r["user_id"],
                "author_name": r["name"],
                "is_mine": r["user_id"] == user["id"],
                "text": r["text"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.post("/posts/{post_id}/comments")
def add_comment(post_id: str, req: CommentRequest, user: dict = Depends(get_current_user)):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write something first.")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="Comments are capped at 1000 characters.")
    comment_id = str(uuid.uuid4())
    with get_conn() as conn:
        _require_post(conn, post_id)
        conn.execute(
            "INSERT INTO post_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (comment_id, post_id, user["id"], text, _now()),
        )
        count = conn.execute(
            "SELECT COUNT(*) c FROM post_comments WHERE post_id = ?", (post_id,)
        ).fetchone()["c"]
    return {
        "comment": {
            "id": comment_id,
            "author_id": user["id"],
            "author_name": user["name"],
            "is_mine": True,
            "text": text,
            "created_at": _now(),
        },
        "comment_count": count,
    }


@router.delete("/posts/{post_id}/comments/{comment_id}")
def delete_comment(post_id: str, comment_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        post = _require_post(conn, post_id)
        comment = conn.execute(
            "SELECT * FROM post_comments WHERE id = ? AND post_id = ?", (comment_id, post_id)
        ).fetchone()
        if comment is None:
            raise HTTPException(status_code=404, detail="Comment not found.")
        # The commenter or the post owner can remove a comment
        if user["id"] not in (comment["user_id"], post["user_id"]):
            raise HTTPException(status_code=403, detail="You can't delete this comment.")
        conn.execute("DELETE FROM post_comments WHERE id = ?", (comment_id,))
    return {"success": True}


# ---------------------------------------------------------------------------
# Shared trip: view the exact itinerary behind a post, then copy it
# ---------------------------------------------------------------------------

def _require_shared_trip(post) -> dict:
    if not post["trip_snapshot"]:
        raise HTTPException(status_code=400, detail="This post isn't linked to a trip.")
    return json.loads(post["trip_snapshot"])


@router.get("/posts/{post_id}/trip")
def read_shared_trip(post_id: str, user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        post = _require_post(conn, post_id)
        snapshot = _require_shared_trip(post)
        author = conn.execute("SELECT name FROM users WHERE id = ?", (post["user_id"],)).fetchone()
    return {
        "post_id": post["id"],
        "author_name": author["name"] if author else "Traveler",
        "destination": post["destination"],
        "caption": post["caption"],
        "plan": normalize_snapshot(snapshot),
        "ai_available": ai_available(),
    }


def _validate_copy_dates(start: str, end: str) -> None:
    try:
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Pick valid start and end dates.")
    if e <= s:
        raise HTTPException(status_code=400, detail="The end date must be after the start date.")
    if (e - s).days > 30:
        raise HTTPException(status_code=400, detail="Copied trips are capped at 30 days.")


@router.post("/posts/{post_id}/copy-check")
def copy_check(post_id: str, req: CopyCheckRequest, user: dict = Depends(get_current_user)):
    """AI feasibility pass: does this itinerary work on the copier's dates?"""
    _validate_copy_dates(req.start_date, req.end_date)
    with get_conn() as conn:
        post = _require_post(conn, post_id)
        snapshot = _require_shared_trip(post)
        author = conn.execute("SELECT name FROM users WHERE id = ?", (post["user_id"],)).fetchone()

    try:
        result = adapt_copied_plan(
            snapshot, req.start_date, req.end_date,
            original_author=author["name"] if author else "a traveler",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Feasibility check failed: {exc}")

    return {"report": result["report"], "plan": result["plan"], "ai_available": ai_available()}


@router.post("/posts/{post_id}/copy")
def copy_trip(post_id: str, req: CopyTripRequest, user: dict = Depends(get_current_user)):
    """Create the copier's own trip from the adapted plan they reviewed."""
    import random
    import string

    _validate_copy_dates(req.start_date, req.end_date)
    if not isinstance(req.adapted_plan, dict) or not (req.adapted_plan.get("itinerary") or {}).get("days"):
        raise HTTPException(status_code=400, detail="Run the feasibility check first — no adapted plan to copy.")

    with get_conn() as conn:
        post = _require_post(conn, post_id)
        _require_shared_trip(post)

        destination = req.adapted_plan.get("destination") or post["destination"] or "Copied trip"
        name = req.name.strip() or f"{destination} (copied)"
        plan = dict(req.adapted_plan)
        plan["dates"] = {"start": req.start_date, "end": req.end_date}

        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        while conn.execute("SELECT 1 FROM trips WHERE invite_code = ?", (code,)).fetchone():
            code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

        trip_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO trips (id, name, invite_code, leader_id, destination, selected_destination,
                                  status, final_plan, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'final_review', ?, ?)""",
            (trip_id, name, code, user["id"], destination, destination, json.dumps(plan), _now()),
        )
        conn.execute(
            "INSERT INTO trip_members (trip_id, user_id, joined_at) VALUES (?, ?, ?)",
            (trip_id, user["id"], _now()),
        )
    return {"id": trip_id, "name": name, "invite_code": code}
