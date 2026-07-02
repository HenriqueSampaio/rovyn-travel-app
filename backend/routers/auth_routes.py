import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import create_session, get_current_user, hash_password, verify_password
from backend.db import get_conn
from backend.models import LoginRequest, SignupRequest

router = APIRouter()


@router.post("/signup")
def signup(req: SignupRequest):
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
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="An account with that email already exists.")
        token = create_session(conn, user_id)

    return {"token": token, "user": {"id": user_id, "email": req.email.lower(), "name": req.name.strip()}}


@router.post("/login")
def login(req: LoginRequest):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (req.email.lower(),)).fetchone()
        if row is None or not verify_password(req.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Wrong email or password.")
        token = create_session(conn, row["id"])

    return {"token": token, "user": {"id": row["id"], "email": row["email"], "name": row["name"]}}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}
