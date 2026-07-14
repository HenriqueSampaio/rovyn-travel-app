"""Authentication — two modes, decided by environment.

Legacy (default): email/password with PBKDF2 hashes in `users` and opaque
bearer tokens in `auth_sessions`. Still what local dev uses out of the box.

Supabase (when SUPABASE_URL is set): the frontend signs in through Supabase
Auth (email/password, Google, Apple) and sends the Supabase access token —
a JWT — as the bearer token. We verify it against the project's JWKS
(ES256/RS256; HS256 via SUPABASE_JWT_SECRET for older projects), then
provision a row in `users` keyed by the Supabase user id. If a legacy
account already owns that email, its trips are re-keyed to the new id so
nobody loses data by switching sign-in methods.

Both token kinds are accepted concurrently: JWTs have two dots, legacy
tokens have none, so requests route to the right verifier either way.
"""

import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timezone

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.db import IntegrityError, get_conn

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_JWT_SECRET = (os.environ.get("SUPABASE_JWT_SECRET") or "").strip()

# Dev/test escape hatch: keeps the legacy signup/login endpoints available
# even when Supabase Auth is on — accounts are created instantly, with no
# email verification, which is what you want for fake test accounts.
# Never set this on a production deployment.
ALLOW_LEGACY_AUTH = (os.environ.get("ALLOW_LEGACY_AUTH") or "").strip().lower() in ("1", "true", "yes", "on")

_bearer = HTTPBearer(auto_error=False)


def supabase_enabled() -> bool:
    return bool(SUPABASE_URL)


def legacy_auth_allowed() -> bool:
    return not supabase_enabled() or ALLOW_LEGACY_AUTH


# ---------- legacy password auth ----------

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


def create_session(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, datetime.now(timezone.utc).isoformat()),
    )
    return token


# ---------- Supabase JWT verification ----------

_jwks_lock = threading.Lock()
_jwks_cache: dict = {"keys": [], "fetched_at": 0.0}
_JWKS_TTL = 600  # seconds


def _jwks_keys(force: bool = False) -> list:
    with _jwks_lock:
        fresh = time.time() - _jwks_cache["fetched_at"] < _JWKS_TTL
        if _jwks_cache["keys"] and fresh and not force:
            return _jwks_cache["keys"]
        resp = httpx.get(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json", timeout=10)
        resp.raise_for_status()
        _jwks_cache["keys"] = resp.json().get("keys", [])
        _jwks_cache["fetched_at"] = time.time()
        return _jwks_cache["keys"]


def _signing_key_for(token: str):
    import jwt as pyjwt

    kid = pyjwt.get_unverified_header(token).get("kid")
    for attempt in (False, True):  # second pass refetches, covering key rotation
        for k in _jwks_keys(force=attempt):
            if k.get("kid") == kid:
                return pyjwt.PyJWK(k).key
    raise HTTPException(status_code=401, detail="Unknown signing key — please sign in again.")


def verify_supabase_jwt(token: str) -> dict:
    """Return the verified claims of a Supabase access token, or raise 401."""
    import jwt as pyjwt

    try:
        alg = pyjwt.get_unverified_header(token).get("alg", "")
        if alg.startswith("HS"):
            if not SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status_code=401,
                    detail="This server can't verify HS256 tokens — set SUPABASE_JWT_SECRET.",
                )
            key, algorithms = SUPABASE_JWT_SECRET, ["HS256"]
        else:
            key, algorithms = _signing_key_for(token), ["ES256", "RS256"]
        return pyjwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience="authenticated",
            issuer=f"{SUPABASE_URL}/auth/v1",
            options={"require": ["exp", "sub"]},
        )
    except HTTPException:
        raise
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — please sign in again.")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session — please sign in again.")


def _claims_identity(claims: dict) -> tuple[str, str, str]:
    uid = claims["sub"]
    email = (claims.get("email") or "").lower()
    meta = claims.get("user_metadata") or {}
    name = (meta.get("full_name") or meta.get("name") or "").strip()
    if not name:
        name = email.split("@")[0] if email else "Traveler"
    return uid, email, name


def _adopt_legacy_user(conn, legacy_id: str, new_id: str) -> None:
    """Re-key a pre-Supabase account (matched by email) to the Supabase user id,
    so existing trips, stars and posts survive the switch to Supabase sign-in."""
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (legacy_id,))
    for table, col in [
        ("trips", "leader_id"),
        ("trip_members", "user_id"),
        ("questionnaires", "user_id"),
        ("votes", "user_id"),
        ("stars", "user_id"),
        ("category_progress", "user_id"),
        ("approvals", "user_id"),
        ("payments", "user_id"),
        ("bookings", "booked_by"),
        ("posts", "user_id"),
        ("post_likes", "user_id"),
        ("post_comments", "user_id"),
        ("wishlist_items", "user_id"),
        ("media_uploads", "owner_id"),
    ]:
        conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new_id, legacy_id))
    conn.execute("DELETE FROM users WHERE id = ?", (legacy_id,))


def _ensure_supabase_user(claims: dict) -> dict:
    uid, email, name = _claims_identity(claims)
    with get_conn() as conn:
        row = conn.execute("SELECT id, email, name FROM users WHERE id = ?", (uid,)).fetchone()
        if row:
            user = dict(row)
            if email and (user["email"] != email or user["name"] != name):
                conn.execute("UPDATE users SET email = ?, name = ? WHERE id = ?", (email, name, uid))
                user.update(email=email, name=name)
            return user

        legacy = None
        if email:
            legacy = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if legacy:
            # Free the unique email slot, insert the Supabase identity, move the data over.
            conn.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                (f"{email}#migrated-{legacy['id'][:8]}", legacy["id"]),
            )
        try:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, email or f"{uid}@users.rovyn", name, "", datetime.now(timezone.utc).isoformat()),
            )
        except IntegrityError:
            # Lost a race with a concurrent first request for the same user.
            conn.rollback()  # Postgres aborts the transaction on constraint errors
            row = conn.execute("SELECT id, email, name FROM users WHERE id = ?", (uid,)).fetchone()
            if row:
                return dict(row)
            raise HTTPException(status_code=401, detail="Could not establish your account — try again.")
        if legacy:
            _adopt_legacy_user(conn, legacy["id"], uid)
        return {"id": uid, "email": email, "name": name}


# ---------- request dependency ----------

# Resolving a token costs a database round trip on every request, which adds
# up fast when Postgres is remote. Verified tokens are cached in-process for
# a short window; the TTL bounds how long a revoked session or an expired JWT
# can outlive itself, and how stale a name/email edit can look.
_USER_CACHE_TTL = 60.0  # seconds
_USER_CACHE_MAX = 2000
_user_cache: dict[str, tuple[float, dict]] = {}
_user_cache_lock = threading.Lock()


def _user_cache_get(token: str) -> dict | None:
    with _user_cache_lock:
        hit = _user_cache.get(token)
        if hit is None:
            return None
        stamp, user = hit
        if time.time() - stamp >= _USER_CACHE_TTL:
            _user_cache.pop(token, None)
            return None
        return dict(user)


def _user_cache_put(token: str, user: dict) -> None:
    with _user_cache_lock:
        if len(_user_cache) >= _USER_CACHE_MAX:
            now = time.time()
            expired = [k for k, (t, _) in _user_cache.items() if now - t >= _USER_CACHE_TTL]
            for k in expired:
                _user_cache.pop(k, None)
            while len(_user_cache) >= _USER_CACHE_MAX:  # still full: drop oldest inserts
                _user_cache.pop(next(iter(_user_cache)))
        _user_cache[token] = (time.time(), dict(user))


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    token = credentials.credentials

    cached = _user_cache_get(token)
    if cached is not None:
        return cached

    if supabase_enabled() and token.count(".") == 2:
        claims = verify_supabase_jwt(token)
        user = _ensure_supabase_user(claims)
        _user_cache_put(token, user)
        return user

    with get_conn(readonly=True) as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.name FROM auth_sessions s
               JOIN users u ON u.id = s.user_id WHERE s.token = ?""",
            (token,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Session expired — please sign in again.")
    user = dict(row)
    _user_cache_put(token, user)
    return user
