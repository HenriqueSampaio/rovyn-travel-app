"""Copy the local SQLite database into Postgres/Supabase.

Usage:
    DATABASE_URL=postgresql://... python -m backend.scripts.migrate_to_postgres
    (or pass the URL as the first argument)

The target's schema must already be in place: apply supabase/migrations
first (`supabase db push`) — this script copies data, it does not create
tables, and it exits with instructions if the schema is missing.

Idempotent: rows that already exist in Postgres are left alone
(ON CONFLICT DO NOTHING), so re-running after a partial copy is safe.
Tables are copied in foreign-key order.

Note on accounts: rows in `users` carry the app's legacy PBKDF2 hashes,
which Supabase Auth cannot read. When the app runs with Supabase Auth,
a traveler who signs in with the same email is automatically re-keyed
to their new Supabase identity and keeps their trips.
"""

import os
import sqlite3
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()  # picks up DATABASE_URL from Rovyn/.env

SQLITE_PATH = Path(__file__).resolve().parent.parent / "data" / "rovyn.db"

# Parents before children.
TABLES = [
    "users",
    "auth_sessions",
    "trips",
    "trip_members",
    "questionnaires",
    "votes",
    "stars",
    "category_progress",
    "approvals",
    "payments",
    "bookings",
    "posts",
    "post_media",
    "post_likes",
    "post_comments",
]


def main() -> None:
    url = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL", "")).strip()
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("Pass a Postgres connection string via DATABASE_URL or as the first argument.")
    if not SQLITE_PATH.exists():
        sys.exit(f"No SQLite database found at {SQLITE_PATH} — nothing to migrate.")

    # Verify the target schema before copying — importing backend.db runs
    # init_db, which in Postgres mode checks the supabase/migrations schema
    # is applied and raises with `supabase db push` instructions if not.
    os.environ["DATABASE_URL"] = url
    from backend import db  # noqa: PLC0415  (import runs init_db against DATABASE_URL)

    assert db.IS_POSTGRES, "backend.db did not come up in Postgres mode"

    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row

    with psycopg.connect(url) as dst:
        for table in TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"{table:20} — empty")
                continue
            cols = rows[0].keys()
            col_list = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))
            stmt = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                "ON CONFLICT DO NOTHING"
            )
            with dst.cursor() as cur:
                cur.executemany(stmt, [tuple(r) for r in rows])
            print(f"{table:20} — {len(rows)} rows copied")
        dst.commit()

    print("\nDone. Point the backend at Postgres by keeping DATABASE_URL in Rovyn/.env.")


if __name__ == "__main__":
    main()
