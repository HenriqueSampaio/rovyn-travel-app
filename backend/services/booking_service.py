"""The money-and-booking orchestrator — Rovyn as merchant of record.

Phase C of docs/BOOKING-ARCHITECTURE.md, specified in docs/PAYMENTS-MOR-SPEC.md:

    approved plan ──► VERIFY & LOCK   live prices from the suppliers; flight holds placed
                 ──► COLLECT          every traveler authorizes their share — nobody is charged
                 ──► settle()         PREFLIGHT → RE-VERIFY → BOOK (pre-capture-safe hotels)
                                      → CAPTURE → BOOK (the rest) → PAY FLIGHTS → CONFIRM
                                      refund any item that fails after capture

Rules that shape the code:

- Suppliers first, then capture, but only for reservations that can be undone
  for free (refundable hotel rates well inside their deadline, airline holds).
  Everything else is bought after the cards are captured and refunded if it
  fails. Nothing non-refundable is bought before every capture succeeded.
- No provider call ever runs inside an open database connection. Each call is
  bracketed: write the cursor and commit, call, write the result and commit.
  A dead process leaves a cursor the reconciler can resolve by READING the
  provider — money-moving calls are never repeated on a guess.
- Every status transition is a compare-and-set with its rowcount checked.
- Personal data is redacted before anything is persisted as audit.
- One money mode per settlement (simulated | test | live), decided at lock
  time, re-checked against every credential and every provider object.
"""
import hashlib
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

import certifi

from backend.db import IntegrityError, fetch_many, get_conn
from backend.services import duffel_booking as duffel
from backend.services import email_service
from backend.services import payment_service as stripe
from backend.services.checklist import REAL_STATUSES, checklist_from_plan, sync_checklist
from backend.services.redact import redact, redacted_json, request_hash

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_LITE_SEARCH = "https://api.liteapi.travel/v3.0"
_LITE_BOOK = "https://book.liteapi.travel/v3.0"

OPEN_STATUSES = ("collecting", "reconfirm")
SETTLING_STATUSES = ("verifying", "booking", "capturing", "unwinding", "paying", "reconciling")
DONE_STATUSES = ("booked", "partially_booked")
TERMINAL_STATUSES = ("cancelled", "failed", "resolved")
ACTIVE_ATTEMPT_STATUSES = ("pending", "authorized", "nothing_due", "releasing", "captured", "refunded")
COMMITTED_STATUSES = ("authorized", "nothing_due")   # counts toward "everyone has paid"
# The group's deadline runs only while somebody is committed: a round with none
# of these has no deadline, so it can never read "everyone must pay by …" over
# nobody, nor be expired for a clock that no longer protects anyone's hold.
DEADLINE_STATUSES = COMMITTED_STATUSES + ("captured",)
MAX_RECONCILE_ATTEMPTS = 3         # after this many automatic resumes a person takes over
CAPTURE_RETRIES = 3                # re-reads of a capture whose outcome Stripe did not confirm
PRICE_RISE_TOLERANCE_CENTS = 100   # above this per person the group must re-pay
ABSORB_CENTS_PER_PERSON = 100      # below this Rovyn absorbs a post-capture airline increase
HOLD_MIN_WINDOW_HOURS = 12         # a hold that would leave the group less than this is not worth taking
PRE_CAPTURE_MARGIN_HOURS = 48      # a refundable rate must stay free this long past the deadline
ESTIMATE_LINES = (
    ("visits", "Starred visits & sights"),
    ("events", "Starred events"),
    ("intercity_transport", "Trains & flights between cities"),
    ("meals_estimate", "Meals (estimate)"),
    ("transport_estimate", "Local transport (estimate)"),
)
# The checklist row of a hotel Rovyn booked ahead of capture and then cancelled
# because the round fell through: without a reason it would read as a bare
# "Not booked yet" and the group would not know its reservation is gone.
UNWOUND_HOTEL_NOTE = ("Rovyn's hotel booking was cancelled because the group checkout did not complete — "
                      "book it yourself or pay in Rovyn again.")


class BookingError(Exception):
    def __init__(self, message: str, code=None, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


class RefundUnresolved(Exception):
    """A refund could not be confirmed with Stripe; the settlement is parked in
    needs_attention with its refund cursor and the reconciler retries it."""


# ---------------------------------------------------------------------------
# Small helpers & configuration
# ---------------------------------------------------------------------------

def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat(timespec="seconds")


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cents(amount) -> int:
    try:
        return int(round(float(amount or 0) * 100))
    except (TypeError, ValueError):
        return 0


def dollars(c: int) -> str:
    return f"{int(c) / 100:.2f}"


def split_cents(total: int, n: int) -> list[int]:
    """Exact equal split: the first ``total % n`` people pay one cent more."""
    n = max(1, n)
    base, rem = divmod(int(total), n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def liteapi_book_mode() -> str:
    mode = (os.environ.get("LITEAPI_BOOK_MODE") or "off").strip().lower()
    return mode if mode in ("off", "sandbox", "live") else "off"


def liteapi_book_key() -> str:
    # Fail closed: never fall back to the search key.
    return (os.environ.get("LITEAPI_BOOK_API_KEY") or "").strip()


def hotel_booking_enabled() -> bool:
    return liteapi_book_mode() != "off" and bool(liteapi_book_key())


def service_fee_percent() -> float:
    try:
        return max(0.0, float(os.environ.get("ROVYN_SERVICE_FEE_PERCENT") or 0))
    except ValueError:
        return 0.0


def checkout_window_days() -> int:
    try:
        return max(1, min(int(os.environ.get("CHECKOUT_WINDOW_DAYS") or 5), stripe.AUTH_HOLD_DAYS))
    except ValueError:
        return 5


def app_public_url() -> str:
    return (os.environ.get("APP_PUBLIC_URL") or "http://localhost:5173").rstrip("/")


def money_mode_now() -> str:
    return stripe.money_mode_now()


def simulation_allowed() -> bool:
    """Simulated bookings exist for demos and test mode; never next to live money."""
    return money_mode_now() != "live"


def booking_modes() -> dict:
    return {
        "money": money_mode_now(),
        "payments": stripe.provider_mode(),
        "hotels": liteapi_book_mode() if hotel_booking_enabled() else ("simulated" if simulation_allowed() else "off"),
        "flights": duffel.duffel_mode() if duffel.duffel_available() else ("simulated" if simulation_allowed() else "off"),
    }


def settle_inline() -> bool:
    return (os.environ.get("ROVYN_SETTLE_INLINE") or "").strip() == "1"


# ---------------------------------------------------------------------------
# LiteAPI booking adapter (book.liteapi.travel, the booking key, net rates)
# ---------------------------------------------------------------------------

def _book_request(path: str, payload: dict | None = None, params: dict | None = None,
                  timeout: int = 30, base: str = _LITE_BOOK, method: str | None = None) -> dict:
    """One LiteAPI booking-side call: its own host, key and timeouts, one
    attempt only — anything unclear is resolved by reading back, not retrying."""
    key = liteapi_book_key()
    if not key:
        raise BookingError("LiteAPI booking is not configured (LITEAPI_BOOK_API_KEY).", "no_key")
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "Rovyn/1.0 (group trip planner)"},
        method=method or ("POST" if payload is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as res:
            raw = res.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            err = (json.loads(raw) or {}).get("error") or {}
        except json.JSONDecodeError:
            err = {}
        code = err.get("code")
        if exc.code >= 500:
            raise BookingError(f"LiteAPI HTTP {exc.code}", "network", exc.code) from None
        raise BookingError(err.get("description") or err.get("message") or f"LiteAPI HTTP {exc.code}",
                           code, exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BookingError(f"LiteAPI unreachable: {exc}", "network") from None


def _rate_identity(rate: dict) -> dict:
    policies = rate.get("cancellationPolicies") or {}
    # Lists, not tuples: the identity round-trips through JSON and must compare equal after.
    infos = sorted(
        [str(i.get("cancelTime") or ""), str(i.get("amount") or ""), str(i.get("type") or "")]
        for i in policies.get("cancelPolicyInfos") or [])
    return {"name": (rate.get("name") or "").strip(), "board_type": rate.get("boardType") or "",
            "refundable_tag": policies.get("refundableTag") or "", "policies": infos}


def _policy_deadline(policies: dict) -> str | None:
    """'Free cancellation until …' — the earliest moment a fee kicks in (ISO)."""
    times = []
    for info in (policies or {}).get("cancelPolicyInfos") or []:
        try:
            if float(info.get("amount") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        when = _parse_dt(info.get("cancelTime"))
        if when:
            times.append(when)
    return min(times).isoformat(timespec="seconds") if times else None


def hotel_rates(hotel_id: str, check_in: str, check_out: str, occupancies: list[dict]) -> list[dict]:
    """Every offer for the group's rooms at NET rates (``margin: 0``), each
    with the rate identity used to re-find it later (offer ids go stale)."""
    data = _book_request("/hotels/rates", payload={
        "hotelIds": [hotel_id], "occupancies": occupancies, "currency": "USD", "guestNationality": "US",
        "checkin": check_in, "checkout": check_out, "margin": 0, "maxRatesPerHotel": 8, "timeout": 10,
    }, base=_LITE_SEARCH, timeout=30)
    offers = []
    for entry in data.get("data") or []:
        if entry.get("hotelId") not in (None, hotel_id):
            continue
        for rt in entry.get("roomTypes") or []:
            amount = (rt.get("offerRetailRate") or {}).get("amount")
            rates = rt.get("rates") or []
            if not amount or not rates:
                continue
            refundable = all(((r.get("cancellationPolicies") or {}).get("refundableTag")) == "RFN" for r in rates)
            included = due_later = 0
            for r in rates:
                for t in ((r.get("retailRate") or {}).get("taxesAndFees") or []):
                    if t.get("included"):
                        included += cents(t.get("amount"))
                    else:
                        due_later += cents(t.get("amount"))
            deadlines = [d for d in (_policy_deadline(r.get("cancellationPolicies")) for r in rates) if d]
            offers.append({
                "offer_id": rt.get("offerId"), "total_cents": cents(amount),
                "currency": (rt.get("offerRetailRate") or {}).get("currency") or "USD",
                "refundable": refundable, "cancel_deadline": min(deadlines) if deadlines else None,
                "identity": [_rate_identity(r) for r in rates],
                "rate_name": (rates[0].get("name") or "").strip(), "board": (rates[0].get("boardName") or "").strip(),
                "taxes_included_cents": included, "due_at_property_cents": due_later,
                "sandbox": data.get("sandbox"),
            })
    return offers


def hotel_prebook(offer_id: str) -> dict:
    data = _book_request("/rates/prebook", payload={"offerId": offer_id, "usePaymentSdk": False},
                         params={"timeout": 30}, timeout=30)
    body = data.get("data") or {}
    if not body.get("prebookId"):
        raise BookingError("Prebook returned no id.", "prebook")
    return {
        "prebook_id": body["prebookId"], "total_cents": cents(body.get("price")),
        "currency": body.get("currency") or "USD",
        "clean": not (body.get("priceDifferencePercent") or body.get("cancellationChanged") or body.get("boardChanged")),
        "sandbox": data.get("sandbox"),
    }


def hotel_book(prebook_id: str, holder: dict, guests: list[dict], client_reference: str,
               custom_tags: dict | None = None) -> dict:
    payload = {"prebookId": prebook_id, "holder": holder, "guests": guests,
               "payment": {"method": "ACC_CREDIT_CARD"}, "clientReference": client_reference}
    if custom_tags:
        payload["customTags"] = custom_tags
    return _book_request("/rates/book", payload=payload, params={"timeout": 120}, timeout=130)


def hotel_get(booking_id: str) -> dict:
    return _book_request(f"/bookings/{booking_id}", params={"timeout": 4}, timeout=15)


def hotel_find_by_reference(client_reference: str) -> dict | None:
    today = date.today()
    data = _book_request("/bookings/", params={
        "bookingStartDate": (today - timedelta(days=1)).isoformat(),
        "bookingEndDate": (today + timedelta(days=1)).isoformat(),
    }, timeout=15)
    for row in data.get("data") or []:
        if (row.get("client_reference") or row.get("clientReference")) == client_reference:
            return row
    return None


def hotel_cancel(booking_id: str) -> dict:
    data = _book_request(f"/bookings/{booking_id}", payload={}, params={"timeout": 4}, timeout=20, method="PUT")
    body = data.get("data") or {}
    return {"status": (body.get("status") or "CANCELLED").upper(), "refund_cents": cents(body.get("refund_amount")),
            "fee_cents": cents(body.get("cancellation_fee")), "sandbox": data.get("sandbox")}


def _assert_usd(*quotes: dict) -> None:
    """Travelers are charged in USD cents; a rate quoted in anything else is never locked."""
    for q in quotes:
        cur = (q.get("currency") or "USD").upper()
        if cur != "USD":
            raise BookingError(f"this rate is priced in {cur}, not USD", "currency")


def _assert_sandbox_flag(payload: dict, money_mode: str, what: str) -> None:
    flag = payload.get("sandbox") if isinstance(payload, dict) else None
    if flag is None:
        return
    if bool(flag) != (money_mode != "live"):
        raise BookingError(f"{what} sandbox={flag} does not match settlement money mode {money_mode}", "mode_mismatch")


def sim_confirmation() -> str:
    return f"SIM-{secrets.token_hex(3).upper()}"


# ---------------------------------------------------------------------------
# Trip context, events, settlement rows
# ---------------------------------------------------------------------------

def _trip_context(trip_id: str) -> dict:
    real = ",".join("?" for _ in REAL_STATUSES)
    with get_conn(readonly=True) as conn:
        trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
        if trip is None:
            raise BookingError("Trip not found.", "not_found", 404)
        members, q_rows, profile_rows, self_rows = fetch_many(conn, [
            ("""SELECT u.id, u.name, u.email FROM trip_members t JOIN users u ON u.id = t.user_id
               WHERE t.trip_id = ? ORDER BY t.joined_at""", (trip_id,)),
            ("SELECT user_id, data FROM questionnaires WHERE trip_id = ?", (trip_id,)),
            ("""SELECT p.* FROM traveler_profiles p JOIN trip_members t ON t.user_id = p.user_id
               WHERE t.trip_id = ?""", (trip_id,)),
            # Items a traveler already booked by hand on the checklist — Rovyn must never buy them twice.
            (f"""SELECT b.item_key, u.name FROM bookings b LEFT JOIN users u ON u.id = b.booked_by
                WHERE b.trip_id = ? AND b.provider = 'self' AND b.status IN ({real})""", (trip_id, *REAL_STATUSES)),
        ])
    plan = json.loads(trip["final_plan"]) if trip["final_plan"] else {}
    return {
        "trip": dict(trip), "plan": plan, "members": [dict(m) for m in members],
        "cities": {r["user_id"]: (json.loads(r["data"]).get("departure_city") or "").strip() for r in q_rows},
        "profiles": {r["user_id"]: dict(r) for r in profile_rows},
        "self_booked": {r["item_key"]: r["name"] or "a traveler" for r in self_rows},
        "plan_hash": hashlib.sha256((trip["final_plan"] or "").encode()).hexdigest(),
    }


def _event(conn, settlement_id: str, step: str, message: str, item_key: str | None = None,
           level: str = "info", data=None) -> None:
    conn.execute(
        "INSERT INTO settlement_events (settlement_id, at, step, item_key, level, message, data_redacted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (settlement_id, _now(), step, item_key, level, message[:500], redacted_json(data) if data is not None else None))


def active_settlement(conn, trip_id: str):
    return conn.execute(
        "SELECT * FROM settlements WHERE trip_id = ? AND status NOT IN ('cancelled', 'failed', 'resolved') "
        "ORDER BY round DESC LIMIT 1", (trip_id,)).fetchone()


def _settlement(conn, settlement_id: str):
    return conn.execute("SELECT * FROM settlements WHERE id = ?", (settlement_id,)).fetchone()


def _cas(conn, settlement_id: str, new: str, expected: tuple) -> bool:
    marks = ",".join("?" for _ in expected)
    cur = conn.execute(
        f"UPDATE settlements SET status = ?, updated_at = ? WHERE id = ? AND status IN ({marks})",
        (new, _now(), settlement_id, *expected))
    return cur.rowcount == 1


def _save_lock(conn, settlement_id: str, lock: dict) -> None:
    conn.execute("UPDATE settlements SET lock = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(lock), _now(), settlement_id))


def _set_cursor(conn, settlement_id: str, cursor: dict | None) -> None:
    conn.execute("UPDATE settlements SET cursor = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(cursor) if cursor else None, _now(), settlement_id))


def _touch(conn, settlement_id: str) -> None:
    conn.execute("UPDATE settlements SET updated_at = ? WHERE id = ?", (_now(), settlement_id))


def _attempts(conn, settlement_id: str) -> dict:
    marks = ",".join("?" for _ in ACTIVE_ATTEMPT_STATUSES)
    return {r["user_id"]: dict(r) for r in conn.execute(
        f"SELECT * FROM payment_attempts WHERE settlement_id = ? AND status IN ({marks})",
        (settlement_id, *ACTIVE_ATTEMPT_STATUSES)).fetchall()}


def _sync_summary(conn, attempt: dict, receipt: dict | None = None) -> None:
    """``payments`` is the one-row-per-member summary everything else reads.
    ``receipt=None`` keeps whatever receipt the row already holds."""
    status = attempt["status"]
    if status in ("released", "failed", "expired"):
        conn.execute("DELETE FROM payments WHERE trip_id = ? AND user_id = ? AND mode = 'full'",
                     (attempt["trip_id"], attempt["user_id"]))
        return
    receipt_json = json.dumps(receipt) if receipt is not None else None
    conn.execute(
        """INSERT INTO payments (trip_id, user_id, amount_cents, currency, mode, receipt, email_status, paid_at,
                                 status, provider, current_attempt_id, captured_at, captured_cents, refunded_cents)
           VALUES (?, ?, ?, 'USD', 'full', COALESCE(?, '{}'), 'none', ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(trip_id, user_id) DO UPDATE SET
             amount_cents = excluded.amount_cents, receipt = COALESCE(?, payments.receipt), paid_at = excluded.paid_at,
             status = excluded.status, provider = excluded.provider, current_attempt_id = excluded.current_attempt_id,
             captured_at = excluded.captured_at, captured_cents = excluded.captured_cents,
             refunded_cents = excluded.refunded_cents""",
        (attempt["trip_id"], attempt["user_id"], attempt["amount_cents"], receipt_json, attempt["updated_at"], status,
         attempt["provider"], attempt["id"], attempt["updated_at"] if status in ("captured", "refunded") else None,
         attempt.get("captured_cents"), attempt.get("refunded_cents") or 0, receipt_json))


def _update_attempt(conn, attempt_id: str, **fields) -> dict:
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE payment_attempts SET {sets} WHERE id = ?", (*fields.values(), attempt_id))
    return dict(conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (attempt_id,)).fetchone())


def _receipt(ctx: dict, member: dict, check: dict, status: str, captured_cents: int | None = None) -> dict:
    trip, plan = ctx["trip"], ctx["plan"]
    return {
        "receipt_no": f"RV-{trip['id'][:8].upper()}-{member['id'][:4].upper()}",
        "trip_name": trip["name"], "destination": plan.get("destination", ""), "dates": plan.get("dates", {}),
        "items": [{"item_key": l["item_key"], "label": l["label"], "amount": round(l["cents"] / 100, 2),
                   "charged": l["charged"], "estimate": l["estimate"]} for l in check["lines"]],
        "total": round((captured_cents if captured_cents is not None else check["charged_cents"]) / 100, 2),
        "estimate_total": round(check["estimate_cents"] / 100, 2),
        "currency": "USD", "mode": "full", "status": status, "paid_at": _now(),
        "demo": money_mode_now() != "live", "confirmations": [],
    }


def _name_parts(user: dict, profile: dict | None) -> tuple[str, str]:
    if profile and profile.get("given_name"):
        return profile["given_name"], profile.get("family_name") or ""
    parts = (user.get("name") or "Traveler").split()
    return parts[0], (" ".join(parts[1:]) or parts[0])


# ---------------------------------------------------------------------------
# Verify & lock
# ---------------------------------------------------------------------------

def _occupancies_for(plan: dict, stay_key: str, group: int) -> list[dict]:
    """The rooming board's chosen mix when it exists, else two to a room."""
    rooming = (plan.get("rooming") or {}).get(stay_key) or {}
    rooms = ((rooming.get("mix") or {}).get("rooms")) or []
    occ = []
    for r in rooms:
        try:
            occ.extend([{"adults": int(r["sleeps"])}] * int(r.get("count") or 1))
        except (KeyError, TypeError, ValueError):
            occ = []
            break
    return occ or [{"adults": 2}] * max(1, (group + 1) // 2)


def _board_signature(f: dict) -> str:
    """The itinerary identity of a board flight option from its stored segments."""
    parts = []
    for row in (f.get("segments") or {}).get("out") or []:
        parts.append(_segment_part(row))
    for row in (f.get("segments") or {}).get("back") or []:
        parts.append(_segment_part(row))
    return "|".join(sorted(p for p in parts if p))


def _segment_part(row: dict) -> str:
    code = (row.get("flight_no") or "").replace(" ", "")
    depart = (row.get("depart") or "").replace(" ", "T")[:16]
    return f"{code}@{depart}" if code and depart else ""


def _slices_from_flight(f: dict) -> list[dict] | None:
    segs = f.get("segments") or {}
    out, back = segs.get("out") or [], segs.get("back") or []
    if not out:
        return None

    def code(row, which):
        parts = [p.strip() for p in (row.get("leg") or "").replace("→", ">").split(">")]
        return (parts[0] if which == 0 else parts[-1]) if len(parts) == 2 else ""

    slices = [{"origin": code(out[0], 0), "destination": code(out[-1], 1),
               "departure_date": (out[0].get("depart") or "")[:10]}]
    if back:
        slices.append({"origin": code(back[0], 0), "destination": code(back[-1], 1),
                       "departure_date": (back[0].get("depart") or "")[:10]})
    if any(not s["origin"] or not s["destination"] or not s["departure_date"] for s in slices):
        return None
    return slices


def _lock_flight(f: dict, travelers: list[str], money_mode: str, lock_time: datetime) -> dict:
    """A fresh Duffel offer for the board's itinerary with THIS group's
    passengers, and an airline hold when the fare allows one. Anything that
    cannot survive a group window becomes a self-book item, with the reason."""
    slices = _slices_from_flight(f)
    if not slices:
        return {"self": "This flight's itinerary isn't detailed enough to rebook — book it yourself"}
    request = duffel.search_offers(slices, len(travelers))
    offers = request.get("offers") or []
    wanted = _board_signature(f)
    same = [o for o in offers if duffel.itinerary_signature(o) == wanted]
    if not same:
        return {"self": "This exact itinerary is no longer on sale — book it yourself"}
    offer = min(same, key=lambda o: float(o.get("total_amount") or 0))
    duffel.assert_live_mode(offer, money_mode, "Duffel offer")
    if (offer.get("total_currency") or "USD") != "USD":
        return {"self": f"This fare is sold in {offer.get('total_currency')} — book it yourself"}
    reqs = offer.get("payment_requirements") or {}
    if reqs.get("requires_instant_payment"):
        return {"self": "This airline wants payment the moment it's booked — book it yourself"}
    required_by = _parse_dt(reqs.get("payment_required_by"))
    if required_by and required_by - timedelta(hours=2) < lock_time + timedelta(hours=HOLD_MIN_WINDOW_HOURS):
        hours = max(1, int((required_by - lock_time).total_seconds() // 3600))
        return {"self": f"This airline only holds fares for {hours} hours — book it yourself"}
    pax = {uid: p.get("id") for uid, p in zip(travelers, offer.get("passengers") or [])}
    # The higher of the equal shares: Rovyn may over-collect by (n − 1) cents, never under.
    per_person = split_cents(cents(offer.get("total_amount")), max(1, len(travelers)))[0]
    return {
        "offer": offer, "per_person_cents": per_person,
        "currency": "USD", "signature": duffel.itinerary_signature(offer),
        "ref": {"offer_id": offer.get("id"), "offer_request_id": request.get("id"), "passenger_ids": pax,
                "signature": duffel.itinerary_signature(offer), "expires_at": offer.get("expires_at"),
                "owner": (offer.get("owner") or {}).get("name"), "conditions": offer.get("conditions") or {},
                "requires_instant_payment": False, "orders": []},
        "operated_by": duffel.operating_carriers(offer),
    }


def _place_hold(ctx: dict, item: dict, settlement_id: str) -> dict:
    """Create the airline hold for a locked flight — no money moves."""
    passengers = _passengers_for(ctx, item)
    order = duffel.create_hold_order(item["ref"]["offer_id"], passengers,
                                     {"rovyn_trip_id": ctx["trip"]["id"][:40], "rovyn_item": item["item_key"][:40],
                                      "rovyn_attempt": item["attempt_ref"][:40], "rovyn_settlement": settlement_id[:40]})
    duffel.assert_live_mode(order, ctx.get("money_mode") or money_mode_now(), "Duffel order")
    status = order.get("payment_status") or {}
    return {"order_id": order.get("id"), "booking_reference": order.get("booking_reference"),
            "payment_required_by": status.get("payment_required_by"),
            "price_guarantee_expires_at": status.get("price_guarantee_expires_at"),
            "total_amount": order.get("total_amount"), "total_currency": order.get("total_currency"),
            "paid": False, "signature": duffel.itinerary_signature(order), "conditions": order.get("conditions") or {}}


def build_lock(ctx: dict, settlement_id: str, money_mode: str, prior: dict | None = None) -> dict:
    """Every checklist item priced by the provider that will book it, or marked
    as an estimate that is NOT charged. Deterministic and auditable — never a
    model. Provider calls happen here, so callers must not hold a connection."""
    trip, plan, members, cities = ctx["trip"], ctx["plan"], ctx["members"], ctx["cities"]
    group = max(1, len(members))
    lock_time = _now_dt()
    deadline_terms: list[str] = []
    items: list[dict] = []
    prior_items = {i["item_key"]: i for i in (prior or {}).get("items", [])}
    # What a traveler already booked by hand on the checklist stays theirs: it is
    # never priced, held or charged again (that would buy the same room twice).
    self_booked = ctx.get("self_booked") or {}

    def attempt_ref(key: str) -> str:
        return (prior_items.get(key) or {}).get("attempt_ref") or uuid.uuid4().hex

    checklist = checklist_from_plan(trip, group, _city_counter(cities))
    stays = [c for c in checklist if c["category"] == "stay"]
    multi_stay = len(stays) > 1
    for c in stays:
        stay, key = c["details"], c["item_key"]
        estimate_pp = cents(stay.get("total_per_person"))
        item = {
            "item_key": key, "category": "stay", "scope": "group", "title": c["title"],
            "provider": "self", "mode": None, "charge": False, "estimate": True,
            "total_cents": estimate_pp * group, "per_person_cents": estimate_pp, "currency": "USD",
            "estimate_cents": estimate_pp, "delta_cents": 0, "refundable": None, "cancel_deadline": None,
            "pre_capture_safe": False, "attempt_ref": attempt_ref(key),
            "ref": {"hotel_id": stay.get("liteapi_hotel_id")}, "note": None,
        }
        if key in self_booked:
            item["note"] = _already_booked_note(self_booked[key])
        elif hotel_booking_enabled() and stay.get("liteapi_hotel_id"):
            try:
                check_in = stay.get("check_in") or (plan.get("dates") or {}).get("start") or ""
                check_out = stay.get("check_out") or (plan.get("dates") or {}).get("end") or ""
                occupancies = _occupancies_for(plan, stay.get("city") or stay.get("name"), group)
                offers = hotel_rates(stay["liteapi_hotel_id"], check_in, check_out, occupancies)
                if not offers:
                    raise BookingError("No bookable rate for these dates.", "no_availability")
                _assert_sandbox_flag(offers[0], money_mode, "LiteAPI rates")
                offers.sort(key=lambda o: (not o["refundable"], o["total_cents"]))
                offer = offers[0]
                pre = hotel_prebook(offer["offer_id"])
                if not pre["clean"]:
                    offers = hotel_rates(stay["liteapi_hotel_id"], check_in, check_out, occupancies)
                    offers.sort(key=lambda o: (not o["refundable"], o["total_cents"]))
                    offer = offers[0]
                    pre = hotel_prebook(offer["offer_id"])
                    if not pre["clean"]:
                        raise BookingError("The hotel's price keeps changing between search and prebook.", "unstable")
                _assert_sandbox_flag(pre, money_mode, "LiteAPI prebook")
                _assert_usd(pre, offer)
                total = pre["total_cents"] or offer["total_cents"]
                deadline_dt = _parse_dt(offer["cancel_deadline"])
                safe = bool(offer["refundable"]) and (
                    deadline_dt is None or deadline_dt > lock_time + timedelta(days=checkout_window_days(), hours=PRE_CAPTURE_MARGIN_HOURS))
                if multi_stay and not safe:
                    item["note"] = "With more than one stay, only freely cancellable rates are booked by Rovyn — book this one yourself"
                else:
                    item.update({
                        "provider": "liteapi", "mode": liteapi_book_mode(), "charge": True, "estimate": False,
                        "total_cents": total, "per_person_cents": split_cents(total, group)[0], "currency": pre["currency"],
                        "delta_cents": total - estimate_pp * group, "refundable": offer["refundable"],
                        "cancel_deadline": offer["cancel_deadline"], "pre_capture_safe": safe,
                        "taxes_included_cents": offer["taxes_included_cents"],
                        "due_at_property_cents": offer["due_at_property_cents"],
                        "ref": {"hotel_id": stay["liteapi_hotel_id"], "check_in": check_in, "check_out": check_out,
                                "occupancies": occupancies, "identity": offer["identity"], "offer_id": offer["offer_id"],
                                "prebook_id": pre["prebook_id"], "rate_name": offer["rate_name"], "board": offer["board"],
                                "rooms": len(occupancies)},
                        "verified_at": _now(),
                    })
            except Exception as exc:
                item["note"] = f"Could not verify a live rate ({exc}) — book it yourself"
        elif simulation_allowed() and not hotel_booking_enabled():
            item.update({"provider": "simulated", "mode": "simulated", "charge": True, "pre_capture_safe": True})
        items.append(item)

    for c in (x for x in checklist if x["category"] == "flight"):
        f, key = c["details"], c["item_key"]
        city = key.split(":", 1)[1]
        travelers = [m["id"] for m in members if cities.get(m["id"], "").casefold() == city.strip().casefold()]
        estimate_pp = cents(f.get("est_price_per_person"))
        item = {
            "item_key": key, "category": "flight", "scope": c["scope"], "title": c["title"],
            "provider": "self", "mode": None, "charge": False, "estimate": True,
            "per_person_cents": estimate_pp, "total_cents": estimate_pp * len(travelers), "currency": "USD",
            "estimate_cents": estimate_pp, "delta_cents": 0, "travelers": travelers, "pre_capture_safe": True,
            "attempt_ref": attempt_ref(key), "ref": {"board_offer_id": f.get("duffel_offer_id")}, "note": None,
        }
        hinted = bool(f.get("duffel_offer_id") or f.get("duffel_offer_ids"))
        # An open jaw the board priced as two one-way tickets: the lock books
        # ONE Duffel offer per flight item, and re-searching the pair as one
        # ticket is bound to miss (no airline sold it as one — that's why it
        # was paired), so say what it really is instead of "no longer on sale".
        pair = bool(f.get("separate_tickets")) or (
            len(f.get("duffel_offer_ids") or []) > 1 and not f.get("duffel_offer_id"))
        if key in self_booked:
            item["note"] = _already_booked_note(self_booked[key])
        elif travelers and pair:
            item["note"] = "Priced as two separate one-way tickets — Rovyn can't book this pair yet, book both legs yourself"
        elif travelers and hinted and duffel.duffel_available():
            try:
                live = _lock_flight(f, travelers, money_mode, lock_time)
                if live.get("self"):
                    item["note"] = live["self"]
                else:
                    item.update({
                        "provider": "duffel", "mode": duffel.duffel_mode(), "charge": True, "estimate": False,
                        "per_person_cents": live["per_person_cents"], "currency": "USD",
                        "total_cents": live["per_person_cents"] * len(travelers),
                        "delta_cents": live["per_person_cents"] - estimate_pp, "ref": live["ref"],
                        "operated_by": live["operated_by"],
                        "refundable": bool(((live["ref"]["conditions"].get("refund_before_departure") or {}).get("allowed"))),
                        "verified_at": _now(),
                    })
            except Exception as exc:
                item["note"] = f"Could not verify this fare ({exc}) — book it yourself"
        elif travelers and simulation_allowed() and not duffel.duffel_available():
            item.update({"provider": "simulated", "mode": "simulated", "charge": True})
        elif travelers and not hinted:
            item["note"] = "No bookable fare for this flight yet — book it yourself"
        items.append(item)

    # Airline holds go in after every item exists, one per locked flight — as
    # soon as every passenger on that flight has given the details the airline
    # needs (see place_pending_holds, called when each traveler checks out).
    ctx["money_mode"] = money_mode
    for item in items:
        if item["provider"] == "duffel" and item["charge"]:
            _try_place_hold(ctx, item, settlement_id, deadline_terms)

    costs = plan.get("cost_per_person") or {}
    for key, label in ESTIMATE_LINES:
        c = cents(costs.get(key))
        if c > 0:
            items.append({"item_key": f"estimate:{key}", "category": "estimate", "scope": "group", "title": label,
                          "provider": "self", "mode": None, "charge": False, "estimate": True, "per_person_cents": c,
                          "total_cents": c * group, "currency": "USD", "estimate_cents": c, "delta_cents": 0, "note": None,
                          "pre_capture_safe": True, "attempt_ref": None})

    fee = service_fee_percent()
    return {
        "verified_at": _now(), "currency": "USD", "fee_percent": fee, "group_size": group, "money_mode": money_mode,
        "member_ids": sorted(m["id"] for m in members), "items": items,
        "members": member_checks(items, members, cities, fee),
        "needs_ack": any(i["charge"] and not i.get("pre_capture_safe") for i in items),
        "deadline_terms": deadline_terms,
    }


def _passengers_complete(ctx: dict, item: dict) -> bool:
    passengers = _passengers_for(ctx, item)
    if len(passengers) != len(item.get("travelers") or []):
        return False
    return all(p.get(f) for p in passengers for f in ("given_name", "family_name", "born_on", "gender", "title", "email", "phone_number"))


def _self_book(item: dict, note: str) -> None:
    item.update({"provider": "self", "mode": None, "charge": False, "estimate": True,
                 "per_person_cents": item["estimate_cents"], "total_cents": item["estimate_cents"] * len(item["travelers"]),
                 "note": note})
    item["ref"] = {"board_offer_id": (item.get("ref") or {}).get("board_offer_id")}
    item.pop("hold_pending", None)


def _already_booked_note(who: str) -> str:
    return f"Already booked by {who} — not charged by Rovyn"


def _drop_already_booked(item: dict, who: str, group: int) -> None:
    """A charged item a traveler recorded as booked by hand AFTER the prices
    were locked: it leaves the charge (back to its estimate — what that
    traveler pays the seller) and its airline hold, if any, is released.
    Makes provider calls, so callers must not hold a connection."""
    _release_holds({"items": [item]})
    note = _already_booked_note(who)
    if item["category"] == "flight":
        _self_book(item, note)
        return
    item.update({"provider": "self", "mode": None, "charge": False, "estimate": True, "pre_capture_safe": False,
                 "per_person_cents": item["estimate_cents"], "total_cents": item["estimate_cents"] * group,
                 "delta_cents": 0, "note": note})
    item["ref"] = {"hotel_id": (item.get("ref") or {}).get("hotel_id")}


def _adopt_live_fare(item: dict, live: dict) -> None:
    """A fresh offer for the same itinerary replaces the stale one (same attempt reference)."""
    item["ref"].update(live["ref"])
    item.update({
        "per_person_cents": live["per_person_cents"], "total_cents": live["per_person_cents"] * len(item["travelers"]),
        "delta_cents": live["per_person_cents"] - item["estimate_cents"], "operated_by": live["operated_by"],
        "refundable": bool(((live["ref"]["conditions"].get("refund_before_departure") or {}).get("allowed"))),
        "verified_at": _now(),
    })


def _plan_flight(ctx: dict, item: dict) -> dict | None:
    city = item["item_key"].split(":", 1)[1]
    flights = (ctx.get("plan") or {}).get("flights") or {}
    if city in flights:
        return flights[city]
    return next((f for c, f in flights.items() if c.strip().casefold() == city.strip().casefold()), None)


def _find_hold_by_attempt(attempt_ref: str) -> dict | None:
    """After a timeout on POST /air/orders: did the airline create our hold?
    Recent orders are read back and matched on the attempt reference we sent."""
    for order in duffel.list_recent_orders():
        if (order.get("metadata") or {}).get("rovyn_attempt") == attempt_ref:
            status = order.get("payment_status") or {}
            return {"order_id": order.get("id"), "booking_reference": order.get("booking_reference"),
                    "payment_required_by": status.get("payment_required_by"),
                    "price_guarantee_expires_at": status.get("price_guarantee_expires_at"),
                    "total_amount": order.get("total_amount"), "total_currency": order.get("total_currency"),
                    "paid": not status.get("awaiting_payment", True), "signature": duffel.itinerary_signature(order),
                    "conditions": order.get("conditions") or {}}
    return None


def _try_place_hold(ctx: dict, item: dict, settlement_id: str, deadline_terms: list) -> None:
    """Place the airline hold now, or mark it pending until every passenger
    on the flight has their details. A refused hold turns the flight into a
    self-book item with the airline's reason; an unanswered one is parked —
    never silently handed back to the traveler while an order may exist."""
    if not _passengers_complete(ctx, item):
        item["hold_pending"] = True
        item["note"] = "The airline hold is placed once every traveler on this flight adds their details"
        return
    try:
        if duffel.offer_expired(item["ref"]):
            # Offers live about 30 minutes: re-search the same itinerary for this group before holding it.
            f = _plan_flight(ctx, item)
            live = (_lock_flight(f, item["travelers"], ctx.get("money_mode") or money_mode_now(), _now_dt())
                    if f else {"self": "This flight is no longer in the plan — book it yourself"})
            if live.get("self"):
                _self_book(item, live["self"])
                return
            _adopt_live_fare(item, live)
        hold = _place_hold(ctx, item, settlement_id)
    except duffel.DuffelError as exc:
        if exc.code == "network":
            found = None
            try:
                found = _find_hold_by_attempt(item["attempt_ref"])
            except Exception:
                found = None
            if found is None:
                item["hold_pending"] = True
                item["note"] = "The airline did not answer — the hold will be retried"
                with get_conn() as conn:
                    _event(conn, settlement_id, "lock", f"{item['title']}: the airline did not answer while placing the hold — parked, not self-booked",
                           item_key=item["item_key"], level="warn")
                return
            hold = found
        else:
            _self_book(item, f"The airline would not hold this fare ({exc}) — book it yourself")
            return
    except Exception as exc:
        _self_book(item, f"The airline would not hold this fare ({exc}) — book it yourself")
        return
    item["ref"]["orders"] = [hold]
    item.pop("hold_pending", None)
    item["note"] = None
    due = _parse_dt(hold.get("payment_required_by"))
    if due:
        deadline_terms.append((due - timedelta(hours=2)).isoformat(timespec="seconds"))


def place_pending_holds(settlement_id: str) -> None:
    """Called when a traveler checks out: holds that were waiting for traveler
    details (or for the airline to answer) are placed now. A hold that fails,
    or a fare that rose by more than the tolerance, after others already paid
    changes the shares, so the round restarts (reconfirm)."""
    with get_conn(readonly=True) as conn:
        row = _settlement(conn, settlement_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return
        lock = json.loads(row["lock"])
        attempts = _attempts(conn, settlement_id)
    pending = [i for i in lock["items"] if i["provider"] == "duffel" and i["charge"] and i.get("hold_pending")]
    if not pending:
        return
    ctx = _trip_context(row["trip_id"])
    ctx["money_mode"] = row["money_mode"]
    terms = list(lock.get("deadline_terms") or [])
    changed, broke = False, []
    for item in pending:
        if not _passengers_complete(ctx, item):
            continue
        before = item["per_person_cents"]
        _try_place_hold(ctx, item, settlement_id, terms)
        changed = True
        if item["provider"] != "duffel":
            broke.append(f"{item['title']}: {item['note']}")
        elif not item.get("hold_pending") and item["per_person_cents"] > before + PRICE_RISE_TOLERANCE_CENTS:
            broke.append(f"{item['title']}: the fare rose by ${dollars(item['per_person_cents'] - before)} per person")
    if not changed:
        return
    lock["deadline_terms"] = terms
    lock["members"] = member_checks(lock["items"], ctx["members"], ctx["cities"], lock["fee_percent"])
    if broke and any(a["status"] in COMMITTED_STATUSES for a in attempts.values()):
        _go_reconfirm(settlement_id, lock, broke, row["status"])
        return
    with get_conn() as conn:
        _save_lock(conn, settlement_id, lock)
        _event(conn, settlement_id, "lock", "airline hold placed" if not broke else "; ".join(broke),
               level="warn" if broke else "info",
               data={"items": [{"item_key": i["item_key"], "provider": i["provider"], "note": i.get("note")} for i in pending]})
        recompute_deadline(conn, settlement_id)


def _city_counter(cities: dict):
    from collections import Counter
    return Counter(c.strip().casefold() for c in cities.values() if c and c.strip())


def member_checks(items: list[dict], members: list[dict], cities: dict, fee_percent: float) -> dict:
    """What each traveler owes, line by line: equal shares of group items,
    their own city's flight, estimates shown but never charged, the service
    fee (if any) inside the charged total. A member whose city has no priced
    flight sees exactly that — never a substituted average."""
    checks = {m["id"]: {"lines": [], "charged_cents": 0, "estimate_cents": 0, "total_cents": 0} for m in members}
    ids = [m["id"] for m in members]
    for item in items:
        if item.get("failed"):
            continue
        if item["category"] == "stay":
            for uid, share in zip(ids, split_cents(item["total_cents"], len(ids))):
                checks[uid]["lines"].append({
                    "item_key": item["item_key"], "label": f"{item['title']} (your equal share)", "cents": share,
                    "charged": item["charge"], "estimate": item["estimate"], "provider": item["provider"]})
        elif item["category"] == "flight":
            for uid in item.get("travelers") or []:
                checks[uid]["lines"].append({
                    "item_key": item["item_key"], "label": item["title"] + ("" if item["charge"] else " — not booked by Rovyn"),
                    "cents": item["per_person_cents"], "charged": item["charge"], "estimate": item["estimate"],
                    "provider": item["provider"]})
        else:
            for uid in ids:
                checks[uid]["lines"].append({
                    "item_key": item["item_key"], "label": item["title"], "cents": item["per_person_cents"],
                    "charged": False, "estimate": True, "provider": "self"})
    for uid, check in checks.items():
        charged = sum(l["cents"] for l in check["lines"] if l["charged"])
        if fee_percent > 0 and charged > 0:
            fee = int(round(charged * fee_percent / 100))
            check["lines"].append({"item_key": "fee", "label": "Rovyn service fee", "cents": fee,
                                   "charged": True, "estimate": False, "provider": "rovyn"})
            charged += fee
        check["charged_cents"] = charged
        check["total_cents"] = charged
        check["estimate_cents"] = sum(l["cents"] for l in check["lines"])
    return checks


# ---------------------------------------------------------------------------
# Settlement lifecycle: create, sessions, authorizations, release, abandon
# ---------------------------------------------------------------------------

def availability(conn, trip) -> tuple[bool, str | None]:
    if not trip["final_plan"]:
        return False, "There is no final plan yet."
    if trip["status"] not in ("approved", "booked"):
        return False, "Checkout opens once every traveler approves the plan."
    if conn.execute("SELECT 1 FROM payments WHERE trip_id = ? AND mode = 'self_book'", (trip["id"],)).fetchone():
        return False, "A traveler already chose to book this trip themselves — the group books item by item."
    return True, None


def ensure_settlement(trip_id: str) -> dict:
    """The one active settlement per trip — a fresh lock on first use, a new
    round after a terminal one. Provider calls run with no connection open."""
    with get_conn() as conn:
        row = active_settlement(conn, trip_id)
        if row is not None:
            return dict(row)
        last = conn.execute("SELECT MAX(round) r FROM settlements WHERE trip_id = ?", (trip_id,)).fetchone()["r"] or 0
    settlement_id = uuid.uuid4().hex
    money_mode = money_mode_now()
    ctx = _trip_context(trip_id)
    lock = build_lock(ctx, settlement_id, money_mode)
    now = _now()
    with get_conn() as conn:
        existing = active_settlement(conn, trip_id)
        if existing is not None:
            _release_holds(lock)  # somebody won the race — our holds lapse
            return dict(existing)
        try:
            conn.execute(
                """INSERT INTO settlements (id, trip_id, round, status, money_mode, currency, lock, plan_hash, cursor,
                                            deadline, created_at, updated_at)
                   VALUES (?, ?, ?, 'collecting', ?, 'USD', ?, ?, NULL, NULL, ?, ?)""",
                (settlement_id, trip_id, last + 1, money_mode, json.dumps(lock), ctx["plan_hash"], now, now))
        except IntegrityError:
            winner = dict(active_settlement(conn, trip_id))
            lost = True
        else:
            lost = False
        if lost:
            _release_holds(lock)  # the other traveler's lock won the insert — our holds lapse
            return winner
        _event(conn, settlement_id, "lock", "prices verified and locked",
               data={"items": [{"item_key": i["item_key"], "provider": i["provider"], "charge": i["charge"],
                                "total_cents": i["total_cents"], "pre_capture_safe": i.get("pre_capture_safe"),
                                "note": i.get("note")}
                               for i in lock["items"]], "money_mode": money_mode})
        return dict(_settlement(conn, settlement_id))


def _release_holds(lock: dict) -> None:
    for item in lock.get("items", []):
        for order in (item.get("ref") or {}).get("orders") or []:
            if order.get("order_id") and not order.get("paid"):
                try:
                    duffel.cancel_order(order["order_id"])
                except Exception:
                    pass  # an unpaid hold lapses on its own


def lock_prices(trip_id: str, user: dict) -> dict:
    """Create the settlement (verify-and-lock) without paying — so every
    traveler sees their live share before authorizing anything."""
    expire_if_due(trip_id)
    settlement = ensure_settlement(trip_id)
    return {"settlement_id": settlement["id"], "status": settlement["status"], "round": settlement["round"]}


def profile_requirements(lock: dict, user_id: str, profile: dict | None, email: str | None) -> dict:
    """Missing traveler fields, computed from the lock: names for any booking;
    the airline set when a Duffel item books this traveler."""
    on_flight = any(i["category"] == "flight" and i["provider"] == "duffel" and i["charge"]
                    and user_id in (i.get("travelers") or []) for i in lock["items"])
    fields = ["given_name", "family_name"] + (["born_on", "gender", "title", "phone"] if on_flight else [])
    profile = profile or {}
    missing = [f for f in fields if not (profile.get(f) or "").strip()]
    if on_flight and not email:
        missing.append("email")
    return {"fields": fields, "missing": missing, "flight": on_flight}


def recompute_deadline(conn, settlement_id: str) -> str | None:
    """The open round's deadline: min(first commitment + window, card holds −
    24 h, airline holds − 2 h). Called whenever a commitment appears or goes
    away, so it always follows whoever is still committed. With nobody
    committed it is NULL: a leftover deadline would tell the group "everyone
    must pay by …" over an empty round, and could expire it. A closed round
    keeps the deadline it closed with — its audit trail names it."""
    row = _settlement(conn, settlement_id)
    if row is None:
        return None
    if row["status"] not in OPEN_STATUSES:
        return row["deadline"]
    lock = json.loads(row["lock"])
    attempts = [a for a in _attempts(conn, settlement_id).values() if a["status"] in DEADLINE_STATUSES]
    if not attempts:
        if row["deadline"] is not None:
            conn.execute("UPDATE settlements SET deadline = NULL, updated_at = ? WHERE id = ?", (_now(), settlement_id))
        return None
    first = min(_parse_dt(a["updated_at"]) or _now_dt() for a in attempts)
    terms = [first + timedelta(days=checkout_window_days())]
    for a in attempts:
        cb = _parse_dt(a.get("capture_before"))
        if cb:
            terms.append(cb - timedelta(days=1))
    for t in lock.get("deadline_terms") or []:
        dt = _parse_dt(t)
        if dt:
            terms.append(dt)
    deadline = min(terms).isoformat(timespec="seconds")
    conn.execute("UPDATE settlements SET deadline = ?, updated_at = ? WHERE id = ?", (deadline, _now(), settlement_id))
    return deadline


def expire_if_due(trip_id: str) -> bool:
    """A group that missed its deadline is released. Called on the write
    paths and by the reconciler — never from the polled read. A round where
    nobody is committed is never failed on a deadline: no hold is left for the
    clock to protect, so a leftover deadline is cleared and the round (its
    locked prices, any open checkout links) carries on."""
    with get_conn() as conn:
        row = active_settlement(conn, trip_id)
        if row is None or row["status"] not in OPEN_STATUSES or not row["deadline"]:
            return False
        due = _parse_dt(row["deadline"])
        if due is None or _now_dt() < due:
            return False
        if not any(a["status"] in DEADLINE_STATUSES for a in _attempts(conn, row["id"]).values()):
            recompute_deadline(conn, row["id"])
            return False
        if not _cas(conn, row["id"], "failed", OPEN_STATUSES):
            return False
        attempts = _attempts(conn, row["id"])
        lock = json.loads(row["lock"])
        _event(conn, row["id"], "deadline", f"deadline {row['deadline']} passed — authorizations released", level="warn")
    for a in attempts.values():
        _release_attempt(row["id"], a, "deadline")
    _release_holds(lock)
    return True


def _create_pending_attempt(conn, settlement: dict, user_id: str, amount: int, provider: str) -> dict:
    attempt_id = uuid.uuid4().hex
    try:
        conn.execute(
            """INSERT INTO payment_attempts (id, trip_id, user_id, settlement_id, round, provider, checkout_session_id,
                                             payment_intent_id, status, amount_cents, capture_before, captured_cents,
                                             refunded_cents, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'pending', ?, NULL, NULL, 0, ?, ?)""",
            (attempt_id, settlement["trip_id"], user_id, settlement["id"], settlement["round"], provider, amount,
             _now(), _now()))
    except IntegrityError:
        blocker = conn.execute(
            "SELECT status FROM payment_attempts WHERE settlement_id = ? AND user_id = ? AND status = 'releasing'",
            (settlement["id"], user_id)).fetchone()
        if blocker:
            raise BookingError("Your previous authorization is still being released — try again in a few minutes.", "releasing", 409)
        raise BookingError("You already have a payment in progress — refresh.", "conflict", 409)
    attempt = dict(conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (attempt_id,)).fetchone())
    _sync_summary(conn, attempt)
    return attempt


def _authorize_attempt(conn, ctx: dict, settlement: dict, attempt: dict, provider_ref: str,
                       capture_before: str, session_id: str | None) -> dict:
    lock = json.loads(settlement["lock"])
    member = next((m for m in ctx["members"] if m["id"] == attempt["user_id"]), None)
    check = lock["members"].get(attempt["user_id"])
    attempt = _update_attempt(conn, attempt["id"], status="authorized", payment_intent_id=provider_ref,
                              checkout_session_id=session_id or attempt.get("checkout_session_id"),
                              capture_before=capture_before)
    _sync_summary(conn, attempt, receipt=_receipt(ctx, member, check, "authorized") if member and check else {})
    recompute_deadline(conn, settlement["id"])
    _event(conn, settlement["id"], "authorize", "share authorized", data={"user_id": attempt["user_id"],
           "amount_cents": attempt["amount_cents"], "provider": attempt["provider"], "capture_before": capture_before})
    return attempt


def start_session(trip_id: str, user: dict, acknowledge: bool = False) -> dict:
    """Create (or reuse) this traveler's Checkout Session — or, without a
    Stripe key, record a simulated authorization straight away. A traveler
    who owes nothing commits without any card, in every money mode."""
    expire_if_due(trip_id)
    settlement = ensure_settlement(trip_id)
    if settlement["status"] not in OPEN_STATUSES:
        raise BookingError(f"Checkout is {settlement['status'].replace('_', ' ')} — nothing to pay right now.", "closed", 409)
    lock = json.loads(settlement["lock"])
    check = lock["members"].get(user["id"])
    if check is None:
        raise BookingError("You joined after the prices were locked — ask the leader to restart the checkout.", "no_check", 400)
    ctx = _trip_context(trip_id)
    member = next((m for m in ctx["members"] if m["id"] == user["id"]), None)
    if member is None:
        raise BookingError("Not a member of this trip.", "forbidden", 403)
    reqs = profile_requirements(lock, user["id"], ctx["profiles"].get(user["id"]), member.get("email"))
    if reqs["missing"]:
        raise BookingError("Add your traveler details first: " + ", ".join(reqs["missing"]), "profile_required", 400)
    if lock.get("needs_ack") and not acknowledge:
        raise BookingError("This check includes a non-refundable item — tick the acknowledgment to continue.", "ack_required", 400)
    place_pending_holds(settlement["id"])
    with get_conn(readonly=True) as conn:
        settlement = dict(_settlement(conn, settlement["id"]))
    if settlement["status"] not in OPEN_STATUSES:
        raise BookingError("Checkout changed while you were checking out — refresh.", "closed", 409)
    lock = json.loads(settlement["lock"])
    check = lock["members"].get(user["id"]) or check

    with get_conn(readonly=True) as conn:
        mine = next((a for a in _attempts(conn, settlement["id"]).values() if a["user_id"] == user["id"]), None)
        releasing = conn.execute(
            "SELECT 1 FROM payment_attempts WHERE trip_id = ? AND user_id = ? AND status = 'releasing'",
            (trip_id, user["id"])).fetchone()
    if releasing:
        raise BookingError("Your previous authorization is still being released — try again in a few minutes.", "releasing", 409)
    if mine and mine["status"] in ("authorized", "nothing_due", "captured", "refunded"):
        raise BookingError("You've already paid your share — waiting on the others.", "already_authorized", 400)
    if mine and mine["status"] == "pending" and mine.get("checkout_session_id") and stripe.stripe_available():
        session = stripe.retrieve_session(mine["checkout_session_id"])
        stripe.assert_livemode(session, settlement["money_mode"], "Checkout Session")
        if session.get("status") == "open" and (session.get("expires_at") or 0) > _now_dt().timestamp() + 600:
            return {"url": session.get("url"), "session_id": session["id"], "expires_at": session.get("expires_at")}
        pi = stripe.payment_intent_of(session) if session.get("status") == "complete" else None
        if pi and pi.get("status") == "requires_capture":
            return _confirm_intent(trip_id, settlement, mine, pi, session["id"])
        if session.get("status") == "open":
            # Too close to its expiry to hand out again: close it at Stripe first, so it
            # can never complete behind the fresh one (it may have completed just now).
            try:
                stripe.expire_session(session["id"])
            except stripe.StripeError:
                session = stripe.retrieve_session(session["id"])
                pi = stripe.payment_intent_of(session) if session.get("status") == "complete" else None
                if pi and pi.get("status") == "requires_capture":
                    return _confirm_intent(trip_id, settlement, mine, pi, session["id"])
        with get_conn() as conn:
            mine = _update_attempt(conn, mine["id"], status="expired")
            _sync_summary(conn, mine)
            _event(conn, settlement["id"], "session", "stale checkout link expired", data={"user_id": user["id"]})
        mine = None
    elif mine and mine["status"] == "pending":  # a simulated or orphaned pending row
        with get_conn() as conn:
            mine = _update_attempt(conn, mine["id"], status="expired")
            _sync_summary(conn, mine)
        mine = None

    amount = check["charged_cents"]
    if amount <= 0:
        # Nothing to charge: a commitment with no card and no provider, in every money mode.
        with get_conn() as conn:
            attempt = _create_pending_attempt(conn, settlement, user["id"], 0, "none")
            attempt = _update_attempt(conn, attempt["id"], status="nothing_due")
            _sync_summary(conn, attempt)
            recompute_deadline(conn, settlement["id"])  # a $0 commitment starts the group's clock too
            _event(conn, settlement["id"], "authorize", "nothing due — committed without a card",
                   data={"user_id": user["id"], "amount_cents": 0})
        _maybe_settle(settlement["id"])
        return {"authorized": True, "nothing_to_charge": True, "simulated": False}
    if not stripe.stripe_available():
        auth = stripe.simulated_authorization(user["id"])
        with get_conn() as conn:
            attempt = _create_pending_attempt(conn, settlement, user["id"], amount, "simulated")
            _authorize_attempt(conn, ctx, settlement, attempt, auth["id"], auth["capture_before"], None)
        _maybe_settle(settlement["id"])
        return {"simulated": True, "authorized": True, "nothing_to_charge": False}

    with get_conn() as conn:
        attempt = _create_pending_attempt(conn, settlement, user["id"], amount, "stripe")
    base = app_public_url()
    try:
        session = stripe.create_checkout_session(
            amount_cents=amount, currency="USD", product_name=f"Your share of {ctx['trip']['name']}",
            description=f"{ctx['plan'].get('destination', '')} · {len(ctx['members'])} travelers · authorized now, charged when everyone has paid",
            success_url=f"{base}/trips/{trip_id}/final?tab=money&checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/trips/{trip_id}/final?tab=money&checkout=cancel",
            client_reference_id=attempt["id"], customer_email=user.get("email"),
            metadata={"trip_id": trip_id, "user_id": user["id"], "settlement_id": settlement["id"],
                      "attempt_id": attempt["id"], "round": settlement["round"]},
            statement_suffix="ROVYN TRIP",
        )
        stripe.assert_livemode(session, settlement["money_mode"], "Checkout Session")
    except Exception as exc:
        with get_conn() as conn:
            attempt = _update_attempt(conn, attempt["id"], status="failed")
            _sync_summary(conn, attempt)
            _event(conn, settlement["id"], "session", f"checkout session could not be created: {exc}", level="error",
                   data={"user_id": user["id"]})
        raise
    with get_conn() as conn:
        _update_attempt(conn, attempt["id"], checkout_session_id=session["id"])
        _event(conn, settlement["id"], "session", "checkout session created",
               data={"user_id": user["id"], "session_id": session["id"], "amount_cents": amount})
    return {"url": session.get("url"), "session_id": session["id"], "expires_at": session.get("expires_at")}


def _confirm_intent(trip_id: str, settlement: dict, attempt: dict, pi: dict, session_id: str | None) -> dict:
    """A PaymentIntent in requires_capture is an authorization — persist it.
    Idempotent: the webhook and the return page may both report it."""
    stripe.assert_livemode(pi, settlement["money_mode"], "PaymentIntent")
    if settlement["status"] not in OPEN_STATUSES:
        return {"state": "closed"}
    ctx = _trip_context(trip_id)
    with get_conn() as conn:
        fresh = dict(conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (attempt["id"],)).fetchone())
        if fresh["status"] != "pending":
            return {"state": fresh["status"]}
        _authorize_attempt(conn, ctx, settlement, fresh, pi["id"], stripe.capture_before_of(pi), session_id)
        _event(conn, settlement["id"], "authorize", "extended authorization status",
               data={"user_id": attempt["user_id"], "status": stripe.extended_auth_status(pi)})
    _maybe_settle(settlement["id"])
    return {"state": "authorized"}


def _release_stray(settlement_id: str, pi: dict, user_id: str) -> None:
    """A requires_capture PaymentIntent that no live attempt owns (a replaced
    or expired link completed late): cancel it so the card is not held."""
    try:
        stripe.cancel_payment_intent(pi["id"], "duplicate", idempotency_key=f"stray:{pi['id']}")
        outcome, level = "stray authorization released", "warn"
    except stripe.StripeError as exc:
        outcome, level = f"stray authorization could NOT be released: {exc}", "error"
    with get_conn() as conn:
        _event(conn, settlement_id, "authorize", outcome, level=level, data={"user_id": user_id, "pi": pi["id"]})


def confirm_session(trip_id: str, user: dict, session_id: str) -> dict:
    """Back from Stripe: find OUR attempt for that session, then trust only
    what Stripe says about it. The hold an attempt owns is never touched here —
    the webhook may have recorded it first, and the page may be reloaded."""
    expire_if_due(trip_id)
    if not stripe.stripe_available():
        return {"state": "simulated"}
    with get_conn(readonly=True) as conn:
        attempt = conn.execute("SELECT * FROM payment_attempts WHERE checkout_session_id = ?", (session_id,)).fetchone()
        settlement = _settlement(conn, attempt["settlement_id"]) if attempt else None
    if attempt is None or attempt["trip_id"] != trip_id or attempt["user_id"] != user["id"]:
        raise BookingError("That checkout session isn't yours.", "wrong_session", 400)
    attempt, settlement = dict(attempt), dict(settlement)
    session = stripe.retrieve_session(session_id)
    stripe.assert_livemode(session, settlement["money_mode"], "Checkout Session")
    if session.get("status") == "expired":
        with get_conn() as conn:
            if attempt["status"] == "pending":
                attempt = _update_attempt(conn, attempt["id"], status="expired")
                _sync_summary(conn, attempt)
        return {"state": "expired"}
    pi = stripe.payment_intent_of(session)
    if pi and pi.get("status") == "requires_capture":
        if attempt.get("payment_intent_id") == pi["id"]:
            return {"state": attempt["status"]}  # already recorded (webhook first, or a reload)
        if attempt["status"] == "pending" and settlement["status"] in OPEN_STATUSES:
            return _confirm_intent(trip_id, settlement, attempt, pi, session_id)
        if attempt["status"] in ("expired", "failed", "released") and settlement["status"] not in SETTLING_STATUSES:
            _release_stray(settlement["id"], pi, user["id"])
        return {"state": attempt["status"]}
    if attempt["status"] != "pending":
        return {"state": attempt["status"]}  # e.g. captured: the hold is gone because the trip is booked
    return {"state": "pending", "payment_status": session.get("payment_status")}


def _undo_at_stripe(attempt: dict, reason: str) -> None:
    """Cancel the hold (authorized) or close the hosted link (pending). A link
    that completed in the meantime leaves a hold, which is cancelled instead."""
    if attempt.get("payment_intent_id"):
        stripe.cancel_payment_intent(attempt["payment_intent_id"],
                                     "abandoned" if reason == "abandoned" else "requested_by_customer",
                                     idempotency_key=f"cancel:{attempt['payment_intent_id']}")
        return
    if not attempt.get("checkout_session_id"):
        return
    try:
        stripe.expire_session(attempt["checkout_session_id"])
    except stripe.StripeError as exc:
        session = stripe.retrieve_session(attempt["checkout_session_id"])
        pi = stripe.payment_intent_of(session) if session.get("status") == "complete" else None
        if pi and pi.get("status") == "requires_capture":
            stripe.cancel_payment_intent(pi["id"], "abandoned", idempotency_key=f"cancel:{pi['id']}")
        elif session.get("status") == "open":
            raise exc


def _release_attempt(settlement_id: str, attempt: dict, reason: str) -> None:
    """Undo one traveler's hold: CAS →releasing, undo at Stripe, then released
    (the summary row disappears so nothing reads as paid). If Stripe does not
    confirm, the row STAYS releasing: no new attempt can start for that
    traveler and the reconciler finishes it (resolve_releasing)."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE payment_attempts SET status = 'releasing', updated_at = ? WHERE id = ? AND status IN ('authorized', 'pending', 'nothing_due')",
                           (_now(), attempt["id"]))
        if cur.rowcount != 1:
            return
        # The commitment ends the moment its release starts (even one Stripe has
        # yet to confirm): the deadline now follows whoever is still committed,
        # or goes away with the last of them.
        recompute_deadline(conn, settlement_id)
    if attempt["provider"] == "stripe":
        try:
            _undo_at_stripe(attempt, reason)
        except stripe.StripeError as exc:
            with get_conn() as conn:
                _event(conn, settlement_id, "release", f"release not confirmed by Stripe ({exc}) — the reconciler will finish it",
                       level="error", data={"user_id": attempt["user_id"]})
            return
    with get_conn() as conn:
        fresh = _update_attempt(conn, attempt["id"], status="released")
        _sync_summary(conn, fresh)
        _event(conn, settlement_id, "release", f"authorization released ({reason})", data={"user_id": attempt["user_id"]})


def releasing_attempt_ids(stale_after_seconds: int) -> list[str]:
    cutoff = (_now_dt() - timedelta(seconds=stale_after_seconds)).isoformat(timespec="seconds")
    with get_conn(readonly=True) as conn:
        rows = conn.execute("SELECT id FROM payment_attempts WHERE status = 'releasing' AND updated_at < ?", (cutoff,)).fetchall()
    return [r["id"] for r in rows]


def resolve_releasing(attempt_id: str) -> dict:
    """Finish a release Stripe never confirmed, by reading the hold first:
    canceled → released; still held → cancel again (same key); captured → a person."""
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (attempt_id,)).fetchone()
    if row is None or row["status"] != "releasing":
        return {"attempt_id": attempt_id, "skipped": True}
    a = dict(row)
    if a["provider"] == "stripe":
        try:
            if a.get("payment_intent_id"):
                pi = stripe.retrieve_payment_intent(a["payment_intent_id"])
                if pi.get("status") == "succeeded":
                    with get_conn() as conn:
                        _event(conn, a["settlement_id"], "release", "the hold was CAPTURED while being released — needs a person",
                               level="error", data={"user_id": a["user_id"], "pi": pi["id"]})
                    _needs_attention(a["settlement_id"], "a traveler's card was charged while their authorization was being released", resumable=False)
                    return {"attempt_id": attempt_id, "needs_attention": True}
                if pi.get("status") == "requires_capture":
                    stripe.cancel_payment_intent(pi["id"], "requested_by_customer", idempotency_key=f"cancel:{pi['id']}")
            else:
                _undo_at_stripe(a, "reconciled")
        except stripe.StripeError as exc:
            with get_conn() as conn:
                _event(conn, a["settlement_id"], "release", f"release still not confirmed by Stripe ({exc})", level="error",
                       data={"user_id": a["user_id"]})
            return {"attempt_id": attempt_id, "retry": True}
    with get_conn() as conn:
        fresh = _update_attempt(conn, a["id"], status="released")
        _sync_summary(conn, fresh)
        _event(conn, a["settlement_id"], "release", "authorization released (reconciled)", data={"user_id": a["user_id"]})
    return {"attempt_id": attempt_id, "released": True}


def release_authorization(trip_id: str, user: dict) -> dict:
    with get_conn() as conn:
        settlement = active_settlement(conn, trip_id)
        if settlement is None or settlement["status"] not in OPEN_STATUSES:
            raise BookingError("Your payment can't be released at this stage.", "closed", 409)
        mine = next((a for a in _attempts(conn, settlement["id"]).values() if a["user_id"] == user["id"]), None)
    if mine is None:
        return {"released": False}
    _release_attempt(settlement["id"], mine, "released_by_traveler")
    return {"released": True}


def abandon(trip_id: str, user: dict) -> dict:
    with get_conn() as conn:
        settlement = active_settlement(conn, trip_id)
        if settlement is None:
            return {"abandoned": False}
        if settlement["status"] not in OPEN_STATUSES:
            raise BookingError("Booking is under way — cancel individual bookings instead.", "closed", 409)
        if not _cas(conn, settlement["id"], "cancelled", OPEN_STATUSES):
            raise BookingError("Checkout changed under you — refresh.", "closed", 409)
        attempts = _attempts(conn, settlement["id"])
        lock = json.loads(settlement["lock"])
        _event(conn, settlement["id"], "abandon", "checkout abandoned by the leader", data={"user_id": user["id"]})
        conn.execute("DELETE FROM payments WHERE trip_id = ? AND mode = 'full' AND status IN ('pending', 'nothing_due', 'released', 'failed', 'expired')",
                     (trip_id,))
    for a in attempts.values():
        _release_attempt(settlement["id"], a, "abandoned")
    _release_holds(lock)
    return {"abandoned": True}


def retry(trip_id: str, user: dict) -> dict:
    """After a failed round: a new settlement with fresh prices and holds."""
    with get_conn() as conn:
        row = active_settlement(conn, trip_id)
        if row is not None:
            raise BookingError("The current checkout is still active.", "closed", 409)
    settlement = ensure_settlement(trip_id)
    with get_conn() as conn:
        _event(conn, settlement["id"], "retry", "new round started", data={"user_id": user["id"]})
    return {"retried": True, "round": settlement["round"]}


def on_members_changed(trip_id: str, reason: str) -> None:
    """The group changed while collecting: every share changes. The new lock
    is built FIRST; only once it exists is the round moved to reconfirm and
    every hold released. If the new group cannot be priced, the round fails
    cleanly — a lock that still lists the old members is never left behind."""
    with get_conn(readonly=True) as conn:
        row = active_settlement(conn, trip_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return
    row = dict(row)
    ctx = _trip_context(trip_id)
    try:
        lock = build_lock(ctx, row["id"], row["money_mode"])
    except Exception as exc:
        with get_conn() as conn:
            _event(conn, row["id"], "reconfirm", f"group changed ({reason}) but the new prices could not be locked: {exc}", level="error")
        _fail_settlement(row["id"], f"group changed ({reason}) — the round must restart", OPEN_STATUSES, json.loads(row["lock"]))
        return
    with get_conn() as conn:
        if not _cas(conn, row["id"], "reconfirm", OPEN_STATUSES):
            moved = True
        else:
            moved = False
            attempts = _attempts(conn, row["id"])
            old_lock = json.loads(row["lock"])
            _save_lock(conn, row["id"], lock)
            conn.execute("UPDATE settlements SET deadline = NULL, plan_hash = ? WHERE id = ?", (ctx["plan_hash"], row["id"]))
            _event(conn, row["id"], "reconfirm", f"group changed: {reason}", level="warn")
    if moved:
        _release_holds(lock)  # the round closed under us — the fresh holds lapse
        return
    for a in attempts.values():
        _release_attempt(row["id"], a, "group_changed")
    _release_holds(old_lock)


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def _record_webhook(conn, provider: str, event_id: str, kind: str, body) -> bool:
    """INSERT … ON CONFLICT DO NOTHING; False when already processed."""
    cur = conn.execute(
        "INSERT INTO webhook_events (id, provider, type, received_at, processed_at, body_redacted) VALUES (?, ?, ?, ?, NULL, ?) "
        "ON CONFLICT(id) DO NOTHING", (event_id, provider, kind, _now(), redacted_json(body, 8000)))
    if cur.rowcount == 1:
        return True
    row = conn.execute("SELECT processed_at FROM webhook_events WHERE id = ?", (event_id,)).fetchone()
    return row is not None and row["processed_at"] is None  # recorded but never processed: try again


def _mark_webhook(conn, event_id: str) -> None:
    conn.execute("UPDATE webhook_events SET processed_at = ? WHERE id = ?", (_now(), event_id))


def handle_stripe_event(event: dict) -> dict:
    """Route by PaymentIntent / session id against payment_attempts; events
    for non-current attempts are logged and ignored; unroutable events stay
    as dead letters (processed_at NULL)."""
    event_id, kind = event.get("id") or "", event.get("type") or ""
    obj = ((event.get("data") or {}).get("object")) or {}
    with get_conn() as conn:
        if not _record_webhook(conn, "stripe", event_id, kind, obj):
            return {"handled": True, "duplicate": True}
        pi_id = obj.get("id") if (obj.get("object") == "payment_intent") else (
            obj.get("payment_intent") if isinstance(obj.get("payment_intent"), str) else (obj.get("payment_intent") or {}).get("id"))
        session_id = obj.get("id") if obj.get("object") == "checkout.session" else None
        attempt = None
        if session_id:
            attempt = conn.execute("SELECT * FROM payment_attempts WHERE checkout_session_id = ?", (session_id,)).fetchone()
        if attempt is None and pi_id:
            attempt = conn.execute("SELECT * FROM payment_attempts WHERE payment_intent_id = ?", (pi_id,)).fetchone()
        if attempt is None and (obj.get("metadata") or {}).get("attempt_id"):
            attempt = conn.execute("SELECT * FROM payment_attempts WHERE id = ?", ((obj.get("metadata") or {}).get("attempt_id"),)).fetchone()
        if attempt is None:
            return {"handled": False, "reason": "no matching attempt"}  # dead letter, still 200
        attempt = dict(attempt)
        settlement = dict(_settlement(conn, attempt["settlement_id"]))
    stripe.assert_livemode(obj, settlement["money_mode"], "Stripe event object")

    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded",
                "payment_intent.amount_capturable_updated"):
        pi = stripe.retrieve_payment_intent(pi_id) if pi_id else stripe.payment_intent_of(obj)
        if not (pi and pi.get("status") == "requires_capture"):
            result = {"state": attempt["status"], "ignored": "no hold to record"}
        elif attempt["status"] == "pending" and settlement["status"] in OPEN_STATUSES:
            result = _confirm_intent(settlement["trip_id"], settlement, attempt, pi, session_id or attempt.get("checkout_session_id"))
        elif attempt.get("payment_intent_id") == pi["id"]:
            result = {"state": attempt["status"], "ignored": "already recorded"}
        elif attempt["status"] in ("expired", "failed", "released") and settlement["status"] not in SETTLING_STATUSES:
            # A replaced or expired link completed late: the hold belongs to nobody — release it.
            _release_stray(settlement["id"], pi, attempt["user_id"])
            result = {"state": attempt["status"], "stray_released": True}
        else:
            result = {"state": attempt["status"], "ignored": "not a current pending attempt"}
    elif kind == "checkout.session.expired":
        with get_conn() as conn:
            if attempt["status"] == "pending":
                fresh = _update_attempt(conn, attempt["id"], status="expired")
                _sync_summary(conn, fresh)
        result = {"state": "expired"}
    elif kind in ("payment_intent.canceled", "payment_intent.payment_failed"):
        with get_conn() as conn:
            if attempt["status"] == "authorized" and settlement["status"] in OPEN_STATUSES:
                fresh = _update_attempt(conn, attempt["id"], status="failed")
                _sync_summary(conn, fresh)
                recompute_deadline(conn, settlement["id"])  # a lost hold is a commitment gone
                _event(conn, settlement["id"], "authorize", f"authorization lost ({kind})", level="warn",
                       data={"user_id": attempt["user_id"], "reason": obj.get("cancellation_reason")})
            else:
                _event(conn, settlement["id"], "webhook", f"{kind} for a non-current attempt — logged only",
                       data={"user_id": attempt["user_id"], "attempt_status": attempt["status"]})
        result = {"state": "noted"}
    else:
        result = {"ignored": kind}
    with get_conn() as conn:
        _mark_webhook(conn, event_id)
    return {"handled": True, **result}


def handle_liteapi_event(payload: dict) -> dict:
    """The hotel's confirmation code arrives after booking. We act only on
    what GET /bookings/{id} returns for a booking we made."""
    event_id, name = str(payload.get("event_id") or ""), payload.get("event_name") or ""
    # The envelope's request/response are JSON *strings*: parse them so the audit copy is
    # redacted field by field (holder, guests, phone, email) instead of stored verbatim.
    envelope = dict(payload)
    for field in ("request", "response"):
        value = payload.get(field)
        if isinstance(value, str):
            try:
                envelope[field] = json.loads(value)
            except json.JSONDecodeError:
                envelope.pop(field, None)
    response = envelope.get("response") if isinstance(envelope.get("response"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    booking_id = (data or {}).get("bookingId") or (data or {}).get("booking_id")
    with get_conn() as conn:
        if event_id and not _record_webhook(conn, "liteapi", event_id, name, envelope):
            return {"handled": True, "duplicate": True}
        row = conn.execute("SELECT * FROM bookings WHERE provider_booking_id = ? AND provider = 'liteapi'", (booking_id,)).fetchone() if booking_id else None
        if row is None:
            return {"handled": False, "reason": "unknown booking"}
        settlement = active_settlement(conn, row["trip_id"]) or conn.execute(
            "SELECT * FROM settlements WHERE trip_id = ? ORDER BY round DESC LIMIT 1", (row["trip_id"],)).fetchone()
        money_mode = settlement["money_mode"] if settlement else money_mode_now()
    if "sandbox" in payload and bool(payload.get("sandbox")) != (money_mode != "live"):
        raise BookingError("webhook sandbox flag does not match the settlement's money mode", "mode_mismatch", 400)
    fresh = hotel_get(booking_id)
    _assert_sandbox_flag(fresh, money_mode, "LiteAPI booking")
    body = fresh.get("data") or {}
    with get_conn() as conn:
        docs = json.loads(row["documents"] or "{}")
        code = (body.get("hotelConfirmationCode") or "").strip()
        if code and name in ("booking.book.hotelConfirmationNumber", "booking.book"):
            docs["hotel_confirmation_code"] = code
            conn.execute("UPDATE bookings SET confirmation = ?, status = 'booked', documents = ? WHERE trip_id = ? AND item_key = ?",
                         (code, json.dumps(docs), row["trip_id"], row["item_key"]))
        if name == "booking.checkinInstruction" and body.get("checkinInstructions"):
            docs["check_in_instructions"] = body.get("checkinInstructions")
            conn.execute("UPDATE bookings SET documents = ? WHERE trip_id = ? AND item_key = ?",
                         (json.dumps(docs), row["trip_id"], row["item_key"]))
        if settlement:
            _event(conn, settlement["id"], "webhook", f"liteapi {name}", item_key=row["item_key"],
                   data={"booking_id": booking_id, "status": body.get("status")})
        if event_id:
            _mark_webhook(conn, event_id)
    return {"handled": True}


def poll_hotel_confirmations(trip_id: str) -> int:
    """Fallback for the webhook: fill pending hotel codes from GET /bookings/{id}."""
    filled = 0
    with get_conn(readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bookings WHERE trip_id = ? AND provider = 'liteapi' AND status = 'pending_confirmation'", (trip_id,)).fetchall()]
    for row in rows:
        try:
            body = hotel_get(row["provider_booking_id"]).get("data") or {}
        except BookingError:
            continue
        code = (body.get("hotelConfirmationCode") or "").strip()
        if code:
            with get_conn() as conn:
                docs = json.loads(row["documents"] or "{}")
                docs["hotel_confirmation_code"] = code
                conn.execute("UPDATE bookings SET confirmation = ?, status = 'booked', documents = ? WHERE trip_id = ? AND item_key = ?",
                             (code, json.dumps(docs), trip_id, row["item_key"]))
            filled += 1
    return filled


# ---------------------------------------------------------------------------
# Settlement: preflight → re-verify → book → capture → book → pay → confirm
# ---------------------------------------------------------------------------

def _maybe_settle(settlement_id: str) -> bool:
    with get_conn() as conn:
        row = _settlement(conn, settlement_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return False
        total = conn.execute("SELECT COUNT(*) c FROM trip_members WHERE trip_id = ?", (row["trip_id"],)).fetchone()["c"]
        done = conn.execute(
            "SELECT COUNT(*) c FROM payment_attempts WHERE settlement_id = ? AND status IN ('authorized', 'nothing_due')",
            (settlement_id,)).fetchone()["c"]
        if total == 0 or done < total:
            return False
        if not _cas(conn, settlement_id, "verifying", OPEN_STATUSES):
            return False
        _event(conn, settlement_id, "collect", f"all {total} travelers authorized — settling")
    spawn_settle(settlement_id)
    return True


_live_threads: set = set()


def _run_settle(settlement_id: str) -> None:
    """The thread boundary: an unexpected error is recorded and parks the
    round in needs_attention instead of dying silently in a daemon thread."""
    try:
        settle(settlement_id)
    except Exception as exc:
        try:
            with get_conn() as conn:
                _event(conn, settlement_id, "error", f"settlement stopped on an error: {type(exc).__name__}: {str(exc)[:300]}", level="error")
            _needs_attention(settlement_id, f"settlement stopped on an error: {str(exc)[:200]}")
        except Exception:
            pass
    finally:
        _live_threads.discard(threading.current_thread())


def spawn_settle(settlement_id: str):
    if settle_inline():
        _run_settle(settlement_id)
        return None
    thread = threading.Thread(target=_run_settle, args=(settlement_id,), daemon=False, name=f"settle-{settlement_id[:8]}")
    _live_threads.add(thread)
    thread.start()
    return thread


def join_live_settlements(timeout: float = 130.0) -> int:
    """At shutdown: let in-flight settlements finish their current provider call."""
    deadline = time.monotonic() + timeout
    for thread in list(_live_threads):
        thread.join(max(0.0, deadline - time.monotonic()))
    return len(_live_threads)


def _call(settlement_id: str, step: str, item_key: str | None, attempt_ref: str | None, fn, user_id: str | None = None,
          idempotency_key: str | None = None):
    """Bracket a provider call with a persisted cursor (no connection held
    across the call). Returns (result, error)."""
    with get_conn() as conn:
        _set_cursor(conn, settlement_id, {"step": step, "item_key": item_key, "attempt_ref": attempt_ref,
                                          "user_id": user_id, "idempotency_key": idempotency_key, "started_at": _now()})
    try:
        return fn(), None
    except Exception as exc:  # the caller decides; the cursor stays until it does
        return None, exc


def _clear_cursor(settlement_id: str) -> None:
    with get_conn() as conn:
        _set_cursor(conn, settlement_id, None)


def _fail_settlement(settlement_id: str, reason: str, from_states: tuple, lock: dict | None = None) -> None:
    """Release everything that is still releasable, then mark failed."""
    with get_conn() as conn:
        if not _cas(conn, settlement_id, "failed", from_states):
            return
        _set_cursor(conn, settlement_id, None)
        attempts = _attempts(conn, settlement_id)
        trip_id = _settlement(conn, settlement_id)["trip_id"]
        _event(conn, settlement_id, "fail", reason, level="error")
    for a in attempts.values():
        if a["status"] in ("authorized", "pending", "nothing_due"):
            _release_attempt(settlement_id, a, "settlement_failed")
    with get_conn() as conn:
        # Nothing reads as paid after a failed round: refunded/released summaries go.
        conn.execute("DELETE FROM payments WHERE trip_id = ? AND mode = 'full' AND status IN ('pending', 'nothing_due', 'released', 'failed', 'expired', 'refunded')",
                     (trip_id,))
    if lock:
        _release_holds(lock)


def _needs_attention(settlement_id: str, reason: str, ctx: dict | None = None, resume_to: str | None = None,
                     resumable: bool = True) -> None:
    """Park the round for a person, keeping the in-flight cursor. While the
    cursor carries a ``resume_to`` phase the reconciler keeps trying to
    resolve it by reading the providers (bounded by MAX_RECONCILE_ATTEMPTS);
    without one the round waits for an operator (backend/scripts/settle_resolve.py)."""
    with get_conn() as conn:
        row = _settlement(conn, settlement_id)
        if row is None or row["status"] in TERMINAL_STATUSES or row["status"] == "needs_attention":
            return
        cursor = json.loads(row["cursor"]) if row["cursor"] else {}
        target = resume_to or cursor.get("resume_to") or row["status"]
        if resumable and target in SETTLING_STATUSES + DONE_STATUSES and target != "reconciling":
            cursor.setdefault("step", "paused")
            cursor["resume_to"] = target
        else:
            cursor.pop("resume_to", None)
        conn.execute("UPDATE settlements SET status = 'needs_attention', cursor = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(cursor) if cursor else None, _now(), settlement_id))
        _event(conn, settlement_id, "attention", reason, level="error")
        trip_id = row["trip_id"]
    try:
        ctx = ctx or _trip_context(trip_id)
        leader = next((m for m in ctx["members"] if m["id"] == ctx["trip"]["leader_id"]), None)
        if leader:
            email_service.send_email(leader["email"], f"Rovyn needs a hand with {ctx['trip']['name']}",
                                     email_service.attention_html(ctx["trip"]["name"], reason))
    except Exception:
        pass


def _preflight(settlement: dict, lock: dict, ctx: dict, attempts: dict, stage: str) -> str | None:
    """Everything that must be true before money or reservations move."""
    if ctx["trip"]["status"] != "approved":
        return f"trip status is {ctx['trip']['status']}, not approved"
    if settlement["plan_hash"] != ctx["plan_hash"]:
        return "the plan changed after prices were locked"
    if sorted(m["id"] for m in ctx["members"]) != lock["member_ids"]:
        return "the group changed after prices were locked"
    if settlement["money_mode"] != money_mode_now():
        return f"money mode changed ({settlement['money_mode']} → {money_mode_now()})"
    for item in lock["items"]:
        if item["charge"] and item["mode"] == "live" and settlement["money_mode"] != "live":
            return f"{item['item_key']} is a live booking inside a {settlement['money_mode']} settlement"
        if item["charge"] and item["provider"] == "simulated" and settlement["money_mode"] == "live":
            return f"{item['item_key']} is simulated inside a live settlement"
    charged_items = sum(i["total_cents"] for i in lock["items"] if i["charge"] and not i.get("failed"))
    fee_total = sum(sum(l["cents"] for l in c["lines"] if l["item_key"] == "fee") for c in lock["members"].values())
    member_total = sum(c["charged_cents"] for c in lock["members"].values())
    if member_total != charged_items + fee_total:
        return f"ledger mismatch: members {member_total} vs items {charged_items} + fees {fee_total}"
    for m in ctx["members"]:
        a = attempts.get(m["id"])
        if a is None or a["status"] not in ("authorized", "captured", "nothing_due"):
            return f"{m['name']} has no authorization on file"
        if a["status"] == "nothing_due" or (a["provider"] == "none" and a["amount_cents"] == 0):
            if (lock["members"].get(m["id"]) or {}).get("charged_cents", 0) > 0:
                return f"{m['name']} owes money but committed with nothing due"
            continue
        if a["provider"] == "stripe" and a["status"] == "authorized":
            pi = stripe.retrieve_payment_intent(a["payment_intent_id"])
            stripe.assert_livemode(pi, settlement["money_mode"], "PaymentIntent")
            if pi.get("status") != "requires_capture":
                return f"{m['name']}'s card hold is {pi.get('status')}"
            if int(pi.get("amount_capturable") or 0) != a["amount_cents"]:
                return f"{m['name']}'s hold is {pi.get('amount_capturable')} cents, expected {a['amount_cents']}"
            cb = _parse_dt(stripe.capture_before_of(pi))
            if cb and cb < _now_dt() + timedelta(hours=3):
                return f"{m['name']}'s card hold expires too soon ({cb.isoformat()})"
        elif a["provider"] == "simulated" and settlement["money_mode"] != "simulated":
            return f"{m['name']} has a simulated authorization inside a {settlement['money_mode']} settlement"
    return None


def _reverify(ctx: dict, settlement: dict, lock: dict) -> tuple[dict, list[str]]:
    """Fresh prices for everything charged. Returns (lock, problems)."""
    money_mode = settlement["money_mode"]
    problems = []
    self_booked = ctx.get("self_booked") or {}
    for item in lock["items"]:
        if not item["charge"] or item.get("failed") or item.get("booking"):
            continue
        if item["item_key"] in self_booked:
            # A traveler recorded it as booked by hand after the lock (from before the
            # checklist refused that, or a race with it): Rovyn must not buy it a second
            # time. It leaves the charge, so every share falls and the capture takes
            # the lower amount — never a reason to make the group pay again.
            _drop_already_booked(item, self_booked[item["item_key"]], lock["group_size"])
            with get_conn() as conn:
                _event(conn, settlement["id"], "verify", f"{item['title']}: already booked on the checklist — no longer charged",
                       item_key=item["item_key"], level="warn")
            continue
        prev = item["per_person_cents"]
        try:
            if item["provider"] == "liteapi":
                ref = item["ref"]
                offers = hotel_rates(ref["hotel_id"], ref["check_in"], ref["check_out"], ref["occupancies"])
                same = [o for o in offers if o["identity"] == ref["identity"]]
                if not same:
                    problems.append(f"{item['title']}: that rate is no longer on sale")
                    continue
                offer = min(same, key=lambda o: o["total_cents"])
                _assert_sandbox_flag(offer, money_mode, "LiteAPI rates")
                pre = hotel_prebook(offer["offer_id"])
                if not pre["clean"]:
                    problems.append(f"{item['title']}: the price moved between search and prebook")
                    continue
                _assert_sandbox_flag(pre, money_mode, "LiteAPI prebook")
                _assert_usd(pre, offer)
                total = pre["total_cents"] or offer["total_cents"]
                ref.update({"offer_id": offer["offer_id"], "prebook_id": pre["prebook_id"]})
                item["previous_cents"] = prev
                item["total_cents"] = total
                item["per_person_cents"] = split_cents(total, lock["group_size"])[0]
                item["cancel_deadline"] = offer["cancel_deadline"]
                item["verified_at"] = _now()
            elif item["provider"] == "duffel":
                for order in item["ref"].get("orders") or []:
                    fresh = duffel.get_order(order["order_id"])
                    duffel.assert_live_mode(fresh, money_mode, "Duffel order")
                    if duffel.itinerary_signature(fresh) != order.get("signature"):
                        problems.append(f"{item['title']}: the airline changed the itinerary")
                        continue
                    if (fresh.get("conditions") or {}) != (order.get("conditions") or {}):
                        problems.append(f"{item['title']}: the fare conditions changed")
                        continue
                    status = fresh.get("payment_status") or {}
                    guarantee = _parse_dt(status.get("price_guarantee_expires_at"))  # null = no guarantee given, not expired
                    required = _parse_dt(status.get("payment_required_by"))
                    soon = _now_dt() + timedelta(minutes=15)
                    if (guarantee is not None and guarantee < soon) or (required is not None and required < soon):
                        problems.append(f"{item['title']}: the airline's price guarantee ran out")
                        continue
                    new_total = cents(fresh.get("total_amount"))
                    new_pp = split_cents(new_total, max(1, len(item["travelers"])))[0]
                    if new_pp > item["per_person_cents"] + PRICE_RISE_TOLERANCE_CENTS:
                        problems.append(f"{item['title']}: the fare rose by ${dollars(new_pp - item['per_person_cents'])} per person")
                        continue
                    order["total_amount"] = fresh.get("total_amount")
                    item["previous_cents"] = prev
                    item["per_person_cents"] = min(item["per_person_cents"], new_pp)
                    item["total_cents"] = item["per_person_cents"] * len(item["travelers"])
                    item["verified_at"] = _now()
        except Exception as exc:
            problems.append(f"{item['title']}: could not be re-verified ({exc})")
    lock["members"] = member_checks(lock["items"], ctx["members"], ctx["cities"], lock["fee_percent"])
    return lock, problems


def _go_reconfirm(settlement_id: str, lock: dict, reasons: list[str], from_state: str) -> None:
    """Prices moved: release every hold and ask the group to pay again at
    the new totals (Stripe cannot raise an existing authorization)."""
    with get_conn() as conn:
        if not _cas(conn, settlement_id, "reconfirm", (from_state,)):
            return
        attempts = _attempts(conn, settlement_id)
        _set_cursor(conn, settlement_id, None)
        _event(conn, settlement_id, "reconfirm", "; ".join(reasons)[:500], level="warn")
    for a in attempts.values():
        _release_attempt(settlement_id, a, "price_changed")
    _release_holds(lock)
    ctx = _trip_context(lock_trip_id(settlement_id))
    fresh = build_lock(ctx, settlement_id, money_mode_now(), prior=lock)
    for item in fresh["items"]:
        old = next((i for i in lock["items"] if i["item_key"] == item["item_key"]), None)
        if old:  # what the group had agreed to pay, before this re-verification moved it
            item["previous_cents"] = old.get("previous_cents", old.get("per_person_cents"))
    with get_conn() as conn:
        _save_lock(conn, settlement_id, fresh)
        conn.execute("UPDATE settlements SET deadline = NULL, plan_hash = ? WHERE id = ?", (ctx["plan_hash"], settlement_id))


def lock_trip_id(settlement_id: str) -> str:
    with get_conn(readonly=True) as conn:
        return _settlement(conn, settlement_id)["trip_id"]


def _holder_and_guests(ctx: dict, item: dict) -> tuple[dict, list[dict]]:
    trip, members, profiles = ctx["trip"], ctx["members"], ctx["profiles"]
    leader = next((m for m in members if m["id"] == trip["leader_id"]), members[0])
    first, last = _name_parts(leader, profiles.get(leader["id"]))
    holder = {"firstName": first, "lastName": last, "email": leader["email"],
              "phone": (profiles.get(leader["id"]) or {}).get("phone") or "+10000000000"}
    occupancies = item["ref"].get("occupancies") or [{"adults": 2}] * max(1, (len(members) + 1) // 2)
    guests, cursor = [], 0
    for n, occ in enumerate(occupancies):
        m = members[min(cursor, len(members) - 1)]
        gf, gl = _name_parts(m, profiles.get(m["id"]))
        guests.append({"occupancyNumber": n + 1, "firstName": gf, "lastName": gl, "email": m["email"]})
        cursor += int(occ.get("adults") or 1)
    return holder, guests


def _passengers_for(ctx: dict, item: dict) -> list[dict]:
    out = []
    for uid, pid in (item["ref"].get("passenger_ids") or {}).items():
        member = next((m for m in ctx["members"] if m["id"] == uid), None)
        if member is None:
            continue
        out.append(duffel.passenger_payload(pid, ctx["profiles"].get(uid) or {}, member["email"]))
    return out


def _dry_validate(ctx: dict, lock: dict) -> str | None:
    """Build every supplier payload locally and refuse to start on a hole."""
    for item in lock["items"]:
        if not item["charge"] or item.get("failed"):
            continue
        if item["provider"] == "liteapi":
            holder, guests = _holder_and_guests(ctx, item)
            if not holder["firstName"] or not holder["lastName"] or not holder["email"]:
                return f"{item['title']}: the hotel holder is missing a name or email"
            for g in guests:
                if not g["firstName"] or not g["lastName"]:
                    return f"{item['title']}: a guest is missing a name"
            if not item["ref"].get("prebook_id"):
                return f"{item['title']}: no prebook on file"
        elif item["provider"] == "duffel":
            for p in _passengers_for(ctx, item):
                for field in ("given_name", "family_name", "born_on", "gender", "title", "email", "phone_number"):
                    if not p.get(field):
                        return f"{item['title']}: a passenger is missing {field}"
            if not item["ref"].get("orders"):
                return f"{item['title']}: no airline hold on file (a traveler's details are missing)"
    return None


def _write_booking(conn, trip_id: str, item: dict, result: dict) -> bool:
    """UPDATE the checklist row the sync created; never INSERT from here.
    ``payer_id`` names the one traveler a single-passenger flight was bought
    for; group items (and multi-passenger flights) leave it NULL — the lock's
    traveler list decides who may cancel (see _may_cancel)."""
    travelers = item.get("travelers") or []
    payer = travelers[0] if item["category"] == "flight" and len(travelers) == 1 else None
    cur = conn.execute(
        """UPDATE bookings SET status = ?, provider = ?, confirmation = ?, provider_booking_id = ?, documents = ?, raw = ?,
           amount_cents = ?, payer_id = ?, booked_at = ?, booked_by = NULL, note = NULL, stale = 0, cancelled_at = NULL
           WHERE trip_id = ? AND item_key = ?""",
        (result.get("status", "booked"), item["provider"], result.get("confirmation"), result.get("provider_booking_id"),
         json.dumps(result.get("documents") or {}), redacted_json(result.get("raw") or {}), item["total_cents"], payer,
         _now(), trip_id, item["item_key"]))
    return cur.rowcount == 1


def _book_hotel(ctx: dict, item: dict) -> dict:
    holder, guests = _holder_and_guests(ctx, item)
    data = hotel_book(item["ref"]["prebook_id"], holder, guests, item["attempt_ref"],
                      custom_tags={"TRIP_ID": ctx["trip"]["id"][:30]})
    return _hotel_result(data, ctx.get("money_mode") or money_mode_now())


def _hotel_result(data: dict, money_mode: str) -> dict:
    _assert_sandbox_flag(data, money_mode, "LiteAPI booking")
    body = data.get("data") or data
    if (body.get("status") or "").upper() != "CONFIRMED":
        raise BookingError(f"Hotel booking came back {body.get('status') or 'unconfirmed'}.", "unconfirmed", 410)
    code = (body.get("hotelConfirmationCode") or "").strip()
    return {"provider_booking_id": body["bookingId"], "confirmation": code or None,
            "status": "booked" if code else "pending_confirmation",
            "documents": {"booking_id": body["bookingId"], "hotel_confirmation_code": code or None,
                          "supplier_booking_id": body.get("supplierBookingId"),
                          "cancellation_policies": body.get("cancellationPolicies"),
                          "last_free_cancellation": body.get("lastFreeCancellationDate"),
                          "check_in": body.get("checkin"), "check_out": body.get("checkout"),
                          "rooms": [{"room": (r.get("roomType") or {}).get("name") or r.get("name"), "board": r.get("boardName")}
                                    for r in body.get("bookedRooms") or []]},
            "raw": {"bookingId": body["bookingId"], "status": body.get("status"), "price": body.get("price"),
                    "currency": body.get("currency"), "supplierBookingId": body.get("supplierBookingId"),
                    "sandbox": data.get("sandbox")}}


def _resolve_hotel_booking(ctx: dict, item: dict) -> dict | None:
    """After a timeout: did the booking go through? Re-POST with the SAME
    clientReference (4005 = exists), then read it back. None = still unknown."""
    try:
        data = hotel_book(item["ref"]["prebook_id"], *_holder_and_guests(ctx, item), item["attempt_ref"],
                          custom_tags={"TRIP_ID": ctx["trip"]["id"][:30]})
        return _hotel_result(data, ctx.get("money_mode") or money_mode_now())
    except BookingError as exc:
        if exc.code == "network":
            return None
        if exc.code != 4005 and exc.status and exc.status < 500 and exc.code != "unconfirmed":
            raise  # an explicit refusal: the first call cannot have booked either
    found = hotel_find_by_reference(item["attempt_ref"])
    if not found:
        return None
    bid = found.get("bookingId") or found.get("booking_Id") or found.get("booking_id")
    return _hotel_result(hotel_get(bid), ctx.get("money_mode") or money_mode_now())


def _plan_refunds(conn, settlement_id: str, lock: dict, item: dict, reason: str,
                  refund_cents: int | None = None) -> list[dict]:
    """Insert the PENDING refund rows for one item from the ledger: floor(refund ×
    line / Σ lines), remainder cents to the lowest user ids, each capped at that
    traveler's captured − refunded − already-pending (re-read here, inside the
    caller's write transaction, so two cancellations can never pay out twice).
    Nothing leaves for Stripe until the rows exist."""
    lines = {}
    for uid, check in lock["members"].items():
        own = sum(l["cents"] for l in check["lines"] if l["item_key"] == item["item_key"] and l["charged"])
        fee = sum(l["cents"] for l in check["lines"] if l["item_key"] == "fee")
        charged_wo_fee = max(1, check["charged_cents"] - fee)
        own += int(fee * own / charged_wo_fee) if fee and own else 0  # the fee share, pro rata
        if own > 0:
            lines[uid] = own
    total_lines = sum(lines.values())
    if not total_lines:
        return []
    pool = total_lines if refund_cents is None else max(0, min(int(refund_cents), total_lines))
    shares = {uid: pool * amt // total_lines for uid, amt in lines.items()}
    remainder = pool - sum(shares.values())
    for uid in sorted(lines):
        if remainder <= 0:
            break
        shares[uid] += 1
        remainder -= 1
    planned = []
    for uid in sorted(shares):
        a = conn.execute(
            "SELECT * FROM payment_attempts WHERE settlement_id = ? AND user_id = ? AND status IN ('captured', 'refunded')",
            (settlement_id, uid)).fetchone()
        if a is None:
            continue
        pending = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) s FROM refunds WHERE attempt_id = ? AND status = 'pending'",
                               (a["id"],)).fetchone()["s"]
        cap = max(0, int(a["captured_cents"] or 0) - int(a["refunded_cents"] or 0) - int(pending or 0))
        amount = min(shares[uid], cap)
        if amount <= 0:
            continue
        refund_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO refunds (id, settlement_id, item_key, user_id, attempt_id, reason, amount_cents, idempotency_key, status, provider_ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
            (refund_id, settlement_id, item["item_key"], uid, a["id"], reason, amount, refund_id, _now()))
        planned.append({"id": refund_id, "user_id": uid, "amount_cents": amount})
    return planned


def _update_receipt_lines(conn, attempt: dict, item_key: str | None, amount: int) -> None:
    """A partial refund marks the refunded line on the stored receipt instead of wiping it."""
    row = conn.execute("SELECT receipt FROM payments WHERE trip_id = ? AND user_id = ?", (attempt["trip_id"], attempt["user_id"])).fetchone()
    if row is None or not row["receipt"] or row["receipt"] == "{}":
        return
    try:
        receipt = json.loads(row["receipt"])
    except json.JSONDecodeError:
        return
    for line in receipt.get("items") or []:
        if item_key and line.get("item_key") == item_key:
            line["refunded"] = round(float(line.get("refunded") or 0) + amount / 100, 2)
    receipt["refunded_total"] = round(int(attempt.get("refunded_cents") or 0) / 100, 2)
    receipt["status"] = "refunded" if attempt["status"] == "refunded" else "partially_refunded"
    conn.execute("UPDATE payments SET receipt = ? WHERE trip_id = ? AND user_id = ?",
                 (json.dumps(receipt), attempt["trip_id"], attempt["user_id"]))


def _apply_refund(settlement_id: str, refund_id: str, provider_ref: str | None) -> None:
    """The refund went through: row, ledger, summary and receipt in one transaction."""
    with get_conn() as conn:
        ref = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
        if ref is None or ref["status"] == "succeeded":
            return
        conn.execute("UPDATE refunds SET status = 'succeeded', provider_ref = ? WHERE id = ?", (provider_ref, refund_id))
        conn.execute("UPDATE payment_attempts SET refunded_cents = refunded_cents + ?, updated_at = ? WHERE id = ?",
                     (ref["amount_cents"], _now(), ref["attempt_id"]))
        a = dict(conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (ref["attempt_id"],)).fetchone())
        if int(a["refunded_cents"] or 0) >= int(a["captured_cents"] or 0):
            a = _update_attempt(conn, a["id"], status="refunded")
        _sync_summary(conn, a)
        _update_receipt_lines(conn, a, ref["item_key"], ref["amount_cents"])
        _event(conn, settlement_id, "refund", f"refunded ({ref['reason']})", item_key=ref["item_key"],
               data={"user_id": ref["user_id"], "amount_cents": ref["amount_cents"], "refund_id": refund_id, "provider_ref": provider_ref})


def _issue_refunds(settlement_id: str, refund_ids: list[str]) -> int:
    """POST each pending refund row — its id is the idempotency key — and apply
    it to the ledger. The cursor stays on the refund step until the last one
    is applied, so a crash in between is resumed by the reconciler (it re-POSTs
    every row still pending with the same keys). A refund Stripe did not
    confirm parks the round in needs_attention; an explicit refusal too, with
    the row marked failed for a person."""
    refunded = 0
    for refund_id in refund_ids:
        with get_conn() as conn:
            ref = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
            if ref is None or ref["status"] != "pending":
                continue
            a = dict(conn.execute("SELECT * FROM payment_attempts WHERE id = ?", (ref["attempt_id"],)).fetchone())
            _set_cursor(conn, settlement_id, {"step": "refund", "item_key": ref["item_key"], "user_id": ref["user_id"],
                                              "idempotency_key": refund_id, "started_at": _now()})
        provider_ref = None
        if a["provider"] == "stripe":
            try:
                provider_ref = stripe.refund_payment_intent(a["payment_intent_id"], ref["amount_cents"], idempotency_key=refund_id).get("id")
            except stripe.StripeError as exc:
                if exc.status is None:
                    with get_conn() as conn:  # unknown outcome: row stays pending, cursor stays → reconciler re-POSTs the same key
                        _event(conn, settlement_id, "refund", f"refund not confirmed by Stripe: {exc}", item_key=ref["item_key"],
                               level="error", data={"user_id": ref["user_id"], "amount_cents": ref["amount_cents"], "refund_id": refund_id})
                    _needs_attention(settlement_id, f"a refund of ${dollars(ref['amount_cents'])} could not be confirmed with Stripe — retrying")
                    raise RefundUnresolved(str(exc))
                with get_conn() as conn:
                    conn.execute("UPDATE refunds SET status = 'failed' WHERE id = ?", (refund_id,))
                    _event(conn, settlement_id, "refund", f"refund refused by Stripe: {exc}", item_key=ref["item_key"],
                           level="error", data={"user_id": ref["user_id"], "amount_cents": ref["amount_cents"], "refund_id": refund_id})
                _needs_attention(settlement_id, f"Stripe refused a refund of ${dollars(ref['amount_cents'])}: {exc}")
                raise RefundUnresolved(str(exc))
        _apply_refund(settlement_id, refund_id, provider_ref)
        refunded += ref["amount_cents"]
    with get_conn() as conn:
        _set_cursor(conn, settlement_id, None)
    return refunded


def _reissue_pending_refunds(settlement_id: str) -> int:
    """Every refund row still pending for this settlement, re-POSTed with its own key."""
    with get_conn(readonly=True) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM refunds WHERE settlement_id = ? AND status = 'pending' ORDER BY created_at, id", (settlement_id,)).fetchall()]
    return _issue_refunds(settlement_id, ids) if ids else 0


def _refund_item(settlement_id: str, ctx: dict, lock: dict, item: dict, attempts: dict, reason: str,
                 refund_cents: int | None = None) -> int:
    """Refund an item's charge from the ledger (see _plan_refunds), rows first,
    Stripe second. Raises RefundUnresolved when a refund could not be confirmed."""
    with get_conn() as conn:
        planned = _plan_refunds(conn, settlement_id, lock, item, reason, refund_cents)
    try:
        return _issue_refunds(settlement_id, [p["id"] for p in planned])
    finally:
        with get_conn(readonly=True) as conn:
            attempts.clear()
            attempts.update(_attempts(conn, settlement_id))


def _mark_failed_item(settlement_id: str, trip_id: str, lock: dict, item: dict, reason: str) -> None:
    """Persist the failure BEFORE any refund: a crash between the two then
    re-runs the refund (idempotent rows), never the booking."""
    item["failed"] = reason
    item["charge"] = False
    with get_conn() as conn:
        conn.execute("UPDATE bookings SET status = 'to_book', provider = 'self', booked_at = NULL, note = ? WHERE trip_id = ? AND item_key = ?",
                     (reason[:400], trip_id, item["item_key"]))
        _save_lock(conn, settlement_id, lock)
        _event(conn, settlement_id, "item", reason, item_key=item["item_key"], level="warn")


def settle(settlement_id: str) -> None:
    """Idempotent progress over persisted state — safe to call again after
    a crash once the reconciler resolved the in-flight cursor."""
    with get_conn(readonly=True) as conn:
        row = _settlement(conn, settlement_id)
        if row is None or row["status"] not in ("verifying", "booking", "capturing", "unwinding", "paying"):
            return
    settlement = dict(row)
    trip_id = settlement["trip_id"]
    ctx = _trip_context(trip_id)
    ctx["money_mode"] = settlement["money_mode"]
    with get_conn() as conn:
        # The checklist rows are the bookings ledger: make sure every plan item
        # has one before anything is written against it (never INSERT here).
        sync_checklist(conn, ctx["trip"])

    # ---- unwinding (resumed after a crash or a parked refund) --------------------
    if settlement["status"] == "unwinding":
        lock = json.loads(settlement["lock"])
        with get_conn(readonly=True) as conn:
            attempts = _attempts(conn, settlement_id)
        _unwind_captures(settlement_id, ctx, lock, attempts, lock.get("unwinding") or "resuming an interrupted unwind")
        return

    # ---- verifying ----------------------------------------------------------
    if settlement["status"] == "verifying":
        with get_conn(readonly=True) as conn:
            lock = json.loads(settlement["lock"])
            attempts = _attempts(conn, settlement_id)
        problem = _preflight(settlement, lock, ctx, attempts, "before suppliers")
        if problem is None:
            problem = _dry_validate(ctx, lock)
        if problem:
            _fail_settlement(settlement_id, f"preflight failed: {problem}", ("verifying",), lock)
            return
        old_totals = {uid: c["charged_cents"] for uid, c in lock["members"].items()}
        lock, problems = _reverify(ctx, settlement, lock)
        rose = [uid for uid, c in lock["members"].items() if c["charged_cents"] > old_totals.get(uid, 0) + PRICE_RISE_TOLERANCE_CENTS]
        if problems or rose:
            _go_reconfirm(settlement_id, lock, problems or [f"{len(rose)} share(s) rose by more than $1"], "verifying")
            return
        for uid, c in lock["members"].items():
            c["capture_cents"] = min(attempts[uid]["amount_cents"], c["charged_cents"])
        with get_conn() as conn:
            _save_lock(conn, settlement_id, lock)
            if not _cas(conn, settlement_id, "booking", ("verifying",)):
                return
            _event(conn, settlement_id, "verify", "prices re-verified — booking")
        settlement["status"] = "booking"
        settlement["lock"] = json.dumps(lock)

    # ---- booking (pre-capture-safe hotels, hotels all-or-nothing) -------------
    if settlement["status"] == "booking":
        lock = json.loads(settlement["lock"])
        for item in [i for i in lock["items"] if i["charge"] and i["category"] == "stay" and i.get("pre_capture_safe")
                     and not i.get("booking") and not i.get("failed")]:
            if item["provider"] == "liteapi":
                result, err = _call(settlement_id, "book_hotel", item["item_key"], item["attempt_ref"], lambda it=item: _book_hotel(ctx, it))
                if err is not None and isinstance(err, BookingError) and err.code == "network":
                    result = _resolve_hotel_booking(ctx, item)
                    if result is None:
                        _needs_attention(settlement_id, f"{item['title']}: the hotel did not answer and the booking state is unknown", ctx)
                        return
                    err = None
            else:
                result, err = {"provider_booking_id": f"sim_{uuid.uuid4().hex[:10]}", "confirmation": sim_confirmation(),
                               "status": "booked", "documents": {"simulated": True}, "raw": {"simulated": True}}, None
            if err is not None:
                # Hotels are all-or-nothing: undo any hotel already booked, release everyone.
                for done in [i for i in lock["items"] if i.get("booking") and i["category"] == "stay"]:
                    if not _unbook_hotel(settlement_id, ctx, lock, done):
                        return
                _fail_settlement(settlement_id, f"{item['title']}: hotel booking failed ({err})", ("booking",), lock)
                return
            item["booking"] = result
            with get_conn() as conn:
                if not _write_booking(conn, trip_id, item, result):
                    _event(conn, settlement_id, "book", "checklist row missing for booked item", item_key=item["item_key"], level="error")
                _save_lock(conn, settlement_id, lock)
                _set_cursor(conn, settlement_id, None)
                _event(conn, settlement_id, "book", f"hotel booked ({result['status']})", item_key=item["item_key"],
                       data={"provider_booking_id": result["provider_booking_id"], "confirmation": result.get("confirmation")})
        with get_conn() as conn:
            if not _cas(conn, settlement_id, "capturing", ("booking",)):
                return
        settlement["status"] = "capturing"
        settlement["lock"] = json.dumps(lock)

    # ---- capturing ------------------------------------------------------------
    if settlement["status"] == "capturing":
        lock = json.loads(settlement["lock"])
        with get_conn(readonly=True) as conn:
            attempts = _attempts(conn, settlement_id)
        if lock.get("unwinding") or any(a["status"] == "refunded" for a in attempts.values()):
            # An unwind was interrupted: finish it — a capture is never retried past that point.
            _unwind_captures(settlement_id, ctx, lock, attempts, lock.get("unwinding") or "resuming an interrupted unwind")
            return
        pending = [m for m in ctx["members"] if attempts.get(m["id"], {}).get("status") == "authorized"]
        if pending and not any(a["status"] == "captured" for a in attempts.values()):
            problem = _preflight(settlement, lock, ctx, attempts, "before capture")
            if problem:
                for done in [i for i in lock["items"] if i.get("booking") and i["category"] == "stay"]:
                    if not _unbook_hotel(settlement_id, ctx, lock, done):
                        return
                _fail_settlement(settlement_id, f"capture preflight failed: {problem}", ("capturing",), lock)
                return
        for member in pending:
            a = attempts[member["id"]]
            amount = lock["members"][member["id"]].get("capture_cents", lock["members"][member["id"]]["charged_cents"])
            key = f"capture:{a['payment_intent_id']}"

            def do_capture(a=a, amount=amount, key=key):
                if a["provider"] != "stripe":
                    return {"simulated": True}
                if amount > 0:
                    return stripe.capture_payment_intent(a["payment_intent_id"], amount, idempotency_key=key)
                return stripe.cancel_payment_intent(a["payment_intent_id"], "requested_by_customer", idempotency_key=f"cancel:{a['payment_intent_id']}")

            result, err = _call(settlement_id, "capture", None, None, do_capture, user_id=member["id"], idempotency_key=key)
            if a["provider"] == "stripe":
                for _ in range(CAPTURE_RETRIES):
                    if not (isinstance(err, stripe.StripeError) and err.status is None):
                        break
                    # Transport failure: the capture may or may not have happened — read the PI, never guess.
                    try:
                        pi = stripe.retrieve_payment_intent(a["payment_intent_id"])
                    except stripe.StripeError as exc:
                        err = exc
                        continue
                    if pi.get("status") == "succeeded":
                        result, err, amount = pi, None, int(pi.get("amount_received") or amount)
                    elif pi.get("status") == "requires_capture":
                        result, err = _call(settlement_id, "capture", None, None, do_capture, user_id=member["id"], idempotency_key=key)
                    elif pi.get("status") == "canceled":
                        err = BookingError("the card hold is canceled", "canceled", 402)  # explicit: nothing to capture
                    else:
                        _needs_attention(settlement_id, f"capture outcome unknown for {member['name']}: the hold is {pi.get('status')}", ctx)
                        return
                if isinstance(err, stripe.StripeError) and err.status is None:
                    _needs_attention(settlement_id, f"capture outcome unknown for {member['name']} after {CAPTURE_RETRIES} attempts: {err}", ctx)
                    return
            if err is not None:
                # An explicit refusal (card declined, hold gone): unwind what moved, release the rest.
                _unwind_captures(settlement_id, ctx, lock, attempts, f"capture failed for {member['name']}: {err}")
                return
            with get_conn() as conn:
                a = _update_attempt(conn, a["id"], status="captured", captured_cents=amount)
                attempts[member["id"]] = a
                _sync_summary(conn, a, receipt=_receipt(ctx, member, lock["members"][member["id"]], "captured", amount))
                _set_cursor(conn, settlement_id, None)
                _event(conn, settlement_id, "capture", "share captured", data={"user_id": member["id"], "amount_cents": amount})
        for member in ctx["members"]:  # travelers who owed nothing are settled at $0, no provider involved
            a = attempts.get(member["id"])
            if a and a["status"] == "nothing_due":
                with get_conn() as conn:
                    a = _update_attempt(conn, a["id"], status="captured", captured_cents=0)
                    attempts[member["id"]] = a
                    _sync_summary(conn, a, receipt=_receipt(ctx, member, lock["members"][member["id"]], "captured", 0))
                    _event(conn, settlement_id, "capture", "nothing due — settled at $0", data={"user_id": member["id"], "amount_cents": 0})
        with get_conn() as conn:
            if not _cas(conn, settlement_id, "paying", ("capturing",)):
                return
        settlement["status"] = "paying"

    # ---- paying: the rest of the hotels, then the airlines -----------------------
    if settlement["status"] == "paying":
        lock = json.loads(settlement["lock"])
        with get_conn(readonly=True) as conn:
            attempts = _attempts(conn, settlement_id)
        try:
            _reissue_pending_refunds(settlement_id)  # a refund interrupted earlier in this phase
        except RefundUnresolved:
            return
        for item in [i for i in lock["items"] if i["charge"] and i["category"] == "stay" and not i.get("pre_capture_safe")
                     and not i.get("booking") and not i.get("failed")]:
            if item["provider"] == "liteapi":
                result, err = _call(settlement_id, "book_hotel", item["item_key"], item["attempt_ref"], lambda it=item: _book_hotel(ctx, it))
                if err is not None and isinstance(err, BookingError) and err.code == "network":
                    result = _resolve_hotel_booking(ctx, item)
                    if result is None:
                        _needs_attention(settlement_id, f"{item['title']}: the hotel did not answer and the booking state is unknown", ctx)
                        return
                    err = None
            else:
                result, err = {"provider_booking_id": f"sim_{uuid.uuid4().hex[:10]}", "confirmation": sim_confirmation(),
                               "status": "booked", "documents": {"simulated": True}, "raw": {"simulated": True}}, None
            if err is not None:
                _mark_failed_item(settlement_id, trip_id, lock, item, f"Hotel booking failed: {err}")
                try:
                    _refund_item(settlement_id, ctx, lock, item, attempts, "hotel_failed")
                except RefundUnresolved:
                    return
            else:
                item["booking"] = result
                with get_conn() as conn:
                    _write_booking(conn, trip_id, item, result)
                    _event(conn, settlement_id, "book", f"hotel booked ({result['status']})", item_key=item["item_key"],
                           data={"provider_booking_id": result["provider_booking_id"]})
            with get_conn() as conn:
                _save_lock(conn, settlement_id, lock)
                _set_cursor(conn, settlement_id, None)

        for item in [i for i in lock["items"] if i["charge"] and i["category"] == "flight" and not i.get("booking") and not i.get("failed")]:
            if item["provider"] == "simulated":
                ref = sim_confirmation()
                item["booking"] = {"provider_booking_id": f"sim_{uuid.uuid4().hex[:10]}", "confirmation": ref, "status": "booked",
                                   "documents": {"simulated": True, "booking_reference": ref}, "raw": {"simulated": True}}
                with get_conn() as conn:
                    _write_booking(conn, trip_id, item, item["booking"])
                    _save_lock(conn, settlement_id, lock)
                    _event(conn, settlement_id, "pay", "flight booked (simulated)", item_key=item["item_key"])
                continue
            failure = None
            for order in item["ref"].get("orders") or []:
                if order.get("paid"):
                    continue
                result, err = _call(settlement_id, "pay_flight", item["item_key"], item["attempt_ref"],
                                    lambda o=order: _pay_hold(ctx, o))
                if err is not None:
                    if isinstance(err, duffel.DuffelError) and err.code == "price_changed":
                        fresh = duffel.get_order(order["order_id"])
                        new_pp = split_cents(cents(fresh.get("total_amount")), max(1, len(item["travelers"])))[0]
                        if new_pp <= item["per_person_cents"] + ABSORB_CENTS_PER_PERSON:
                            order["total_amount"] = fresh.get("total_amount")
                            result, err = _call(settlement_id, "pay_flight", item["item_key"], item["attempt_ref"],
                                                lambda o=order: _pay_hold(ctx, o))
                    if err is not None and isinstance(err, duffel.DuffelError) and err.status is None:
                        # transport-level failure: unknown outcome → read it back
                        fresh = duffel.get_order(order["order_id"])
                        if not (fresh.get("payment_status") or {}).get("awaiting_payment", True):
                            result, err = fresh, None
                        else:
                            _needs_attention(settlement_id, f"{item['title']}: the airline did not answer and the payment state is unknown", ctx)
                            return
                if err is not None:
                    failure = str(err)
                    break
                order["paid"] = True
                order["documents"] = duffel.order_documents(result)
                with get_conn() as conn:
                    _save_lock(conn, settlement_id, lock)
                    _set_cursor(conn, settlement_id, None)
            if failure:
                _mark_failed_item(settlement_id, trip_id, lock, item, f"Flight booking failed: {failure}")
                try:
                    _refund_item(settlement_id, ctx, lock, item, attempts, "flight_failed")
                except RefundUnresolved:
                    return
            else:
                docs = [o["documents"] for o in item["ref"]["orders"]]
                item["booking"] = {"provider_booking_id": ",".join(o["order_id"] for o in item["ref"]["orders"]),
                                   "confirmation": " / ".join(d.get("booking_reference") or "" for d in docs).strip(" /"),
                                   "status": "booked", "documents": {"orders": docs},
                                   "raw": [{"order_id": o["order_id"], "total_amount": o.get("total_amount")} for o in item["ref"]["orders"]]}
                with get_conn() as conn:
                    _write_booking(conn, trip_id, item, item["booking"])
                    _event(conn, settlement_id, "pay", "flight paid and ticketed", item_key=item["item_key"],
                           data={"orders": [o["order_id"] for o in item["ref"]["orders"]]})
            with get_conn() as conn:
                _save_lock(conn, settlement_id, lock)
                _set_cursor(conn, settlement_id, None)

        # ---- confirm ------------------------------------------------------------
        with get_conn(readonly=True) as conn:
            unresolved = conn.execute("SELECT COUNT(*) c FROM refunds WHERE settlement_id = ? AND status IN ('pending', 'failed')",
                                      (settlement_id,)).fetchone()["c"]
        if unresolved:
            _needs_attention(settlement_id, f"{unresolved} refund(s) are unresolved — no receipts until they are", ctx)
            return
        failed = [i for i in lock["items"] if i.get("failed")]
        final = "partially_booked" if failed else "booked"
        with get_conn() as conn:
            _save_lock(conn, settlement_id, lock)
            if not _cas(conn, settlement_id, final, ("paying",)):
                return
            _set_cursor(conn, settlement_id, None)
            conn.execute("UPDATE trips SET status = 'booked' WHERE id = ?", (trip_id,))
            confirmations = [dict(r) for r in conn.execute(
                "SELECT item_key, title, provider, confirmation, status FROM bookings WHERE trip_id = ? AND provider != 'self' ORDER BY created_at",
                (trip_id,)).fetchall()]
            _event(conn, settlement_id, "confirm", f"settled: {final}", data={"failed": [i["item_key"] for i in failed]})
        _send_receipts(ctx, lock, attempts, confirmations)


def _pay_hold(ctx: dict, order: dict) -> dict:
    fresh = duffel.get_order(order["order_id"])
    duffel.assert_live_mode(fresh, ctx.get("money_mode") or money_mode_now(), "Duffel order")
    if not (fresh.get("payment_status") or {}).get("awaiting_payment", True):
        return fresh  # already paid (a resolved earlier call)
    duffel.pay_order_from_balance(order["order_id"], fresh.get("total_amount") or order.get("total_amount"),
                                 fresh.get("total_currency") or order.get("total_currency") or "USD")
    return duffel.get_order(order["order_id"])


def _unwind_captures(settlement_id: str, ctx: dict, lock: dict, attempts: dict, reason: str) -> None:
    """A capture was refused after others succeeded: the round moves to
    ``unwinding`` FIRST (so a crash resumes here, never into another capture),
    then every captured share is refunded in full, the hotels booked ahead of
    capture are cancelled, the remaining holds released and the round failed."""
    with get_conn() as conn:
        if not _cas(conn, settlement_id, "unwinding", ("capturing", "unwinding")):
            return
        if not lock.get("unwinding"):
            lock["unwinding"] = reason
            _save_lock(conn, settlement_id, lock)
            _event(conn, settlement_id, "unwind", reason, level="error")
        _set_cursor(conn, settlement_id, None)
    for done in [i for i in lock["items"] if i.get("booking") and i["category"] == "stay"]:
        if not _unbook_hotel(settlement_id, ctx, lock, done):
            return
    with get_conn() as conn:
        for uid, a in sorted(_attempts(conn, settlement_id).items()):
            if a["status"] != "captured":
                continue
            pending = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) s FROM refunds WHERE attempt_id = ? AND status = 'pending'",
                                   (a["id"],)).fetchone()["s"]
            amount = int(a.get("captured_cents") or 0) - int(a.get("refunded_cents") or 0) - int(pending or 0)
            if amount <= 0:
                continue
            refund_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO refunds (id, settlement_id, item_key, user_id, attempt_id, reason, amount_cents, idempotency_key, status, provider_ref, created_at) "
                "VALUES (?, ?, NULL, ?, ?, 'unwind', ?, ?, 'pending', NULL, ?)", (refund_id, settlement_id, uid, a["id"], amount, refund_id, _now()))
    try:
        _reissue_pending_refunds(settlement_id)
    except RefundUnresolved:
        return
    with get_conn(readonly=True) as conn:
        attempts.clear()
        attempts.update(_attempts(conn, settlement_id))
    _fail_settlement(settlement_id, lock.get("unwinding") or reason, ("unwinding",), lock)


def _unbook_hotel(settlement_id: str, ctx: dict, lock: dict, item: dict) -> bool:
    """Cancel a hotel booked ahead of capture. False = the supplier did not
    confirm; the round is parked with a cancel_hotel cursor for the reconciler."""
    if item["provider"] == "liteapi":
        result, err = _call(settlement_id, "cancel_hotel", item["item_key"], item["attempt_ref"],
                            lambda: hotel_cancel(item["booking"]["provider_booking_id"]))
        if err is not None:
            _needs_attention(settlement_id, f"{item['title']}: booked but could not be cancelled while unwinding ({err})", ctx)
            return False
    item.pop("booking", None)
    with get_conn() as conn:
        conn.execute("UPDATE bookings SET status = 'to_book', provider = 'self', confirmation = NULL, booked_at = NULL, cancelled_at = ?, note = ? "
                     "WHERE trip_id = ? AND item_key = ?",
                     (_now(), UNWOUND_HOTEL_NOTE, ctx["trip"]["id"], item["item_key"]))
        _save_lock(conn, settlement_id, lock)
        _set_cursor(conn, settlement_id, None)
        _event(conn, settlement_id, "unwind", "hotel booking cancelled while unwinding", item_key=item["item_key"], level="warn")
    return True


def _send_receipts(ctx: dict, lock: dict, attempts: dict, confirmations: list[dict]) -> None:
    for member in ctx["members"]:
        check = lock["members"].get(member["id"])
        a = attempts.get(member["id"])
        if not check or a is None:
            continue
        receipt = _receipt(ctx, member, check, "captured", a.get("captured_cents"))
        receipt["confirmations"] = confirmations
        html = email_service.receipt_html(ctx["trip"]["name"], ctx["plan"].get("destination", ""), member["name"],
                                          [l for l in receipt["items"] if l["charged"]], receipt["total"], len(ctx["members"]),
                                          confirmations=confirmations, estimates=[l for l in receipt["items"] if not l["charged"]],
                                          demo=receipt["demo"])
        result = email_service.send_email(member["email"], f"Your trip is booked — {ctx['trip']['name']}", html)
        with get_conn() as conn:
            conn.execute("UPDATE payments SET receipt = ?, email_status = ? WHERE trip_id = ? AND user_id = ?",
                         (json.dumps(receipt), "sent" if result.get("sent") else ("simulated" if result.get("simulated") else "failed"),
                          ctx["trip"]["id"], member["id"]))


# ---------------------------------------------------------------------------
# Reconciliation (see services/reconciler.py)
# ---------------------------------------------------------------------------

def stuck_settlement_ids(stale_after_seconds: int) -> list[str]:
    """Rounds the reconciler owns: in flight and quiet for a while, finished
    rounds with a cancellation/refund cursor still set, and parked rounds that
    carry a resume_to phase (needs_attention with something resolvable)."""
    cutoff = (_now_dt() - timedelta(seconds=stale_after_seconds)).isoformat(timespec="seconds")
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            """SELECT id FROM settlements WHERE updated_at < ? AND (
                   status IN ('verifying', 'booking', 'capturing', 'unwinding', 'paying', 'reconciling')
                   OR (status IN ('booked', 'partially_booked') AND cursor IS NOT NULL)
                   OR (status = 'needs_attention' AND cursor LIKE '%resume_to%'))
               ORDER BY updated_at""", (cutoff,)).fetchall()
    return [r["id"] for r in rows]


def overdue_open_trip_ids() -> list[str]:
    """Open rounds whose group deadline passed with nobody acting."""
    with get_conn(readonly=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trip_id FROM settlements WHERE status IN ('collecting', 'reconfirm') AND deadline IS NOT NULL AND deadline < ?",
            (_now(),)).fetchall()
    return [r["trip_id"] for r in rows]


def reconcile(settlement_id: str) -> dict:
    """Resolve the in-flight provider call by READING the provider, then let
    the settlement continue. Unknowable → needs_attention (kept resumable up
    to MAX_RECONCILE_ATTEMPTS times), nothing money-moving issued on a guess."""
    with get_conn() as conn:
        row = _settlement(conn, settlement_id)
        if row is None:
            return {"settlement_id": settlement_id, "skipped": True}
        status = row["status"]
        cursor = json.loads(row["cursor"]) if row["cursor"] else None
        if status in SETTLING_STATUSES:
            original = status if status != "reconciling" else None
        elif status in DONE_STATUSES and cursor:
            original = status
        elif status == "needs_attention" and cursor and cursor.get("resume_to"):
            original = cursor["resume_to"]
        else:
            return {"settlement_id": settlement_id, "skipped": True}
        tries = conn.execute(
            "SELECT COUNT(*) c FROM settlement_events WHERE settlement_id = ? AND step = 'reconcile' AND message LIKE 'reconciling from%'",
            (settlement_id,)).fetchone()["c"]
        if tries >= MAX_RECONCILE_ATTEMPTS:
            if status == "needs_attention":
                cursor.pop("resume_to", None)
                _set_cursor(conn, settlement_id, cursor or None)
                _event(conn, settlement_id, "reconcile", f"giving up after {tries} reconcile attempts — needs an operator", level="error")
                return {"settlement_id": settlement_id, "gave_up": True}
        else:
            if not _cas(conn, settlement_id, "reconciling", (status,)):
                return {"settlement_id": settlement_id, "skipped": True}
            lock = json.loads(row["lock"])
            trip_id = row["trip_id"]
            _event(conn, settlement_id, "reconcile", f"reconciling from {original or 'reconciling'}", data=cursor)
    if tries >= MAX_RECONCILE_ATTEMPTS:
        _needs_attention(settlement_id, f"could not finish after {tries} reconcile attempts", resumable=False)
        return {"settlement_id": settlement_id, "needs_attention": True}
    ctx = _trip_context(trip_id)
    ctx["money_mode"] = row["money_mode"]
    resume_to = original or _infer_phase(lock, settlement_id)
    if cursor and cursor.get("step") not in (None, "paused"):
        step, item_key = cursor.get("step"), cursor.get("item_key")
        item = next((i for i in lock["items"] if i["item_key"] == item_key), None)
        try:
            if step == "book_hotel" and item and not item.get("booking"):
                result = _resolve_hotel_booking(ctx, item)
                if result is None:
                    _needs_attention(settlement_id, f"{item['title']}: booking state unknown after a timeout", ctx, resume_to=resume_to)
                    return {"settlement_id": settlement_id, "needs_attention": True}
                item["booking"] = result
                with get_conn() as conn:
                    _write_booking(conn, trip_id, item, result)
            elif step == "capture" and cursor.get("user_id"):
                with get_conn(readonly=True) as conn:
                    a = next((x for x in _attempts(conn, settlement_id).values() if x["user_id"] == cursor["user_id"]), None)
                if a and a["provider"] == "stripe" and a["status"] == "authorized":
                    pi = stripe.retrieve_payment_intent(a["payment_intent_id"])
                    if pi.get("status") == "succeeded":
                        member = next((m for m in ctx["members"] if m["id"] == a["user_id"]), None)
                        captured = int(pi.get("amount_received") or a["amount_cents"])
                        with get_conn() as conn:
                            a = _update_attempt(conn, a["id"], status="captured", captured_cents=captured)
                            check = lock["members"].get(a["user_id"])
                            _sync_summary(conn, a, receipt=_receipt(ctx, member, check, "captured", captured) if member and check else {})
                            _event(conn, settlement_id, "capture", "share captured (found while reconciling)",
                                   data={"user_id": a["user_id"], "amount_cents": captured})
                    elif pi.get("status") != "requires_capture":
                        _needs_attention(settlement_id, f"{cursor['user_id']}: card hold is {pi.get('status')} mid-capture", ctx, resume_to=resume_to)
                        return {"settlement_id": settlement_id, "needs_attention": True}
            elif step == "pay_flight" and item:
                for order in item["ref"].get("orders") or []:
                    try:
                        match = duffel.get_order(order["order_id"])
                    except duffel.DuffelError as exc:
                        if exc.status == 404:
                            _needs_attention(settlement_id, f"{item['title']}: airline order {order['order_id']} not found while reconciling", ctx, resume_to=resume_to)
                            return {"settlement_id": settlement_id, "needs_attention": True}
                        raise
                    if not (match.get("payment_status") or {}).get("awaiting_payment", True):
                        order["paid"] = True
                        order["documents"] = duffel.order_documents(match)
            elif step == "cancel_hotel" and item and item.get("booking"):
                body = hotel_get(item["booking"]["provider_booking_id"]).get("data") or {}
                if not (body.get("status") or "").upper().startswith("CANCEL"):
                    hotel_cancel(item["booking"]["provider_booking_id"])
                with get_conn() as conn:
                    conn.execute("UPDATE bookings SET status = 'to_book', provider = 'self', confirmation = NULL, booked_at = NULL, cancelled_at = ?, note = ? "
                                 "WHERE trip_id = ? AND item_key = ?",
                                 (_now(), UNWOUND_HOTEL_NOTE, trip_id, item["item_key"]))
                item.pop("booking", None)
            elif step == "cancel_booking" and item:
                with get_conn(readonly=True) as conn:
                    brow = conn.execute("SELECT * FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)).fetchone()
                if brow is not None and brow["status"] == "cancelling":
                    outcome = _read_cancellation(dict(brow), item)
                    if outcome is None:
                        _needs_attention(settlement_id, f"{item['title']}: cancellation state unknown", ctx, resume_to=resume_to)
                        return {"settlement_id": settlement_id, "needs_attention": True}
                    if outcome["cancelled"]:
                        _finish_cancellation(settlement_id, lock, item, dict(brow), outcome["refund_cents"], outcome["note"],
                                             cursor.get("user_id"), outcome.get("fee_cents", 0))
                        resume_to = "partially_booked"
                    else:
                        with get_conn() as conn:
                            conn.execute("UPDATE bookings SET status = ? WHERE trip_id = ? AND item_key = ? AND status = 'cancelling'",
                                         (cursor.get("prior_status") or "booked", trip_id, item_key))
                            _event(conn, settlement_id, "cancel", "the supplier never received the cancellation — booking kept", item_key=item_key, level="warn")
            # Any refund row still pending (this cursor's or an earlier one) is re-POSTed with its own key.
            try:
                _reissue_pending_refunds(settlement_id)
            except RefundUnresolved:
                with get_conn() as conn:
                    _save_lock(conn, settlement_id, lock)
                return {"settlement_id": settlement_id, "needs_attention": True}
        except Exception as exc:
            with get_conn() as conn:
                _save_lock(conn, settlement_id, lock)
            _needs_attention(settlement_id, f"could not resolve the in-flight step {step}: {exc}", ctx, resume_to=resume_to)
            return {"settlement_id": settlement_id, "needs_attention": True}
    with get_conn() as conn:
        _save_lock(conn, settlement_id, lock)
        _set_cursor(conn, settlement_id, None)
        if not _cas(conn, settlement_id, resume_to, ("reconciling",)):
            return {"settlement_id": settlement_id, "skipped": True}
        _event(conn, settlement_id, "reconcile", f"resuming at {resume_to}")
    settle(settlement_id)
    return {"settlement_id": settlement_id, "resumed_at": resume_to}


def _infer_phase(lock: dict, settlement_id: str) -> str:
    with get_conn(readonly=True) as conn:
        attempts = _attempts(conn, settlement_id)
    if lock.get("unwinding") or any(a["status"] == "refunded" for a in attempts.values()):
        return "unwinding"
    if any(a["status"] == "captured" for a in attempts.values()):
        return "paying" if all(a["status"] != "authorized" for a in attempts.values()) else "capturing"
    if any(i.get("booking") for i in lock["items"]):
        return "booking"
    return "verifying"


# ---------------------------------------------------------------------------
# Cancelling a Rovyn-made booking after the fact
# ---------------------------------------------------------------------------

def _may_cancel(leader_id: str, item: dict, row: dict, user_id: str) -> bool:
    """The leader, the traveler a single-passenger flight was bought for, or
    any traveler on a city-scoped flight."""
    if user_id == leader_id or (row.get("payer_id") and user_id == row["payer_id"]):
        return True
    return item.get("category") == "flight" and user_id in (item.get("travelers") or [])


def _read_cancellation(row: dict, item: dict) -> dict | None:
    """After the supplier call's outcome was lost: is the booking cancelled?
    None = still unknown."""
    if row["provider"] == "liteapi":
        body = hotel_get(row["provider_booking_id"]).get("data") or {}
        status = (body.get("status") or "").upper()
        if not status:
            return None
        if not status.startswith("CANCEL"):
            return {"cancelled": False}
        refund = cents(body.get("refund_amount")) or (int(row["amount_cents"] or item["total_cents"] or 0)
                                                      if (body.get("refundType") or "").lower() == "full" else 0)
        return {"cancelled": True, "refund_cents": refund, "fee_cents": cents(body.get("cancellation_fee")),
                "note": f"cancelled with the hotel ({status})"}
    if row["provider"] == "duffel":
        refund, cancelled = 0, True
        for order_id in (row["provider_booking_id"] or "").split(","):
            if not order_id.strip():
                continue
            order = duffel.get_order(order_id.strip())
            if not order.get("cancelled_at") and not order.get("cancellation"):
                cancelled = False
            refund += cents((order.get("cancellation") or {}).get("refund_amount"))
        return {"cancelled": cancelled, "refund_cents": refund, "note": "cancelled with the airline (refund to balance)"}
    return {"cancelled": True, "refund_cents": int(row["amount_cents"] or item["total_cents"] or 0), "note": "cancelled (simulated)"}


def _finish_cancellation(settlement_id: str, lock: dict, item: dict, row: dict, refund_cents: int, note: str,
                         user_id: str | None, fee_cents: int = 0) -> list[str]:
    """The supplier confirmed: persist the cancelled row and the lock, then plan
    the refunds (rows only — _issue_refunds sends them). Returns the refund ids."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE bookings SET status = 'to_book', provider = 'self', confirmation = NULL, booked_at = NULL,
               cancelled_at = ?, note = ? WHERE trip_id = ? AND item_key = ? AND status = 'cancelling'""",
            (_now(), note, row["trip_id"], item["item_key"]))
        item["failed"] = "cancelled after booking"
        item["charge"] = False
        _save_lock(conn, settlement_id, lock)
        _event(conn, settlement_id, "cancel", note, item_key=item["item_key"], data={"user_id": user_id, "refund_cents": refund_cents})
        if fee_cents:
            _event(conn, settlement_id, "loss", f"supplier cancellation fee absorbed by Rovyn: {dollars(fee_cents)}",
                   item_key=item["item_key"], level="warn", data={"fee_cents": fee_cents})
        planned = _plan_refunds(conn, settlement_id, lock, item, "cancelled", refund_cents)
        _set_cursor(conn, settlement_id, {"step": "refund", "item_key": item["item_key"], "started_at": _now()} if planned else None)
    return [p["id"] for p in planned]


def cancel_booking(trip_id: str, item_key: str, user: dict) -> dict:
    """Cancel a Rovyn-made booking after the fact. The row is claimed
    (booked → cancelling) and a cursor persisted BEFORE the supplier is told,
    so a second click, a second canceller or a crash can never cancel or
    refund twice; refunds are planned from the ledger inside one transaction."""
    ctx = _trip_context(trip_id)
    with get_conn(readonly=True) as conn:
        row = conn.execute("SELECT * FROM bookings WHERE trip_id = ? AND item_key = ?", (trip_id, item_key)).fetchone()
        settlement = conn.execute(
            "SELECT * FROM settlements WHERE trip_id = ? AND status IN ('booked', 'partially_booked') ORDER BY round DESC LIMIT 1",
            (trip_id,)).fetchone()
    if row is None or row["provider"] not in ("liteapi", "duffel", "simulated"):
        raise BookingError("That item wasn't booked by Rovyn.", "not_rovyn", 400)
    if row["status"] == "cancelling":
        raise BookingError("That booking is already being cancelled.", "busy", 409)
    if row["status"] not in ("booked", "pending_confirmation"):
        raise BookingError("That booking isn't active.", "not_active", 400)
    if settlement is None:
        raise BookingError("No completed settlement for this trip.", "not_in_lock", 400)
    row, settlement = dict(row), dict(settlement)
    ctx["money_mode"] = settlement["money_mode"]
    lock = json.loads(settlement["lock"])
    item = next((i for i in lock["items"] if i["item_key"] == item_key), None)
    if item is None:
        raise BookingError("That item is not part of the settlement.", "not_in_lock", 400)
    if not _may_cancel(ctx["trip"]["leader_id"], item, row, user["id"]):
        raise BookingError("Only the trip leader (or a traveler this was booked for) can cancel this.", "forbidden", 403)

    # 1. Claim the row and persist the cursor — before anything leaves for the supplier.
    with get_conn() as conn:
        current = _settlement(conn, settlement["id"])
        if current["status"] not in DONE_STATUSES or current["cursor"]:
            raise BookingError("Another cancellation or refund is still being settled — try again in a minute.", "busy", 409)
        cur = conn.execute("UPDATE bookings SET status = 'cancelling' WHERE trip_id = ? AND item_key = ? AND status IN ('booked', 'pending_confirmation')",
                           (trip_id, item_key))
        if cur.rowcount != 1:
            raise BookingError("That booking is already being cancelled.", "busy", 409)
        _set_cursor(conn, settlement["id"], {"step": "cancel_booking", "item_key": item_key, "user_id": user["id"],
                                             "prior_status": row["status"], "started_at": _now()})
        _event(conn, settlement["id"], "cancel", "cancellation requested", item_key=item_key, data={"user_id": user["id"]})

    # 2. Tell the supplier. A lost answer is read back; a refusal restores the row.
    fee_cents = 0
    try:
        if row["provider"] == "liteapi":
            try:
                result = hotel_cancel(row["provider_booking_id"])
            except BookingError as exc:
                if exc.code != "network":
                    raise
                outcome = _read_cancellation(row, item)
                if not outcome or not outcome["cancelled"]:
                    raise
                result = {"status": "CANCELLED", "refund_cents": outcome["refund_cents"], "fee_cents": outcome.get("fee_cents", 0), "sandbox": None}
            _assert_sandbox_flag(result, settlement["money_mode"], "LiteAPI cancel")
            refund_cents, note = result["refund_cents"], f"cancelled with the hotel ({result['status']})"
            if result["status"] == "CANCELLED_WITH_CHARGES":
                fee_cents = result["fee_cents"]
        elif row["provider"] == "duffel":
            refund_cents, parts = 0, []
            for order_id in (row["provider_booking_id"] or "").split(","):
                if order_id.strip():
                    res = duffel.cancel_order(order_id.strip())
                    refund_cents += cents(res.get("refund_amount"))
                    parts.append(res.get("refund_to") or "")
            note = f"cancelled with the airline (refund to {', '.join(p for p in parts if p) or 'balance'})"
        else:
            refund_cents, note = int(row["amount_cents"] or item["total_cents"] or 0), "cancelled (simulated)"
    except Exception as exc:
        refused = (isinstance(exc, BookingError) and exc.code != "network" and exc.status is not None and exc.status < 500) \
            or (isinstance(exc, duffel.DuffelError) and exc.status is not None and exc.status < 500)
        with get_conn() as conn:
            if refused:
                conn.execute("UPDATE bookings SET status = ? WHERE trip_id = ? AND item_key = ? AND status = 'cancelling'",
                             (row["status"], trip_id, item_key))
                _set_cursor(conn, settlement["id"], None)
                _event(conn, settlement["id"], "cancel", f"the supplier refused the cancellation: {exc}", item_key=item_key, level="error")
            else:
                _event(conn, settlement["id"], "cancel", f"the supplier did not answer the cancellation: {exc}", item_key=item_key, level="error")
        if not refused:
            _needs_attention(settlement["id"], f"{row['title']}: cancellation state unknown", ctx)
        raise

    # 3. Persist the cancellation, then refund from the ledger.
    refund_ids = _finish_cancellation(settlement["id"], lock, item, row, refund_cents, note, user["id"], fee_cents)
    with get_conn() as conn:
        _cas(conn, settlement["id"], "partially_booked", DONE_STATUSES)
    try:
        refunded = _issue_refunds(settlement["id"], refund_ids)
    except RefundUnresolved:
        return {"cancelled": True, "refund_cents": refund_cents, "refunded_cents": 0, "refund_pending": True,
                "note": note + " — the refunds are being retried"}
    for member in ctx["members"]:
        email_service.send_email(member["email"], f"Booking cancelled — {ctx['trip']['name']}",
                                 email_service.cancellation_html(ctx["trip"]["name"], row["title"], user["name"], refunded / 100))
    return {"cancelled": True, "refund_cents": refund_cents, "refunded_cents": refunded, "note": note}


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

def checkout_summary(conn, trip, user_id: str) -> dict | None:
    """The light summary the plan poll carries (DB only, no provider calls)."""
    row = active_settlement(conn, trip["id"])
    if row is None:
        row = conn.execute("SELECT * FROM settlements WHERE trip_id = ? ORDER BY round DESC LIMIT 1", (trip["id"],)).fetchone()
    if row is None:
        return {"status": None, "transient": False}
    attempts = _attempts(conn, row["id"])
    mine = attempts.get(user_id)
    due = _parse_dt(row["deadline"])
    return {
        "status": row["status"], "round": row["round"], "money_mode": row["money_mode"], "deadline": row["deadline"],
        "deadline_passed": bool(due and due < _now_dt() and row["status"] in OPEN_STATUSES),
        "authorized_count": sum(1 for a in attempts.values() if a["status"] in ("authorized", "nothing_due", "captured", "refunded")),
        "my_status": mine["status"] if mine else None,
        "transient": row["status"] in SETTLING_STATUSES,
    }


def _public_item(item: dict) -> dict:
    ref = item.get("ref") or {}
    public = {k: v for k, v in item.items() if k not in ("ref", "booking")}
    public.update({
        "rate_name": ref.get("rate_name"), "board": ref.get("board"), "rooms": ref.get("rooms"), "owner": ref.get("owner"),
        "hold_until": min((o.get("payment_required_by") for o in ref.get("orders") or [] if o.get("payment_required_by")), default=None),
        "booked": bool(item.get("booking")),
        "confirmation": (item.get("booking") or {}).get("confirmation"),
    })
    return public


def checkout_state(trip, user: dict) -> dict:
    """Everything the checkout screen shows, member-scoped. No provider calls,
    no expiry side effects (those run on the write paths)."""
    trip_id = trip["id"]
    with get_conn(readonly=True) as conn:
        ok, reason = availability(conn, trip)
        row = active_settlement(conn, trip_id)
        if row is None:
            # A failed round stays visible (the leader can retry); an abandoned or
            # resolved one reads as a clean slate.
            last = conn.execute("SELECT * FROM settlements WHERE trip_id = ? ORDER BY round DESC LIMIT 1", (trip_id,)).fetchone()
            row = last if last is not None and last["status"] in ("failed", "needs_attention") else None
        members, profile_row, booking_rows, me_row = fetch_many(conn, [
            ("""SELECT u.id, u.name, u.email FROM trip_members t JOIN users u ON u.id = t.user_id
               WHERE t.trip_id = ? ORDER BY t.joined_at""", (trip_id,)),
            ("SELECT * FROM traveler_profiles WHERE user_id = ?", (user["id"],)),
            ("""SELECT item_key, category, title, provider, status, confirmation, provider_booking_id, documents,
                      amount_cents, note, cancelled_at, payer_id FROM bookings WHERE trip_id = ? AND provider != 'self'
               ORDER BY created_at""", (trip_id,)),
            ("SELECT * FROM payments WHERE trip_id = ? AND user_id = ?", (trip_id, user["id"])),
        ])
        attempts = _attempts(conn, row["id"]) if row else {}
        events = [dict(e) for e in conn.execute(
            "SELECT at, step, item_key, level, message, data_redacted FROM settlement_events WHERE settlement_id = ? ORDER BY id DESC LIMIT 60",
            (row["id"],)).fetchall()] if row else []
        self_book = conn.execute("SELECT 1 FROM payments WHERE trip_id = ? AND mode = 'self_book'", (trip_id,)).fetchone() is not None
    profile = dict(profile_row[0]) if profile_row else None
    mine = dict(me_row[0]) if me_row else None
    out = {
        "available": ok, "reason": reason, "modes": booking_modes(), "money_mode": money_mode_now(),
        "simulated": money_mode_now() == "simulated", "self_book_chosen": self_book,
        "me": {"profile": {k: v for k, v in (profile or {}).items() if k != "user_id"} if profile else None,
               "needs_profile": {"fields": ["given_name", "family_name"], "missing": [f for f in ("given_name", "family_name") if not (profile or {}).get(f)], "flight": False},
               "payment": None, "receipt": None, "check": None},
        "settlement": None,
        "confirmations": [{**dict(r), "documents": json.loads(r["documents"] or "{}")} for r in booking_rows],
    }
    if row is None:
        return out
    lock = json.loads(row["lock"])
    member = next((m for m in members if m["id"] == user["id"]), None)
    reqs = profile_requirements(lock, user["id"], profile, member["email"] if member else None)
    my_attempt = attempts.get(user["id"])
    my_check = lock["members"].get(user["id"]) or {"lines": [], "charged_cents": 0, "estimate_cents": 0, "total_cents": 0}
    out["me"].update({
        "needs_profile": reqs,
        "payment": {k: v for k, v in my_attempt.items() if k in ("status", "amount_cents", "capture_before", "captured_cents", "refunded_cents", "provider", "created_at", "updated_at")} if my_attempt else None,
        "receipt": json.loads(mine["receipt"]) if mine and mine.get("receipt") and mine["receipt"] not in ("{}", "") else None,
        "check": my_check,
    })

    def visible(e: dict) -> bool:
        data = json.loads(e["data_redacted"]) if e.get("data_redacted") else {}
        if isinstance(data, dict) and data.get("user_id") and data["user_id"] != user["id"]:
            return e["step"] not in ("authorize", "release", "refund", "capture", "session")
        return e["step"] not in ("session",)

    timeline = [{"at": e["at"], "step": e["step"], "item_key": e["item_key"], "level": e["level"], "message": e["message"]}
                for e in events if visible(e)][:25]
    if row["status"] in DONE_STATUSES:
        by_key = {i["item_key"]: i for i in lock["items"]}
        for c in out["confirmations"]:
            it = by_key.get(c["item_key"])
            c["can_cancel"] = bool(it) and c["status"] in ("booked", "pending_confirmation") \
                and _may_cancel(trip["leader_id"], it, c, user["id"])
    due = _parse_dt(row["deadline"])
    out["settlement"] = {
        "id": row["id"], "round": row["round"], "status": row["status"], "money_mode": row["money_mode"],
        "deadline": row["deadline"], "deadline_passed": bool(due and due < _now_dt() and row["status"] in OPEN_STATUSES),
        "verified_at": lock.get("verified_at"), "fee_percent": lock.get("fee_percent", 0),
        "currency": lock.get("currency", "USD"), "needs_ack": bool(lock.get("needs_ack")),
        "items": [_public_item(i) for i in lock["items"]],
        "members": [{"user_id": m["id"], "name": m["name"],
                     "state": (attempts.get(m["id"]) or {}).get("status") or "none",
                     "amount_cents": (lock["members"].get(m["id"]) or {}).get("charged_cents", 0),
                     "refunded_cents": (attempts.get(m["id"]) or {}).get("refunded_cents", 0)}
                    for m in members],
        "timeline": list(reversed(timeline)),
        "transient": row["status"] in SETTLING_STATUSES,
    }
    return out


# ---------------------------------------------------------------------------
# Guards the trip routers call
# ---------------------------------------------------------------------------

def assert_profile_change_allowed(trip_id: str, user: dict, profile: dict) -> None:
    """Once a traveler's share is in, the fields the booking needs cannot be
    blanked (the airline hold already carries them)."""
    with get_conn(readonly=True) as conn:
        row = active_settlement(conn, trip_id)
        if row is None:
            return
        mine = next((a for a in _attempts(conn, row["id"]).values() if a["user_id"] == user["id"]), None)
    if mine is None or mine["status"] not in ("authorized", "nothing_due", "captured", "releasing"):
        return
    reqs = profile_requirements(json.loads(row["lock"]), user["id"], profile, user.get("email"))
    if reqs["missing"]:
        raise BookingError("Your payment is in — these details are needed for the booking and can't be removed now: "
                           + ", ".join(reqs["missing"]), "profile_locked", 409)


def charged_item_keys(conn, trip_id: str) -> list[str]:
    """Checklist items the trip's open round buys through Rovyn (any round not
    cancelled, failed or resolved; [] when there is none). The checklist must
    not let a traveler record one of these as booked by hand — Rovyn would
    then buy it a second time."""
    row = active_settlement(conn, trip_id)
    if row is None:
        return []
    return [i["item_key"] for i in json.loads(row["lock"]).get("items", []) if i.get("charge") and not i.get("failed")]


def before_trip_delete(trip_id: str) -> None:
    """A trip with money in motion cannot be deleted; an idle round is closed
    and its airline holds released so the rows can go."""
    with get_conn(readonly=True) as conn:
        busy = conn.execute(
            "SELECT 1 FROM payment_attempts WHERE trip_id = ? AND status IN ('pending', 'authorized', 'captured', 'releasing')",
            (trip_id,)).fetchone()
        row = active_settlement(conn, trip_id)
    if busy:
        raise BookingError("Money is still in motion for this trip — abandon the group checkout or cancel its bookings first.",
                           "locked", 400)
    if row is not None:
        lock = json.loads(row["lock"])
        with get_conn() as conn:
            _cas(conn, row["id"], "cancelled", (row["status"],))
        _release_holds(lock)


def delete_trip_rows(conn, trip_id: str) -> None:
    """The settlement tables, in foreign-key order, before the trip row goes.
    (webhook_events and traveler_profiles are not trip-scoped and stay.)"""
    conn.execute("DELETE FROM refunds WHERE settlement_id IN (SELECT id FROM settlements WHERE trip_id = ?)", (trip_id,))
    conn.execute("DELETE FROM settlement_events WHERE settlement_id IN (SELECT id FROM settlements WHERE trip_id = ?)", (trip_id,))
    conn.execute("DELETE FROM payment_attempts WHERE trip_id = ?", (trip_id,))
    conn.execute("DELETE FROM settlements WHERE trip_id = ?", (trip_id,))


def pending_attempt_for(conn, trip_id: str, user_id: str) -> dict | None:
    """The traveler's open (pending) attempt, for leave_trip to close properly."""
    row = conn.execute(
        "SELECT * FROM payment_attempts WHERE trip_id = ? AND user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
        (trip_id, user_id)).fetchone()
    return dict(row) if row else None
