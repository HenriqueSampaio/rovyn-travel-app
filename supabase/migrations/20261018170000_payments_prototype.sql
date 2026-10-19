-- Payments & auto-booking prototype (Rovyn as merchant of record).
-- Mirrors the SQLite _SCHEMA / _COLUMN_MIGRATIONS entries in backend/db.py.
-- Canary: bookings.cancelled_at (checked at Postgres boot).

CREATE TABLE IF NOT EXISTS settlements (
    id            TEXT PRIMARY KEY,
    trip_id       TEXT NOT NULL REFERENCES trips(id),
    round         INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'collecting',
    money_mode    TEXT NOT NULL DEFAULT 'simulated',
    currency      TEXT NOT NULL DEFAULT 'USD',
    lock          TEXT NOT NULL,
    plan_hash     TEXT NOT NULL,
    cursor        TEXT,
    deadline      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS settlements_active_idx ON settlements(trip_id)
    WHERE status NOT IN ('cancelled', 'failed', 'resolved');

CREATE TABLE IF NOT EXISTS settlement_events (
    id            BIGSERIAL PRIMARY KEY,
    settlement_id TEXT NOT NULL REFERENCES settlements(id),
    at            TEXT NOT NULL,
    step          TEXT NOT NULL,
    item_key      TEXT,
    level         TEXT NOT NULL DEFAULT 'info',
    message       TEXT NOT NULL DEFAULT '',
    data_redacted TEXT
);

CREATE TABLE IF NOT EXISTS payment_attempts (
    id                  TEXT PRIMARY KEY,
    trip_id             TEXT NOT NULL REFERENCES trips(id),
    user_id             TEXT NOT NULL REFERENCES users(id),
    settlement_id       TEXT NOT NULL REFERENCES settlements(id),
    round               INTEGER NOT NULL DEFAULT 1,
    provider            TEXT NOT NULL DEFAULT 'simulated',
    checkout_session_id TEXT UNIQUE,
    payment_intent_id   TEXT UNIQUE,
    status              TEXT NOT NULL DEFAULT 'pending',
    amount_cents        INTEGER NOT NULL,
    capture_before      TEXT,
    captured_cents      INTEGER,
    refunded_cents      INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS payment_attempts_active_idx ON payment_attempts(settlement_id, user_id)
    WHERE status IN ('pending', 'authorized', 'nothing_due', 'releasing', 'captured');

CREATE TABLE IF NOT EXISTS refunds (
    id              TEXT PRIMARY KEY,
    settlement_id   TEXT NOT NULL REFERENCES settlements(id),
    item_key        TEXT,
    user_id         TEXT NOT NULL REFERENCES users(id),
    attempt_id      TEXT NOT NULL REFERENCES payment_attempts(id),
    reason          TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    provider_ref    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_events (
    id            TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    type          TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    processed_at  TEXT,
    body_redacted TEXT
);

CREATE TABLE IF NOT EXISTS traveler_profiles (
    user_id     TEXT PRIMARY KEY REFERENCES users(id),
    given_name  TEXT NOT NULL,
    family_name TEXT NOT NULL,
    born_on     TEXT,
    gender      TEXT,
    title       TEXT,
    phone       TEXT,
    updated_at  TEXT NOT NULL
);

ALTER TABLE payments ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'paid';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'none';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS current_attempt_id TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS captured_at TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS captured_cents INTEGER;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS refunded_cents INTEGER NOT NULL DEFAULT 0;

ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payer_id TEXT REFERENCES users(id);
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS amount_cents INTEGER;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS provider_booking_id TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS documents TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS raw TEXT;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_at TEXT;
