import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "data" / "rovyn.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    id                         TEXT PRIMARY KEY,
    name                       TEXT NOT NULL,
    invite_code                TEXT UNIQUE NOT NULL,
    leader_id                  TEXT NOT NULL REFERENCES users(id),
    status                     TEXT NOT NULL DEFAULT 'waiting',
    recommendations            TEXT,
    recommendations_generating INTEGER NOT NULL DEFAULT 0,
    itinerary                  TEXT,
    itinerary_generating       INTEGER NOT NULL DEFAULT 0,
    selected_destination       TEXT,
    destination                TEXT,
    options                    TEXT,
    options_generating         INTEGER NOT NULL DEFAULT 0,
    final_plan                 TEXT,
    final_plan_generating      INTEGER NOT NULL DEFAULT 0,
    created_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_members (
    trip_id   TEXT NOT NULL REFERENCES trips(id),
    user_id   TEXT NOT NULL REFERENCES users(id),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS questionnaires (
    trip_id      TEXT NOT NULL REFERENCES trips(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    data         TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS votes (
    trip_id          TEXT NOT NULL REFERENCES trips(id),
    user_id          TEXT NOT NULL REFERENCES users(id),
    destination_name TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS stars (
    trip_id   TEXT NOT NULL REFERENCES trips(id),
    user_id   TEXT NOT NULL REFERENCES users(id),
    category  TEXT NOT NULL,
    option_id TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id, category, option_id)
);

CREATE TABLE IF NOT EXISTS category_progress (
    trip_id    TEXT NOT NULL REFERENCES trips(id),
    user_id    TEXT NOT NULL REFERENCES users(id),
    category   TEXT NOT NULL,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id, category)
);

CREATE TABLE IF NOT EXISTS approvals (
    trip_id     TEXT NOT NULL REFERENCES trips(id),
    user_id     TEXT NOT NULL REFERENCES users(id),
    approved_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS payments (
    trip_id      TEXT NOT NULL REFERENCES trips(id),
    user_id      TEXT NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    receipt      TEXT NOT NULL,
    email_status TEXT NOT NULL DEFAULT 'none',
    paid_at      TEXT NOT NULL,
    PRIMARY KEY (trip_id, user_id)
);

CREATE TABLE IF NOT EXISTS bookings (
    trip_id      TEXT NOT NULL REFERENCES trips(id),
    item_key     TEXT NOT NULL,   -- deterministic: 'flight:London', 'stay:Hotel X', 'visit:visits-2'
    category     TEXT NOT NULL,   -- flight | stay | visit | event
    scope        TEXT NOT NULL DEFAULT 'group',  -- 'group' or 'city:<departure city>'
    title        TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT 'self',   -- who fulfills it (Phase A: always self)
    link         TEXT,            -- deep link to the seller
    status       TEXT NOT NULL DEFAULT 'to_book',  -- to_book | booked
    confirmation TEXT,            -- PNR / hotel code / voucher number pasted by traveler
    note         TEXT,
    booked_by    TEXT REFERENCES users(id),
    booked_at    TEXT,
    details      TEXT,            -- JSON snapshot of the plan item
    stale        INTEGER NOT NULL DEFAULT 0,  -- booked item no longer in the plan (needs cancelling)
    created_at   TEXT NOT NULL,
    PRIMARY KEY (trip_id, item_key)
);

CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id),
    trip_id       TEXT REFERENCES trips(id),
    trip_snapshot TEXT,
    caption       TEXT NOT NULL DEFAULT '',
    destination   TEXT,
    vibes         TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_media (
    id         TEXT PRIMARY KEY,
    post_id    TEXT NOT NULL REFERENCES posts(id),
    position   INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    url        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_likes (
    post_id  TEXT NOT NULL REFERENCES posts(id),
    user_id  TEXT NOT NULL REFERENCES users(id),
    liked_at TEXT NOT NULL,
    PRIMARY KEY (post_id, user_id)
);

CREATE TABLE IF NOT EXISTS post_comments (
    id         TEXT PRIMARY KEY,
    post_id    TEXT NOT NULL REFERENCES posts(id),
    user_id    TEXT NOT NULL REFERENCES users(id),
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Columns added after the first release; applied to pre-existing databases.
_TRIP_MIGRATIONS = [
    ("destination", "TEXT"),
    ("options", "TEXT"),
    ("options_generating", "INTEGER NOT NULL DEFAULT 0"),
    ("final_plan", "TEXT"),
    ("final_plan_generating", "INTEGER NOT NULL DEFAULT 0"),
]


def get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(trips)")}
        for col, ddl in _TRIP_MIGRATIONS:
            if col not in existing:
                conn.execute(f"ALTER TABLE trips ADD COLUMN {col} {ddl}")
        booking_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bookings)")}
        if booking_cols and "stale" not in booking_cols:
            conn.execute("ALTER TABLE bookings ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")


init_db()
