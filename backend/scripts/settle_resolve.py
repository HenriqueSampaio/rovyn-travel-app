"""Operator tool for settlements that stopped in `needs_attention`.

    .venv/bin/python -m backend.scripts.settle_resolve <trip_id>            # show the settlement, items and events
    .venv/bin/python -m backend.scripts.settle_resolve <trip_id> --resolve  # mark it resolved after manual cleanup
    .venv/bin/python -m backend.scripts.settle_resolve <trip_id> --resolve --to partially_booked
                                                                           # …or back to booked/partially_booked when
                                                                           # the bookings stand and only money was fixed

`needs_attention` is where the reconciler parks a round it could not finish by
READING the providers (it retries a few times when the in-flight step is
resolvable, then gives up); no capture, cancel or refund is ever issued on a guess.
A person checks the supplier dashboards (LiteAPI bookings, Duffel orders,
Stripe payments) against the events printed here, fixes money by hand, then
marks the round resolved so the group can start a fresh checkout.

Run with DATABASE_URL/SUPABASE_URL set the way the app runs (or blank for the
local SQLite file).
"""
import json
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from backend.db import get_conn  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    trip_id, resolve = argv[0], "--resolve" in argv
    target = argv[argv.index("--to") + 1] if "--to" in argv and argv.index("--to") + 1 < len(argv) else "resolved"
    if target not in ("resolved", "booked", "partially_booked"):
        print("--to must be resolved, booked or partially_booked")
        return 1
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM settlements WHERE trip_id = ? ORDER BY round DESC LIMIT 1", (trip_id,)).fetchone()
        if row is None:
            print("no settlement for that trip")
            return 1
        print(f"settlement {row['id']} round {row['round']} status {row['status']} money_mode {row['money_mode']}")
        print(f"cursor: {row['cursor']}")
        lock = json.loads(row["lock"])
        for item in lock["items"]:
            print(f"  item {item['item_key']:40s} provider={item['provider']:9s} charge={item['charge']!s:5s} "
                  f"booked={'yes' if item.get('booking') else 'no':3s} failed={item.get('failed') or '-'}")
        for a in conn.execute("SELECT user_id, status, amount_cents, captured_cents, refunded_cents, payment_intent_id "
                              "FROM payment_attempts WHERE settlement_id = ?", (row["id"],)).fetchall():
            print(f"  attempt {a['user_id'][:8]} {a['status']:11s} amount={a['amount_cents']} captured={a['captured_cents']} "
                  f"refunded={a['refunded_cents']} pi={a['payment_intent_id']}")
        for e in conn.execute("SELECT at, step, item_key, level, message FROM settlement_events WHERE settlement_id = ? ORDER BY id",
                              (row["id"],)).fetchall():
            print(f"  {e['at']} {e['level']:5s} {e['step']:22s} {e['item_key'] or '':28s} {e['message']}")
        if resolve:
            if row["status"] != "needs_attention":
                print("only needs_attention settlements can be resolved by hand")
                return 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute("UPDATE settlements SET status = ?, cursor = NULL, updated_at = ? WHERE id = ? AND status = 'needs_attention'",
                         (target, now, row["id"]))
            conn.execute("INSERT INTO settlement_events (settlement_id, at, step, item_key, level, message, data_redacted) "
                         "VALUES (?, ?, 'resolve', NULL, 'info', ?, NULL)", (row["id"], now, f"marked {target} by operator"))
            print(f"marked {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
