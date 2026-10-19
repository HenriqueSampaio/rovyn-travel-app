"""The booking checklist derived from the final plan — shared by the Phase A
checklist router and the settlement engine (which books some of these items
itself). Lives in services/ so neither router has to import the other.

Rules the settlement relies on:
- item keys are deterministic (`flight:<city>`, `stay:<town>:<name>`, …) and
  identical wherever they are derived;
- rows that represent a REAL reservation (made by a traveler or by Rovyn) are
  never deleted or overwritten by a plan change — they are flagged stale.
"""
import json
from collections import Counter
from datetime import datetime, timezone

from backend.db import fetch_many
from backend.services import links

# Rows in these states carry a reservation somebody holds — never touch their
# booking fields from a plan reconcile, never delete them.
REAL_STATUSES = ("booked", "pending_confirmation")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checklist_from_plan(trip, group_size: int, city_counts: Counter) -> list[dict]:
    """Deterministic checklist items derived from the CURRENT final plan."""
    plan = json.loads(trip["final_plan"])
    destination = plan.get("destination", "")
    dates = plan.get("dates", {})
    start, end = dates.get("start", ""), dates.get("end", "")

    items = []
    for city, f in (plan.get("flights") or {}).items():
        adults = city_counts.get(city.strip().casefold(), 1)
        items.append({
            "item_key": f"flight:{city}",
            "category": "flight",
            "scope": f"city:{city}",
            "title": f"Round-trip flight from {city} — {f.get('airline', '')}".strip(" —"),
            "link": links.safe_url(f.get("link"))
                    or links.expedia_flights(city, destination, start, end, adults),
            "details": f,
        })

    for leg in plan.get("transport") or []:
        title_bits = [p for p in (leg.get("mode"), leg.get("carrier")) if p]
        items.append({
            "item_key": f"transport:{leg.get('leg', '?')}",
            "category": "transport",
            "scope": "group",
            "title": f"{leg.get('leg', 'Intercity leg')} — {' · '.join(title_bits)}".strip(" —"),
            "link": links.safe_url(leg.get("link"))
                    or links.rome2rio(leg.get("from", ""), leg.get("to", "")),
            "details": leg,
        })

    # One stay per route city on multi-city trips; the single stay otherwise.
    # Primary link is the BOOKABLE one (dates + party prefilled, affiliate-
    # tagged); the official website is shown separately in the UI.
    for stay in (plan.get("stays") or ([plan["stay"]] if plan.get("stay") else [])):
        if not stay.get("name"):
            continue
        city = (stay.get("city") or "").strip()
        town = city.split(",")[0].strip()
        items.append({
            "item_key": f"stay:{town + ':' if town else ''}{stay['name']}",
            "category": "stay",
            "scope": "group",
            "title": f"Stay{f' in {town}' if town else ''} — {stay['name']}",
            "link": links.safe_url((stay.get("links") or {}).get("booking"))
                    or links.booking_stays(city or destination,
                                           stay.get("check_in") or start,
                                           stay.get("check_out") or end, group_size),
            "details": stay,
        })

    for cat, plural in (("visit", "visits"), ("event", "events")):
        for idx, item in enumerate(plan.get(plural) or []):
            ident = item.get("id") or item.get("name") or f"item-{idx}"
            items.append({
                "item_key": f"{cat}:{ident}",
                "category": cat,
                "scope": "group",
                "title": item.get("name", cat.capitalize()),
                "link": links.safe_url((item.get("links") or {}).get("getyourguide"))
                        or links.viator_tours(destination),
                "details": item,
            })
    return items


def sync_checklist(conn, trip) -> None:
    """Reconcile bookings rows with the current plan.

    - new plan items appear (provider 'self');
    - existing items get refreshed title/link/details/scope — never their
      booking fields (provider, status, confirmation, payer, amounts, ids);
    - unbooked self rows that left the plan are deleted;
    - rows holding a real reservation (booked by a traveler or by Rovyn) that
      left the plan are kept and flagged stale — someone must cancel or keep.
    """
    count_rows, q_rows, existing_rows = fetch_many(conn, [
        ("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (trip["id"],)),
        ("SELECT data FROM questionnaires WHERE trip_id = ?", (trip["id"],)),
        ("SELECT item_key, status, provider FROM bookings WHERE trip_id = ?", (trip["id"],)),
    ])
    group_size = count_rows[0]["c"] or 1
    city_counts = Counter(
        json.loads(r["data"]).get("departure_city", "").strip().casefold()
        for r in q_rows
        if json.loads(r["data"]).get("departure_city", "").strip()
    )
    current = {i["item_key"]: i for i in checklist_from_plan(trip, group_size, city_counts)}
    existing = {r["item_key"]: r for r in existing_rows}

    def is_real(row) -> bool:
        return row["provider"] != "self" or row["status"] in REAL_STATUSES

    updates, inserts = [], []
    for key, item in current.items():
        if key in existing:
            updates.append((item["title"], item["link"], json.dumps(item["details"]),
                            item["scope"], trip["id"], key))
        else:
            inserts.append((trip["id"], key, item["category"], item["scope"], item["title"],
                            item["link"], json.dumps(item["details"]), _now()))
    gone = [key for key in existing if key not in current]
    stale = [(trip["id"], k) for k in gone if is_real(existing[k])]
    drop = [(trip["id"], k) for k in gone if not is_real(existing[k])]

    if updates:
        conn.executemany(
            """UPDATE bookings SET title = ?, link = ?, details = ?, scope = ?, stale = 0
               WHERE trip_id = ? AND item_key = ?""",
            updates,
        )
    if inserts:
        conn.executemany(
            """INSERT INTO bookings
               (trip_id, item_key, category, scope, title, provider, link, details, created_at)
               VALUES (?, ?, ?, ?, ?, 'self', ?, ?, ?)""",
            inserts,
        )
    if stale:
        conn.executemany("UPDATE bookings SET stale = 1 WHERE trip_id = ? AND item_key = ?", stale)
    if drop:
        conn.executemany("DELETE FROM bookings WHERE trip_id = ? AND item_key = ?", drop)


def trip_is_locked(conn, trip_id: str) -> bool:
    """Money is in motion: an active settlement, or a traveler whose payment
    is pending, authorized or captured. The plan can't change under them."""
    active = conn.execute(
        "SELECT 1 FROM settlements WHERE trip_id = ? AND status NOT IN ('cancelled', 'failed', 'resolved')",
        (trip_id,)).fetchone()
    if active:
        return True
    paid = conn.execute(
        "SELECT 1 FROM payments WHERE trip_id = ? AND status NOT IN ('released', 'failed', 'expired')",
        (trip_id,)).fetchone()
    return paid is not None
