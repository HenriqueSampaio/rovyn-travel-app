"""Database layer — SQLite by default, Postgres when DATABASE_URL is set.

The whole app writes plain SQL with `?` placeholders and reads rows by
column name, so both engines run the same statements: in Postgres mode a
thin connection proxy rewrites `?` to `%s` (outside string literals) and
psycopg's dict_row keeps `row["col"]` working. Usage in routers stays
`with get_conn() as conn:` — commit on success, rollback on exception.

Set DATABASE_URL to a Postgres/Supabase connection string to switch, e.g.
postgresql://postgres.<ref>:<password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

_DB_PATH = Path(__file__).parent / "data" / "rovyn.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
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
    stay_media                 TEXT,
    trip_mode                  TEXT,
    city_ideas                 TEXT,
    solo                       INTEGER NOT NULL DEFAULT 0,
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
    mode         TEXT NOT NULL DEFAULT 'full',  -- 'full' (Rovyn books, Phase B) | 'self_book'
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

CREATE TABLE IF NOT EXISTS wishlist_items (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id),
    post_id       TEXT NOT NULL,  -- no FK: the saved snapshot outlives the post
    author_name   TEXT NOT NULL,
    destination   TEXT,
    caption       TEXT NOT NULL DEFAULT '',
    trip_snapshot TEXT NOT NULL,
    added_at      TEXT NOT NULL,
    UNIQUE (user_id, post_id)
);
"""

# Columns added after the first release; applied to pre-existing databases.
_COLUMN_MIGRATIONS = [
    ("trips", "destination", "TEXT"),
    ("trips", "options", "TEXT"),
    ("trips", "options_generating", "INTEGER NOT NULL DEFAULT 0"),
    ("trips", "final_plan", "TEXT"),
    ("trips", "final_plan_generating", "INTEGER NOT NULL DEFAULT 0"),
    ("trips", "stay_media", "TEXT"),
    ("trips", "trip_mode", "TEXT"),      # 'single' | 'multi' — set when the board is generated
    ("trips", "city_ideas", "TEXT"),     # cached nearby-city suggestions for the questionnaire
    ("trips", "solo", "INTEGER NOT NULL DEFAULT 0"),  # one traveler — no invites, tailored copy
    ("bookings", "stale", "INTEGER NOT NULL DEFAULT 0"),
    ("payments", "mode", "TEXT NOT NULL DEFAULT 'full'"),
]

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)

    # A remote Postgres (Supabase) makes every round trip expensive, so the
    # pool keeps a few connections warm (opening one costs a full TCP+TLS+auth
    # handshake) and TCP keepalives stop NATs/poolers from dropping idle ones.
    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=int(os.environ.get("DB_POOL_MIN", "3")),
        max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        kwargs={
            "row_factory": dict_row,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        },
        open=False,
    )

    def _translate(sql: str) -> str:
        """Rewrite `?` placeholders to `%s`, leaving quoted literals alone."""
        out = []
        in_string = False
        for ch in sql:
            if ch == "'":
                in_string = not in_string
                out.append(ch)
            elif ch == "?" and not in_string:
                out.append("%s")
            else:
                out.append(ch)
        return "".join(out)

    class _PgConn:
        """Duck-types the sqlite3.Connection surface the routers use."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            return self._conn.execute(_translate(sql), params)

        def executemany(self, sql, seq_of_params):
            # psycopg batches executemany through a pipeline internally,
            # so this is one round trip instead of one per row.
            return self._conn.executemany(_translate(sql), list(seq_of_params))

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def get_conn(readonly: bool = False):
        """Checkout a pooled connection.

        readonly=True runs the block in autocommit: no BEGIN/COMMIT round
        trips around SELECTs, which saves ~2 network round trips per request.
        Only use it for blocks that never write.
        """
        with _pool.connection() as conn:
            if readonly:
                conn.autocommit = True
                try:
                    yield _PgConn(conn)
                finally:
                    conn.autocommit = False
            else:
                yield _PgConn(conn)

    _PIPELINE_OK = psycopg.Pipeline.is_supported()

    def fetch_many(conn, queries):
        """Run independent SELECTs and return their fetchall() lists.

        The statements travel to Postgres in one libpq pipeline — one network
        round trip instead of one per query. `queries` is [(sql, params), ...].
        """
        raw = conn._conn
        if not _PIPELINE_OK:
            return [conn.execute(sql, params).fetchall() for sql, params in queries]
        with raw.pipeline():
            cursors = [raw.execute(_translate(sql), tuple(params)) for sql, params in queries]
        return [c.fetchall() for c in cursors]

    def _existing_columns(conn, table: str) -> set:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        ).fetchall()
        return {r["column_name"] for r in rows}

    def init_db() -> None:
        try:
            _pool.open(wait=True, timeout=15)
        except Exception as exc:  # unreachable/wrong DATABASE_URL should fail loudly
            raise RuntimeError(
                f"Could not connect to DATABASE_URL ({exc}). Check the connection "
                "string (Supabase → Project Settings → Database → Connection string; "
                "use the session pooler URI and URL-encode the password)."
            ) from exc
        with _pool.connection() as conn:
            conn.execute(_SCHEMA)
            for table, col, ddl in _COLUMN_MIGRATIONS:
                if col not in _existing_columns(conn, table):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}")

else:
    IntegrityError = (sqlite3.IntegrityError,)

    def _raw_sqlite_conn() -> sqlite3.Connection:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def get_conn(readonly: bool = False):  # readonly is a no-op for local SQLite
        conn = _raw_sqlite_conn()
        try:
            with conn:  # sqlite's own context manager: commit / rollback
                yield conn
        finally:
            conn.close()

    def fetch_many(conn, queries):
        """SQLite is in-process — sequential execution is already free."""
        return [conn.execute(sql, params).fetchall() for sql, params in queries]

    def init_db() -> None:
        conn = _raw_sqlite_conn()
        try:
            conn.executescript(_SCHEMA)
            for table, col, ddl in _COLUMN_MIGRATIONS:
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if existing and col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            conn.commit()
        finally:
            conn.close()


init_db()
