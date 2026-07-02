import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.db import get_conn

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return secrets.compare_digest(digest.hex(), expected)


def create_session(conn: sqlite3.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, datetime.now(timezone.utc).isoformat()),
    )
    return token


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.name FROM auth_sessions s
               JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
            (credentials.credentials,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Session expired — please sign in again.")
    return dict(row)
