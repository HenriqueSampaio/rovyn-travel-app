import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import (
    create_session, get_current_user, hash_password, legacy_auth_allowed,
    supabase_enabled, verify_password,
)
from backend.db import IntegrityError, get_conn
from backend.models import LoginRequest, SignupRequest

router = APIRouter()


def _require_legacy_mode():
    if not legacy_auth_allowed():
        # Passwords live in Supabase Auth on this deployment; these endpoints
        # would mint tokens outside it, so they stay off — unless the
        # ALLOW_LEGACY_AUTH dev flag opens them for instant test accounts.
        raise HTTPException(
            status_code=400,
            detail="This deployment signs in through Supabase — use the sign-in page.",
        )


@router.get("/config")
def auth_config():
    """Which sign-in paths this deployment offers — the sign-in page reads
    this to decide whether to show the instant (no-verification) email form."""
    return {"supabase": supabase_enabled(), "legacy": legacy_auth_allowed()}


@router.post("/signup")
def signup(req: SignupRequest):
    _require_legacy_mode()
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Please tell us your name.")

    user_id = str(uuid.uuid4())
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, req.email.lower(), req.name.strip(), hash_password(req.password),
                 datetime.now(timezone.utc).isoformat()),
            )
        except IntegrityError:
            raise HTTPException(status_code=400, detail="An account with that email already exists.")
        token = create_session(conn, user_id)

    return {"token": token, "user": {"id": user_id, "email": req.email.lower(), "name": req.name.strip()}}


@router.post("/login")
def login(req: LoginRequest):
    _require_legacy_mode()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (req.email.lower(),)).fetchone()
        if row is None or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Wrong email or password.")
        token = create_session(conn, row["id"])

    return {"token": token, "user": {"id": row["id"], "email": row["email"], "name": row["name"]}}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}
