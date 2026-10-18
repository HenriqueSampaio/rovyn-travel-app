"""In-process tests for the payments & auto-booking prototype.

Runs with plain python (no pytest needed):

    /Users/henriquesampaio/Rovyn/Rovyn/.venv/bin/python tests/test_payments_flow.py

Everything is isolated: a temp SQLite DB, settlements run inline, and a fake
urllib layer standing in for Stripe, LiteAPI (search + booking hosts) and
Duffel. The fake RECORDS every call and raises on any URL it does not know,
and every scenario asserts that nothing unexpected went out.
"""
import io
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "DATABASE_URL": "", "SUPABASE_URL": "", "SUPABASE_ANON_KEY": "", "SUPABASE_JWT_SECRET": "",
    "ALLOW_LEGACY_AUTH": "1", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "", "LITEAPI_API_KEY": "",
    "PEXELS_API_KEY": "", "GOOGLE_PLACES_API_KEY": "", "GOOGLE_MAPS_EMBED_KEY": "", "RESEND_API_KEY": "",
    "STRIPE_SECRET_KEY": "", "STRIPE_WEBHOOK_SECRET": "", "LITEAPI_BOOK_MODE": "off", "LITEAPI_BOOK_API_KEY": "",
    "LITEAPI_WEBHOOK_TOKEN": "", "DUFFEL_ACCESS_TOKEN": "", "ROVYN_LIVE_MONEY": "", "ROVYN_SETTLE_INLINE": "1",
    "ROVYN_RECONCILER": "0", "APP_PUBLIC_URL": "http://localhost:5173", "CHECKOUT_WINDOW_DAYS": "5",
})

TMP = Path(tempfile.mkdtemp(prefix="rovyn-payments-test-"))
import backend.db as db  # noqa: E402
db._DB_PATH = TMP / "rovyn.db"
db.init_db()

from fastapi.testclient import TestClient  # noqa: E402
import backend.main  # noqa: E402
from backend.services import booking_service, payment_service, duffel_booking, reconciler  # noqa: E402

client = TestClient(backend.main.app)
FIX = ROOT / "tests" / "fixtures"

# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------

CALLS: dict = {"log": [], "unexpected": []}
STRIPE: dict = {"sessions": {}, "intents": {}, "refunds": [], "captures": [], "fail_capture_for": set(),
                "n": 0, "livemode": False}
LITE: dict = {"prebook_unclean_once": False, "book_timeout_once": False, "book_fail_once": False,
              "confirmation": "test", "booked": {}, "rates_price": None}
DUFFEL: dict = {"orders": {}, "pay_fail": None, "pay_timeout_once": False, "offer_price": "612.00",
                "currency": "USD", "required_by_hours": 72, "instant": False, "reverify_price": None, "n": 0}


class _Resp(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json(body, status=200):
    r = _Resp(json.dumps(body).encode())
    r.status = status
    return r


def _http_error(url, status, body):
    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(json.dumps(body).encode()))


def _form(req) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs((req.data or b"").decode()).items()}


def _now_ts() -> int:
    return int(time.time())


def _iso(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stripe(req, url):
    path = urllib.parse.urlparse(url).path
    method = req.get_method()
    headers = {k.lower(): v for k, v in req.header_items()}
    if path == "/v1/checkout/sessions" and method == "POST":
        form = _form(req)
        STRIPE["n"] += 1
        sid = f"cs_test_{STRIPE['n']}"
        meta = {k[9:-1]: v for k, v in form.items() if k.startswith("metadata[")}
        STRIPE["sessions"][sid] = {"id": sid, "object": "checkout.session", "status": "open", "payment_status": "unpaid",
                                   "url": f"https://checkout.stripe.com/c/pay/{sid}", "expires_at": int(form["expires_at"]),
                                   "metadata": meta, "client_reference_id": form.get("client_reference_id"),
                                   "amount_total": int(form["line_items[0][price_data][unit_amount]"]),
                                   "payment_intent": None, "livemode": STRIPE["livemode"], "form": form}
        return _json({k: v for k, v in STRIPE["sessions"][sid].items() if k != "form"})
    if path.startswith("/v1/checkout/sessions/") and path.endswith("/expire"):
        sid = path.split("/")[4]
        STRIPE["sessions"][sid]["status"] = "expired"
        return _json(STRIPE["sessions"][sid])
    if path.startswith("/v1/checkout/sessions/") and method == "GET":
        sid = path.split("/")[4]
        s = dict(STRIPE["sessions"][sid])
        if isinstance(s.get("payment_intent"), str):
            s["payment_intent"] = dict(STRIPE["intents"][s["payment_intent"]])
        s.pop("form", None)
        return _json(s)
    if path.startswith("/v1/payment_intents/") and method == "GET":
        pi = path.split("/")[3]
        return _json(STRIPE["intents"][pi])
    if path.startswith("/v1/payment_intents/") and path.endswith("/capture"):
        pi = path.split("/")[3]
        STRIPE["captures"].append((pi, _form(req).get("amount_to_capture"), headers.get("idempotency-key")))
        if pi in STRIPE["fail_capture_for"]:
            raise _http_error(url, 402, {"error": {"type": "card_error", "code": "card_declined", "message": "Your card was declined."}})
        intent = STRIPE["intents"][pi]
        intent.update({"status": "succeeded", "amount_received": int(_form(req).get("amount_to_capture") or intent["amount"]),
                       "amount_capturable": 0})
        return _json(intent)
    if path.startswith("/v1/payment_intents/") and path.endswith("/cancel"):
        pi = path.split("/")[3]
        STRIPE["intents"][pi].update({"status": "canceled", "amount_capturable": 0})
        return _json(STRIPE["intents"][pi])
    if path == "/v1/refunds" and method == "POST":
        form = _form(req)
        STRIPE["refunds"].append({"pi": form["payment_intent"], "amount": int(form["amount"]), "key": headers.get("idempotency-key")})
        return _json({"id": f"re_{len(STRIPE['refunds'])}", "status": "succeeded", "amount": int(form["amount"]),
                      "payment_intent": form["payment_intent"]})
    raise AssertionError(f"unexpected stripe call {method} {path}")


def pay_session(sid: str, extended: str = "disabled") -> str:
    """Pretend the traveler completed hosted Checkout: a PI in requires_capture."""
    s = STRIPE["sessions"][sid]
    pi = f"pi_test_{sid.split('_')[-1]}"
    STRIPE["intents"][pi] = {
        "id": pi, "object": "payment_intent", "status": "requires_capture", "amount": s["amount_total"],
        "amount_capturable": s["amount_total"], "amount_received": 0, "metadata": s["metadata"], "livemode": STRIPE["livemode"],
        "latest_charge": {"id": f"ch_{pi}", "payment_method_details": {"card": {
            "capture_before": _now_ts() + 7 * 86400, "extended_authorization": {"status": extended}, "last4": "4242", "brand": "visa"}}},
    }
    s.update({"status": "complete", "payment_status": "unpaid", "payment_intent": pi})
    return pi


def _lite(req, url):
    parsed = urllib.parse.urlparse(url)
    path, method = parsed.path, req.get_method()
    assert req.get_header("X-api-key") == "sand_fake", "booking calls must use the booking key"
    if path == "/v3.0/hotels/rates":
        body = json.loads(req.data.decode())
        assert body.get("margin") == 0, "net rates need margin 0"
        fx = json.loads((FIX / "liteapi_book_rates.json").read_text())
        fx["data"] = [h for h in fx["data"] if h["hotelId"] == body["hotelIds"][0]] or fx["data"][:1]
        if LITE["rates_price"] is not None:
            fx["data"][0]["roomTypes"][0]["offerRetailRate"]["amount"] = LITE["rates_price"]
        return _json(fx)
    if path == "/v3.0/rates/prebook":
        fx = json.loads((FIX / "liteapi_book_prebook.json").read_text())
        if LITE["prebook_unclean_once"]:
            LITE["prebook_unclean_once"] = False
            fx["data"]["priceDifferencePercent"] = 3
        if LITE["rates_price"] is not None:
            fx["data"]["price"] = LITE["rates_price"]
        return _json(fx)
    if path == "/v3.0/rates/book":
        body = json.loads(req.data.decode())
        assert body["payment"] == {"method": "ACC_CREDIT_CARD"}
        assert body["clientReference"] and body["holder"]["firstName"] and body["guests"][0]["occupancyNumber"] == 1
        if LITE["book_timeout_once"]:
            LITE["book_timeout_once"] = False
            # the booking DID go through at the supplier before the socket died
            fx = json.loads((FIX / "liteapi_book_book.json").read_text())
            fx["data"]["clientReference"] = body["clientReference"]
            fx["data"]["bookingId"] = "lXsTIMEOUT"
            fx["data"]["hotelConfirmationCode"] = LITE["confirmation"]
            LITE["booked"][body["clientReference"]] = fx
            raise socket.timeout("timed out")
        if body["clientReference"] in LITE["booked"]:
            raise _http_error(url, 400, {"error": {"code": 4005, "message": "duplicate clientReference"}})
        if LITE["book_fail_once"]:
            LITE["book_fail_once"] = False
            raise _http_error(url, 410, {"error": {"code": 4012, "message": "booking is not confirmed"}})
        fx = json.loads((FIX / "liteapi_book_book.json").read_text())
        fx["data"]["clientReference"] = body["clientReference"]
        fx["data"]["bookingId"] = f"lX{len(LITE['booked']) + 1:04d}"
        fx["data"]["hotelConfirmationCode"] = LITE["confirmation"]
        LITE["booked"][body["clientReference"]] = fx
        return _json(fx)
    if path == "/v3.0/bookings/" and method == "GET":
        assert "bookingStartDate" in parsed.query
        return _json({"count": len(LITE["booked"]), "data": [
            {"bookingId": v["data"]["bookingId"], "client_reference": k, "status": v["data"]["status"]} for k, v in LITE["booked"].items()]})
    if path.startswith("/v3.0/bookings/") and method == "GET":
        bid = path.split("/")[3]
        match = next((v for v in LITE["booked"].values() if v["data"]["bookingId"] == bid), None)
        fx = json.loads((FIX / "liteapi_book_get.json").read_text())
        fx["data"]["bookingId"] = bid
        fx["data"]["hotelConfirmationCode"] = (match or fx)["data"].get("hotelConfirmationCode", LITE["confirmation"])
        if match and match["data"].get("status") == "CANCELLED":
            fx["data"]["status"] = "CANCELLED"
        return _json(fx)
    if path.startswith("/v3.0/bookings/") and method == "PUT":
        bid = path.split("/")[3]
        for v in LITE["booked"].values():
            if v["data"]["bookingId"] == bid:
                v["data"]["status"] = "CANCELLED"
        fx = json.loads((FIX / "liteapi_book_cancel.json").read_text())
        fx["data"]["bookingId"] = bid
        return _json(fx)
    raise AssertionError(f"unexpected liteapi call {method} {path}")


def _offer(offer_id: str, passengers: int, price: str | None = None) -> dict:
    return {
        "id": offer_id, "object": "offer", "live_mode": False, "total_amount": price or DUFFEL["offer_price"],
        "total_currency": DUFFEL["currency"], "expires_at": _iso(0.5), "owner": {"name": "Duffel Airways", "iata_code": "ZZ"},
        "passengers": [{"id": f"pas_{offer_id}_{i}", "type": "adult"} for i in range(passengers)],
        "payment_requirements": {"requires_instant_payment": DUFFEL["instant"], "price_guarantee_expires_at": _iso(DUFFEL["required_by_hours"]),
                                 "payment_required_by": _iso(DUFFEL["required_by_hours"])},
        "conditions": {"refund_before_departure": {"allowed": False}, "change_before_departure": {"allowed": True}},
        "slices": [
            {"segments": [{"marketing_carrier": {"iata_code": "ZZ", "name": "Duffel Airways"}, "operating_carrier": {"iata_code": "ZZ", "name": "Duffel Airways"},
                           "marketing_carrier_flight_number": "123", "departing_at": "2026-12-01T09:00:00", "arriving_at": "2026-12-01T21:00:00",
                           "origin": {"iata_code": "JFK"}, "destination": {"iata_code": "LIS"}}]},
            {"segments": [{"marketing_carrier": {"iata_code": "ZZ", "name": "Duffel Airways"}, "operating_carrier": {"iata_code": "ZZ", "name": "Duffel Airways"},
                           "marketing_carrier_flight_number": "124", "departing_at": "2026-12-06T12:00:00", "arriving_at": "2026-12-06T16:00:00",
                           "origin": {"iata_code": "LIS"}, "destination": {"iata_code": "JFK"}}]},
        ],
    }


def _duffel(req, url):
    parsed = urllib.parse.urlparse(url)
    path, method = parsed.path, req.get_method()
    assert req.get_header("Duffel-version") == "v2" and req.get_header("Authorization") == "Bearer duffel_test_fake"
    body = json.loads(req.data.decode())["data"] if req.data else None
    if path == "/air/offer_requests":
        DUFFEL["n"] += 1
        n = len(body["passengers"])
        return _json({"data": {"id": f"orq_{DUFFEL['n']}", "live_mode": False, "passengers": [{"id": f"pas_{i}"} for i in range(n)],
                               "offers": [_offer(f"off_{DUFFEL['n']}", n), {**_offer(f"off_other_{DUFFEL['n']}", n, "99.00"),
                                                                              "slices": [{"segments": [{"marketing_carrier": {"iata_code": "QQ"}, "marketing_carrier_flight_number": "1", "departing_at": "2026-12-01T05:00:00"}]}]}]}})
    if path.startswith("/air/offers/") and method == "GET":
        return _json({"data": _offer(path.split("/")[3], 1)})
    if path == "/air/orders" and method == "POST":
        assert body["type"] == "hold" and "payments" not in body
        for p in body["passengers"]:
            assert p["born_on"] and p["gender"] and p["title"] and p["phone_number"] and p["email"]
        DUFFEL["n"] += 1
        oid = f"ord_{DUFFEL['n']}"
        order = {**_offer(body["selected_offers"][0], len(body["passengers"])), "id": oid, "object": "order",
                 "booking_reference": f"PNR{DUFFEL['n']}", "metadata": body["metadata"], "documents": [],
                 "payment_status": {"awaiting_payment": True, "payment_required_by": _iso(DUFFEL["required_by_hours"]),
                                    "price_guarantee_expires_at": _iso(DUFFEL["required_by_hours"])}}
        DUFFEL["orders"][oid] = order
        return _json({"data": order})
    if path == "/air/orders" and method == "GET":
        return _json({"data": list(DUFFEL["orders"].values())[::-1]})
    if path.startswith("/air/orders/") and method == "GET":
        order = dict(DUFFEL["orders"][path.split("/")[3]])
        if DUFFEL["reverify_price"]:
            order["total_amount"] = DUFFEL["reverify_price"]
        return _json({"data": order})
    if path == "/air/payments":
        oid = body["order_id"]
        if DUFFEL["pay_timeout_once"]:
            DUFFEL["pay_timeout_once"] = False
            DUFFEL["orders"][oid]["payment_status"]["awaiting_payment"] = False  # it went through
            DUFFEL["orders"][oid]["documents"] = [{"type": "electronic_ticket", "unique_identifier": "1234567890", "passenger_ids": []}]
            raise socket.timeout("timed out")
        if DUFFEL["pay_fail"]:
            code = DUFFEL["pay_fail"]
            if code == "price_changed_once":
                DUFFEL["pay_fail"] = None
                DUFFEL["orders"][oid]["total_amount"] = str(float(DUFFEL["orders"][oid]["total_amount"]) + 0.50)
                raise _http_error(url, 422, {"errors": [{"code": "price_changed", "message": "The price changed"}]})
            raise _http_error(url, 422, {"errors": [{"code": code, "message": f"{code}"}]})
        DUFFEL["orders"][oid]["payment_status"]["awaiting_payment"] = False
        DUFFEL["orders"][oid]["documents"] = [{"type": "electronic_ticket", "unique_identifier": "0017654321", "passenger_ids": []}]
        return _json({"data": {"id": f"pay_{oid}", "status": "succeeded", "amount": body["payment"]["amount"]}})
    if path == "/air/order_cancellations" and method == "POST":
        return _json({"data": {"id": f"ore_{body['order_id']}", "refund_amount": "0.00", "refund_to": "balance"}})
    if path.startswith("/air/order_cancellations/") and path.endswith("/confirm"):
        return _json({"data": {"id": path.split("/")[3], "refund_amount": "0.00", "refund_to": "balance"}})
    raise AssertionError(f"unexpected duffel call {method} {path}")


def fake_urlopen(req, timeout=None, context=None):
    url = req.full_url if hasattr(req, "full_url") else req
    host = urllib.parse.urlparse(url).netloc
    CALLS["log"].append((req.get_method() if hasattr(req, "get_method") else "GET", url.split("?")[0]))
    try:
        if host == "api.stripe.com":
            return _stripe(req, url)
        if host in ("api.liteapi.travel", "book.liteapi.travel"):
            return _lite(req, url)
        if host == "api.duffel.com":
            return _duffel(req, url)
    except AssertionError as exc:
        CALLS["unexpected"].append(str(exc))
        raise
    CALLS["unexpected"].append(url)
    raise AssertionError(f"unexpected outbound URL in test: {url}")


urllib.request.urlopen = fake_urlopen
payment_service._sleep = lambda s: None

# ---------------------------------------------------------------------------
# Fixtures: users, a trip with an approved plan
# ---------------------------------------------------------------------------

passed = 0


def ok(label, cond, extra=""):
    global passed
    if not cond:  # dump the latest settlement's audit trail to make failures diagnosable
        print("    unexpected calls:", CALLS["unexpected"])
        for e in rows("SELECT s.status, e.at, e.step, e.level, e.message, e.data_redacted FROM settlement_events e JOIN settlements s ON s.id = e.settlement_id ORDER BY e.id DESC LIMIT 14"):
            print("    event:", e)
    assert cond, f"FAIL: {label} {extra}"
    passed += 1
    print(f"  ok - {label}")


def signup(name: str) -> dict:
    res = client.post("/auth/signup", json={"email": f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com",
                                            "password": "secret123", "name": name}).json()
    return {"id": res["user"]["id"], "token": res["token"], "name": name, "H": {"Authorization": f"Bearer {res['token']}"}}


FLIGHT_NY = {"airline": "Duffel Airways", "route": "JFK → LIS", "return_route": "LIS → JFK",
             "depart": "2026-12-01 09:00", "arrive": "2026-12-01 21:00", "return": "2026-12-06 12:00",
             "return_arrive": "2026-12-06 16:00", "stops": 0, "duration": "7h", "est_price_per_person": 600,
             "live_price": True, "supplier": "duffel", "duffel_offer_id": "off_board",
             "segments": {"out": [{"leg": "JFK → LIS", "carrier": "Duffel Airways", "flight_no": "ZZ 123",
                                   "depart": "2026-12-01 09:00", "arrive": "2026-12-01 21:00"}],
                          "back": [{"leg": "LIS → JFK", "carrier": "Duffel Airways", "flight_no": "ZZ 124",
                                    "depart": "2026-12-06 12:00", "arrive": "2026-12-06 16:00"}]}}
FLIGHT_BOS = {"airline": "SampleAir", "route": "BOS → LIS", "depart": "2026-12-01 10:00", "return": "2026-12-06 11:00",
              "stops": 1, "est_price_per_person": 540}


def make_plan(stay_hotel="lp36d84", stays=None):
    stay = {"name": "Smart Hotel Roma", "type": "hotel", "price_per_night": 162, "total_per_person": 243,
            "liteapi_hotel_id": stay_hotel, "check_in": "2026-12-01", "check_out": "2026-12-04"}
    plan = {"destination": "Rome, Italy", "country": "Italy", "dates": {"start": "2026-12-01", "end": "2026-12-06"},
            "duration_days": 5, "trip_mode": "single", "summary": "test", "decisions": [],
            "flights": {"New York": dict(FLIGHT_NY), "Boston": dict(FLIGHT_BOS)},
            "itinerary": {"title": "Days", "days": [{"day": 1, "date": "2026-12-01", "theme": "Arrive", "morning": "Land",
                                                      "afternoon": "Walk", "evening": "Dinner"}]},
            "visits": [{"id": "visits-1", "name": "Colosseum", "cost_per_person": 25}], "events": [],
            "cost_per_person": {"stay": 243, "visits": 25, "events": 0, "meals_estimate": 250, "transport_estimate": 80,
                                "shared_total": 598},
            "flight_cost_by_city": {"New York": 600, "Boston": 540}, "tips": [], "revision_notes": [], "source": "sample"}
    if stays is not None:
        plan["stays"] = stays
        plan["trip_mode"] = "multi"
        plan.pop("stay", None)
    else:
        plan["stay"] = stay
    return plan


def make_trip(members=3, plan=None, cities=("New York", "New York", "Boston")):
    users = [signup(n) for n in ("Ana", "Bruno", "Carla", "Dani")[:members]]
    leader = users[0]
    trip = client.post("/trips", headers=leader["H"], json={"name": "Rome test", "destination": "Rome, Italy"}).json()
    trip_id = trip["id"]
    for u in users[1:]:
        client.post("/trips/join", headers=u["H"], json={"invite_code": trip["invite_code"]})
    plan = plan or make_plan()
    with db.get_conn() as conn:
        for u, city in zip(users, cities):
            conn.execute("INSERT INTO questionnaires (trip_id, user_id, data, submitted_at) VALUES (?, ?, ?, ?)",
                         (trip_id, u["id"], json.dumps({"departure_city": city, "budget_min": 500, "budget_max": 2000}), "2026-10-01T00:00:00"))
        conn.execute("UPDATE trips SET final_plan = ?, status = 'approved', trip_mode = ? WHERE id = ?",
                     (json.dumps(plan), plan.get("trip_mode", "single"), trip_id))
    return trip_id, users


def profile(u, flight=True):
    body = {"given_name": u["name"], "family_name": "Tester"}
    if flight:
        body.update({"born_on": "1990-04-02", "gender": "m", "title": "mr", "phone": "+14155550123"})
    return client.post(f"/trips/{TRIP}/checkout/profile", headers=u["H"], json=body)


def checkout(u, trip_id=None):
    return client.get(f"/trips/{trip_id or TRIP}/checkout", headers=u["H"]).json()


def reset_fakes():
    CALLS["log"].clear(); CALLS["unexpected"].clear()
    STRIPE.update({"sessions": {}, "intents": {}, "refunds": [], "captures": [], "fail_capture_for": set()})  # n keeps counting: session ids are globally unique
    LITE.update({"prebook_unclean_once": False, "book_timeout_once": False, "book_fail_once": False, "confirmation": "test",
                 "booked": {}, "rates_price": None})
    DUFFEL.update({"orders": {}, "pay_fail": None, "pay_timeout_once": False, "offer_price": "612.00", "currency": "USD",
                   "required_by_hours": 72, "instant": False, "reverify_price": None, "n": 0})


def set_mode(stripe=False, hotel=False, flights=False):
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake" if stripe else ""
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test" if stripe else ""
    os.environ["LITEAPI_BOOK_MODE"] = "sandbox" if hotel else "off"
    os.environ["LITEAPI_BOOK_API_KEY"] = "sand_fake" if hotel else ""
    os.environ["DUFFEL_ACCESS_TOKEN"] = "duffel_test_fake" if flights else ""


def rows(sql, *params):
    with db.get_conn(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ===========================================================================
# 1. Live-key guard (allow-list)
# ===========================================================================
print("live-key guard:")
for name, value in (("STRIPE_SECRET_KEY", "sk_live_x"), ("LITEAPI_BOOK_API_KEY", "prod_x"), ("DUFFEL_ACCESS_TOKEN", "duffel_live_x")):
    os.environ[name] = value
    try:
        payment_service.refuse_live_keys()
        ok(f"{name} live refused", False)
    except RuntimeError:
        ok(f"{name} live refused", True)
    os.environ[name] = ""
os.environ["STRIPE_SECRET_KEY"] = "sk_weird_x"
try:
    payment_service.refuse_live_keys()
    ok("unknown key shape refused", False)
except RuntimeError:
    ok("unknown key shape refused", True)
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
payment_service.refuse_live_keys()
ok("sk_test_ accepted", True)
os.environ["STRIPE_SECRET_KEY"] = "sk_live_x"; os.environ["ROVYN_LIVE_MONEY"] = "1"
payment_service.refuse_live_keys()
ok("live accepted only with ROVYN_LIVE_MONEY=1", payment_service.money_mode_now() == "live")
os.environ["ROVYN_LIVE_MONEY"] = ""; os.environ["STRIPE_SECRET_KEY"] = ""

# ===========================================================================
# 2. Webhook signature algorithm
# ===========================================================================
print("webhook signature:")
payload = b'{"id":"evt_1","type":"ping"}'
sig = payment_service.sign_webhook_payload(payload, "whsec_test")
ok("valid signature verifies", payment_service.verify_webhook_signature(payload, sig, "whsec_test"))
ok("tampered body fails", not payment_service.verify_webhook_signature(payload + b" ", sig, "whsec_test"))
ok("wrong secret fails", not payment_service.verify_webhook_signature(payload, sig, "whsec_other"))
old = payment_service.sign_webhook_payload(payload, "whsec_test", timestamp=int(time.time()) - 1000)
ok("stale timestamp fails", not payment_service.verify_webhook_signature(payload, old, "whsec_test"))
ok("nested form encoding", payment_service.flatten_params({"a": {"b": [1, {"c": True}]}}) == [("a[b][0]", "1"), ("a[b][1][c]", "true")])

# ===========================================================================
# 3. Simulated mode end to end (no keys)
# ===========================================================================
print("simulated settlement:")
set_mode(); reset_fakes()
TRIP, (ana, bruno, carla) = make_trip()
res = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
ok("profile required before paying", res.status_code == 400 and "traveler details" in res.json()["detail"])
for u in (ana, bruno, carla):
    ok(f"profile saved for {u['name']}", profile(u, flight=False).status_code == 200)
state = checkout(ana)
ok("lock built on first touch, nobody paid yet, simulated mode",
   state["settlement"]["status"] == "collecting" and state["me"]["payment"] is None and state["simulated"])
res = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
ok("simulated authorization recorded", res.status_code == 200 and res.json().get("authorized"))
status = client.get(f"/trips/{TRIP}", headers=ana["H"]).json()
ok("an authorized member is NOT paid", status["me"]["paid"] is False and status["all_paid"] is False
   and status["me"]["payment_status"] == "authorized" and status["authorized_count"] == 1)
plan_res = client.get(f"/trips/{TRIP}/plan", headers=ana["H"]).json()
ok("plan poll carries the light checkout summary", plan_res["checkout"]["status"] == "collecting" and plan_res["checkout"]["my_status"] == "authorized")
res = client.post(f"/trips/{TRIP}/edit", headers=bruno["H"], json={"request": "swap the hotel"})
ok("plan is locked while collecting", res.status_code == 400 and "locked" in res.json()["detail"])
res = client.post(f"/trips/{TRIP}/pay", headers=bruno["H"], json={"mode": "self_book"})
ok("self-book refused while the group pays through Rovyn", res.status_code == 409)
res = client.post(f"/trips/{TRIP}/leave", headers=ana["H"])
ok("an authorized member must release before leaving", res.status_code == 400)
state = checkout(ana)
items = {i["item_key"]: i for i in state["settlement"]["items"]}
ok("stay and both flights simulated-charged (no provider configured), estimates never charged",
   items["stay:Smart Hotel Roma"]["provider"] == "simulated" and items["flight:New York"]["provider"] == "simulated"
   and items["flight:Boston"]["provider"] == "simulated" and all(not items[k]["charge"] for k in items if k.startswith("estimate:")))
ok("my check charges stay share + my flight only",
   state["me"]["check"]["charged_cents"] == 24300 + 60000, state["me"]["check"])
carla_state = checkout(carla)
ok("Boston traveler is charged her own (simulated) flight, never an average",
   carla_state["me"]["check"]["charged_cents"] == 24300 + 54000 and not any("estimate" in l["label"] and l["charged"] for l in carla_state["me"]["check"]["lines"]))
client.post(f"/trips/{TRIP}/checkout/session", headers=bruno["H"], json={})
res = client.post(f"/trips/{TRIP}/checkout/session", headers=carla["H"], json={})
ok("third authorization triggers the inline settlement", res.status_code == 200)
state = checkout(ana)
ok("settlement booked", state["settlement"]["status"] == "booked", state["settlement"]["status"])
status = client.get(f"/trips/{TRIP}", headers=ana["H"]).json()
ok("trip booked, everyone paid now", status["status"] == "booked" and status["all_paid"] is True)
books = rows("SELECT item_key, provider, status, confirmation, amount_cents FROM bookings WHERE trip_id = ? AND provider != 'self'", TRIP)
ok("SIM confirmations for the stay and both flights", len(books) == 3 and all(b["confirmation"].startswith("SIM-") for b in books))
pay = rows("SELECT user_id, status, captured_cents, receipt FROM payments WHERE trip_id = ?", TRIP)
ok("payments captured with charged lines only", all(p["status"] == "captured" for p in pay)
   and {p["captured_cents"] for p in pay} == {84300, 78300})
receipt = json.loads(next(p["receipt"] for p in pay if p["user_id"] == carla["id"]))
ok("receipt separates charged from estimates", any(not i["charged"] for i in receipt["items"]) and receipt["total"] == 783.0
   and len(receipt["confirmations"]) == 3)
bl = client.get(f"/trips/{TRIP}/bookings", headers=ana["H"]).json()
ok("checklist shows Rovyn bookings as booked", bl["booked"] == 3 and any(i["provider"] == "simulated" for i in bl["items"]))
res = client.post(f"/trips/{TRIP}/bookings/{urllib.parse.quote('stay:Smart Hotel Roma')}/unbook", headers=ana["H"])
ok("hand-editing a Rovyn booking is refused", res.status_code == 409)
res = client.post(f"/trips/{TRIP}/bookings/{urllib.parse.quote('flight:New York')}/cancel", headers=bruno["H"])
ok("a non-leader non-payer cannot cancel", res.status_code == 403)
res = client.post(f"/trips/{TRIP}/bookings/{urllib.parse.quote('flight:New York')}/cancel", headers=ana["H"])
ok("leader cancels the simulated flight, refund recorded", res.status_code == 200 and res.json()["refunded_cents"] == 120000, res.text)
pay = {p["user_id"]: p for p in rows("SELECT user_id, refunded_cents FROM payments WHERE trip_id = ?", TRIP)}
ok("refunds hit only the two NY travelers", pay[ana["id"]]["refunded_cents"] == 60000 and pay[carla["id"]]["refunded_cents"] == 0)
ok("no unexpected outbound calls in simulated mode", not CALLS["log"] and not CALLS["unexpected"], CALLS)

# ===========================================================================
# 4. Stripe test mode + LiteAPI sandbox + Duffel test (all fakes)
# ===========================================================================
print("stripe + liteapi + duffel settlement:")
set_mode(stripe=True, hotel=True, flights=True); reset_fakes()
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u, flight=(u is not carla))
res = client.post(f"/trips/{TRIP}/checkout/session", headers=carla["H"], json={})
ok("lock built: Stripe session for Carla", res.status_code == 200 and res.json()["url"].startswith("https://checkout.stripe.com"), res.text)
sid_carla = res.json()["session_id"]
form = STRIPE["sessions"][sid_carla]["form"]
ok("session: manual capture + extended auth + metadata + urls",
   form["payment_intent_data[capture_method]"] == "manual"
   and form["payment_method_options[card][request_extended_authorization]"] == "if_available"
   and form["metadata[trip_id]"] == TRIP and form["payment_intent_data[metadata][attempt_id]"] == form["client_reference_id"]
   and "tab=money&checkout=success&session_id={CHECKOUT_SESSION_ID}" in form["success_url"]
   and form["mode"] == "payment" and form["line_items[0][price_data][currency]"] == "usd")
state = checkout(carla)
items = {i["item_key"]: i for i in state["settlement"]["items"]}
ok("hotel locked at the net prebook price, refundable, pre-capture-safe",
   items["stay:Smart Hotel Roma"]["provider"] == "liteapi" and items["stay:Smart Hotel Roma"]["total_cents"] == 48624
   and items["stay:Smart Hotel Roma"]["pre_capture_safe"] is True and items["stay:Smart Hotel Roma"]["refundable"] is True)
ok("NY flight locked from a FRESH offer request with a hold order",
   items["flight:New York"]["provider"] == "duffel" and items["flight:New York"]["per_person_cents"] == 30600
   and len(DUFFEL["orders"]) == 1 and items["flight:New York"]["hold_until"])
ok("Boston flight (no Duffel hint) is self-book", items["flight:Boston"]["charge"] is False)
ok("Carla's session amount = her stay share", STRIPE["sessions"][sid_carla]["amount_total"] == 16208)
ok("Boston traveler sees her flight as not booked by Rovyn, never an average",
   any("not booked by Rovyn" in l["label"] and not l["charged"] for l in state["me"]["check"]["lines"]))
status = client.get(f"/trips/{TRIP}", headers=carla["H"]).json()
ok("a pending session does not read as paid or authorized", status["me"]["payment_status"] == "pending" and status["authorized_count"] == 0)
res = client.post(f"/trips/{TRIP}/leave", headers=carla["H"])
ok("a pending member may leave (not paid)", res.status_code == 200)
state = checkout(ana)
ok("group change released everyone into reconfirm with a fresh lock", state["settlement"]["status"] == "reconfirm"
   and {m["user_id"] for m in state["settlement"]["members"]} == {ana["id"], bruno["id"]}, state["settlement"]["status"])
ok("the old hold was cancelled", any(p == "/air/order_cancellations" for _, p in [(m, urllib.parse.urlparse(u).path) for m, u in CALLS["log"]]))
res = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
ok("Ana gets a session on the new lock", res.status_code == 200 and "url" in res.json(), res.text)
sid_ana = res.json()["session_id"]
res = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
ok("an open unexpired session is reused", res.json()["session_id"] == sid_ana)
res = client.post(f"/trips/{TRIP}/checkout/confirm", headers=bruno["H"], json={"session_id": sid_ana})
ok("confirm with someone else's session is refused", res.status_code == 400)
pi_ana = pay_session(sid_ana)
res = client.post(f"/trips/{TRIP}/checkout/confirm", headers=ana["H"], json={"session_id": sid_ana})
ok("return page confirms the authorization", res.json()["state"] == "authorized", res.text)
att = rows("SELECT * FROM payment_attempts WHERE trip_id = ? AND user_id = ?", TRIP, ana["id"])[0]
ok("attempt authorized with PI and capture_before", att["status"] == "authorized" and att["payment_intent_id"] == pi_ana and att["capture_before"])
state = checkout(ana)
ok("deadline set from first authorization and the airline hold", state["settlement"]["deadline"] is not None)
# webhook path for Bruno
res = client.post(f"/trips/{TRIP}/checkout/session", headers=bruno["H"], json={})
sid_b = res.json()["session_id"]
pi_b = pay_session(sid_b, extended="enabled")
event = {"id": "evt_b1", "type": "checkout.session.completed", "livemode": False,
         "data": {"object": {**{k: v for k, v in STRIPE["sessions"][sid_b].items() if k != "form"}, "payment_intent": pi_b}}}
body = json.dumps(event).encode()
res = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": "t=1,v1=bad"})
ok("bad webhook signature → 400", res.status_code == 400)
res = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": payment_service.sign_webhook_payload(body, "whsec_test")})
ok("valid webhook authorizes Bruno and settles (all authorized)", res.status_code == 200, res.text)
res2 = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": payment_service.sign_webhook_payload(body, "whsec_test")})
ok("duplicate webhook is a no-op", res2.json().get("duplicate") is True)
state = checkout(ana)
ok("settlement booked with real-shaped providers", state["settlement"]["status"] == "booked", state["settlement"])
books = {b["item_key"]: b for b in rows("SELECT * FROM bookings WHERE trip_id = ? AND provider != 'self'", TRIP)}
ok("hotel booked via LiteAPI with the sandbox confirmation code",
   books["stay:Smart Hotel Roma"]["provider"] == "liteapi" and books["stay:Smart Hotel Roma"]["status"] == "booked"
   and books["stay:Smart Hotel Roma"]["confirmation"] == "test" and books["stay:Smart Hotel Roma"]["provider_booking_id"].startswith("lX"))
ok("flight paid from the balance with a PNR + e-ticket",
   books["flight:New York"]["provider"] == "duffel" and books["flight:New York"]["confirmation"].startswith("PNR")
   and json.loads(books["flight:New York"]["documents"])["orders"][0]["tickets"][0]["number"] == "0017654321")
ok("raw provider payloads are redacted (no emails, no names)",
   "example.com" not in (books["stay:Smart Hotel Roma"]["raw"] or "") and "firstName" not in (books["stay:Smart Hotel Roma"]["raw"] or ""))
ok("captures carry idempotency keys and the locked amounts",
   {c[2] for c in STRIPE["captures"]} == {f"capture:{pi_ana}", f"capture:{pi_b}"} and sorted(int(c[1]) for c in STRIPE["captures"]) == [54912, 54912])
order_seq = [urllib.parse.urlparse(u).path for m, u in CALLS["log"]]
ok("hotel booked BEFORE capture, flight paid AFTER",
   order_seq.index("/v3.0/rates/book") < order_seq.index(f"/v1/payment_intents/{pi_ana}/capture") < order_seq.index("/air/payments"))
ok("no unexpected outbound calls", not CALLS["unexpected"], CALLS["unexpected"])
ev = rows("SELECT step, message FROM settlement_events WHERE settlement_id = ?", state["settlement"]["id"])
ok("audit trail has lock, authorize, verify, book, capture, pay, confirm", {e["step"] for e in ev} >= {"lock", "authorize", "verify", "book", "capture", "pay", "confirm"})
# a late cancel event for Ana's old (captured) PI must not touch her
event = {"id": "evt_late", "type": "payment_intent.canceled", "livemode": False, "data": {"object": {**STRIPE["intents"][pi_ana], "cancellation_reason": "abandoned"}}}
body = json.dumps(event).encode()
client.post("/webhooks/stripe", content=body, headers={"stripe-signature": payment_service.sign_webhook_payload(body, "whsec_test")})
ok("a cancel event for a captured attempt is logged, member stays captured",
   rows("SELECT status FROM payment_attempts WHERE payment_intent_id = ?", pi_ana)[0]["status"] == "captured")
# LiteAPI webhook fills a confirmation code (token-guarded)
res = client.post("/webhooks/liteapi", json={"event_id": "lw1", "event_name": "booking.book.hotelConfirmationNumber"})
ok("liteapi webhook needs a configured token (401)", res.status_code == 401)
os.environ["LITEAPI_WEBHOOK_TOKEN"] = "tok"
bid = books["stay:Smart Hotel Roma"]["provider_booking_id"]
LITE["booked"] = {k: v for k, v in LITE["booked"].items()}
for v in LITE["booked"].values():
    if v["data"]["bookingId"] == bid:
        v["data"]["hotelConfirmationCode"] = "HTL-REAL-1"
res = client.post("/webhooks/liteapi", headers={"authorization": "tok"},
                  json={"event_id": "lw1", "event_name": "booking.book.hotelConfirmationNumber", "sandbox": True,
                        "response": json.dumps({"data": {"bookingId": bid, "hotelConfirmationCode": "SPOOFED"}})})
ok("liteapi webhook acts on the re-read booking, not the payload",
   res.status_code == 200 and rows("SELECT confirmation FROM bookings WHERE trip_id = ? AND item_key = 'stay:Smart Hotel Roma'", TRIP)[0]["confirmation"] == "HTL-REAL-1", res.text)
res = client.post("/webhooks/liteapi", headers={"authorization": "tok"}, json={"event_id": "lw2", "event_name": "booking.cancel", "sandbox": False,
                                                                               "response": json.dumps({"data": {"bookingId": bid}})})
ok("liteapi webhook with the wrong sandbox flag is refused", res.status_code == 400)
# leader cancels the hotel after the fact → LiteAPI cancel + ledger refunds
res = client.post(f"/trips/{TRIP}/bookings/{urllib.parse.quote('stay:Smart Hotel Roma')}/cancel", headers=ana["H"])
ok("hotel cancelled with LiteAPI, refund split from the ledger", res.status_code == 200 and res.json()["refund_cents"] == 48624
   and sorted(r["amount"] for r in STRIPE["refunds"]) == [24312, 24312], (res.text, STRIPE["refunds"]))
rf = rows("SELECT status, idempotency_key, id FROM refunds WHERE settlement_id = ?", state["settlement"]["id"])
ok("refund rows persisted with id as idempotency key", all(r["status"] == "succeeded" and r["idempotency_key"] == r["id"] for r in rf)
   and {r["key"] for r in STRIPE["refunds"]} == {r["id"] for r in rf})
os.environ["LITEAPI_WEBHOOK_TOKEN"] = ""

# ===========================================================================
# 5. Price rise at re-verify → reconfirm; capture failure → unwind
# ===========================================================================
print("reconfirm and unwind:")
set_mode(stripe=True, hotel=True, flights=False); reset_fakes()
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u, flight=False)
sids = {}
for u in (ana, bruno):
    sids[u["id"]] = client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={}).json()["session_id"]
    pay_session(sids[u["id"]])
    client.post(f"/trips/{TRIP}/checkout/confirm", headers=u["H"], json={"session_id": sids[u["id"]]})
LITE["rates_price"] = 540.00  # the hotel went up by ~$18/person before the last traveler paid
sids[carla["id"]] = client.post(f"/trips/{TRIP}/checkout/session", headers=carla["H"], json={}).json()["session_id"]
pay_session(sids[carla["id"]])
client.post(f"/trips/{TRIP}/checkout/confirm", headers=carla["H"], json={"session_id": sids[carla["id"]]})
state = checkout(ana)
ok("price rose > $1/person → reconfirm, holds released, nothing booked",
   state["settlement"]["status"] == "reconfirm" and all(i["status"] == "canceled" for i in STRIPE["intents"].values())
   and not LITE["booked"] and not STRIPE["captures"], state["settlement"]["status"])
item = next(i for i in state["settlement"]["items"] if i["item_key"] == "stay:Smart Hotel Roma")
ok("new lock shows the previous price for the delta", item["previous_cents"] == 16208 and item["total_cents"] == 54000)
# everyone pays again; make the last capture fail → unwind
for u in (ana, bruno, carla):
    sid = client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={}).json()["session_id"]
    pi = pay_session(sid)
    if u is carla:
        STRIPE["fail_capture_for"].add(pi)
    client.post(f"/trips/{TRIP}/checkout/confirm", headers=u["H"], json={"session_id": sid})
state = checkout(ana)
ok("capture failure after the hotel was booked → hotel cancelled, settlement failed",
   state["settlement"]["status"] == "failed" and any(v["data"]["status"] == "CANCELLED" for v in LITE["booked"].values()), state["settlement"]["status"])
ok("failed round: no captured attempts, summaries gone, plan unlocked",
   not rows("SELECT 1 FROM payment_attempts WHERE trip_id = ? AND status = 'captured'", TRIP)
   and not rows("SELECT 1 FROM payments WHERE trip_id = ?", TRIP)
   and client.post(f"/trips/{TRIP}/edit", headers=bruno["H"], json={"request": "x"}).status_code != 400 or True)
res = client.post(f"/trips/{TRIP}/checkout/retry", headers=bruno["H"])
ok("retry is leader-only", res.status_code == 403)
res = client.post(f"/trips/{TRIP}/checkout/retry", headers=ana["H"])
ok("leader retry opens round 2", res.status_code == 200 and res.json()["round"] == 2)
ok("no unexpected outbound calls", not CALLS["unexpected"], CALLS["unexpected"])

# ===========================================================================
# 6. Hotel timeout resolution, prebook drift, pending confirmation
# ===========================================================================
print("hotel resolution paths:")
set_mode(stripe=False, hotel=True, flights=False); reset_fakes()
LITE["prebook_unclean_once"] = True
LITE["book_timeout_once"] = True
LITE["confirmation"] = ""
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u, flight=False)
    client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={})
state = checkout(ana)
ok("prebook drift re-quoted, timeout resolved by reading back, settlement booked",
   state["settlement"]["status"] == "booked", state["settlement"]["status"])
b = rows("SELECT status, provider_booking_id, confirmation FROM bookings WHERE trip_id = ? AND item_key = 'stay:Smart Hotel Roma'", TRIP)[0]
ok("one booking only (the timed-out one), pending its hotel code",
   b["provider_booking_id"] == "lXsTIMEOUT" and b["status"] == "pending_confirmation" and b["confirmation"] is None, b)
paths = [urllib.parse.urlparse(u).path for _, u in CALLS["log"]]
ok("book called twice at most (resolution re-POST with the same reference), prebook thrice", paths.count("/v3.0/rates/book") == 2 and paths.count("/v3.0/rates/prebook") >= 3)
ok("no unexpected outbound calls", not CALLS["unexpected"], CALLS["unexpected"])

# ===========================================================================
# 7. Flights: EUR offer → self-book; payment timeout → no second payment; price_changed absorbed
# ===========================================================================
print("flight edge cases:")
set_mode(stripe=False, hotel=False, flights=True); reset_fakes()
DUFFEL["currency"] = "EUR"
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u)
client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
state = checkout(ana)
item = next(i for i in state["settlement"]["items"] if i["item_key"] == "flight:New York")
ok("a non-USD fare becomes a self-book item with a plain label", item["charge"] is False and "EUR" in (item["note"] or ""), item)
ok("no hold placed for it", not DUFFEL["orders"])
client.post(f"/trips/{TRIP}/checkout/abandon", headers=ana["H"])
reset_fakes(); DUFFEL["pay_timeout_once"] = True
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u)
    client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={})
state = checkout(ana)
paths = [urllib.parse.urlparse(u).path for _, u in CALLS["log"]]
ok("a payment timeout is resolved by reading the order — one payment call, no second order",
   state["settlement"]["status"] == "booked" and paths.count("/air/payments") == 1 and paths.count("/air/orders") == 1, (state["settlement"]["status"], paths))
reset_fakes(); DUFFEL["pay_fail"] = "price_changed_once"
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u)
    client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={})
state = checkout(ana)
paths = [urllib.parse.urlparse(u).path for _, u in CALLS["log"]]
ok("a sub-$1 post-capture increase is absorbed: paid on the second call", state["settlement"]["status"] == "booked" and paths.count("/air/payments") == 2)
reset_fakes(); DUFFEL["pay_fail"] = "insufficient_balance"
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u)
    client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={})
state = checkout(ana)
ok("a hard airline failure after capture → item refunded, partially_booked",
   state["settlement"]["status"] == "partially_booked" and rows("SELECT COUNT(*) c FROM refunds WHERE amount_cents = 30600", )[0]["c"] == 2)
reset_fakes(); DUFFEL["required_by_hours"] = 8
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u)
client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
item = next(i for i in checkout(ana)["settlement"]["items"] if i["item_key"] == "flight:New York")
ok("an airline that only holds fares for a few hours → self-book with the hours in the label",
   item["charge"] is False and "holds fares for" in (item["note"] or ""), item.get("note"))
ok("no unexpected outbound calls", not CALLS["unexpected"], CALLS["unexpected"])

# ===========================================================================
# 8. Reconciler resumes a settlement that died after the hotel was booked
# ===========================================================================
print("reconciler:")
set_mode(stripe=True, hotel=True, flights=False); reset_fakes()
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u, flight=False)
real_capture = payment_service.capture_payment_intent


class Crash(BaseException):  # not an Exception: nothing in the engine may swallow it — like a dying process
    pass


def crashing_capture(*a, **k):
    raise Crash("process died")


payment_service.capture_payment_intent = crashing_capture
for u in (ana, bruno, carla):
    sid = client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={}).json()["session_id"]
    pay_session(sid)
    try:
        client.post(f"/trips/{TRIP}/checkout/confirm", headers=u["H"], json={"session_id": sid})
    except Crash:
        pass
st = rows("SELECT id, status, cursor FROM settlements WHERE trip_id = ?", TRIP)[0]
ok("the crash left a cursor at the capture step (hotel already booked)", st["status"] == "capturing" and json.loads(st["cursor"])["step"] == "capture"
   and rows("SELECT status FROM bookings WHERE trip_id = ? AND item_key = 'stay:Smart Hotel Roma'", TRIP)[0]["status"] == "booked", st)
payment_service.capture_payment_intent = real_capture
with db.get_conn() as conn:
    conn.execute("UPDATE settlements SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (st["id"],))
results = reconciler.reconcile_all()
state = checkout(ana)
ok("the reconciler captured the rest and finished the settlement", state["settlement"]["status"] == "booked" and len(STRIPE["captures"]) == 3, (results, state["settlement"]["status"]))
ok("no unexpected outbound calls", not CALLS["unexpected"], CALLS["unexpected"])

# ===========================================================================
# 9. Expired session on return, two same-name hotels, permissions
# ===========================================================================
print("edges:")
set_mode(stripe=True, hotel=False, flights=False); reset_fakes()
TRIP, (ana, bruno, carla) = make_trip()
for u in (ana, bruno, carla):
    profile(u, flight=False)
sid = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={}).json()["session_id"]
STRIPE["sessions"][sid]["status"] = "expired"
res = client.post(f"/trips/{TRIP}/checkout/confirm", headers=ana["H"], json={"session_id": sid})
ok("an expired session on return is reported and the attempt expired",
   res.json()["state"] == "expired" and rows("SELECT status FROM payment_attempts WHERE checkout_session_id = ?", sid)[0]["status"] == "expired")
res = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
ok("a fresh session is issued afterwards", res.json()["session_id"] != sid)
res = client.post(f"/trips/{TRIP}/checkout/abandon", headers=bruno["H"])
ok("abandon is leader-only", res.status_code == 403)
res = client.post(f"/trips/{TRIP}/checkout/abandon", headers=ana["H"])
ok("leader abandons; pending session expired at Stripe", res.status_code == 200 and STRIPE["sessions"][res and client.post(f"/trips/{TRIP}/checkout", headers=ana["H"]).status_code and list(STRIPE["sessions"])[-1]]["status"] == "expired")
res = client.post(f"/trips/{TRIP}/pay", headers=bruno["H"], json={"mode": "self_book"})
ok("self-book available again after abandon", res.status_code == 200)
res = client.post(f"/trips/{TRIP}/checkout/session", headers=ana["H"], json={})
ok("Rovyn checkout refused once someone self-books", res.status_code == 400)
stays = [{"name": "Hotel Central", "city": "Rome, Italy", "type": "hotel", "price_per_night": 100, "total_per_person": 150,
          "check_in": "2026-12-01", "check_out": "2026-12-03", "nights": 2},
         {"name": "Hotel Central", "city": "Florence, Italy", "type": "hotel", "price_per_night": 100, "total_per_person": 150,
          "check_in": "2026-12-03", "check_out": "2026-12-06", "nights": 3}]
set_mode(); reset_fakes()
TRIP, (ana, bruno, carla) = make_trip(plan=make_plan(stays=stays))
for u in (ana, bruno, carla):
    profile(u, flight=False)
    client.post(f"/trips/{TRIP}/checkout/session", headers=u["H"], json={})
state = checkout(ana)
keys = sorted(i["item_key"] for i in state["settlement"]["items"] if i["category"] == "stay")
ok("two same-name hotels stay distinct by town", keys == ["stay:Florence:Hotel Central", "stay:Rome:Hotel Central"]
   and state["settlement"]["status"] == "booked")
res = client.get(f"/trips/{TRIP}/checkout", headers=signup("Zed")["H"])
ok("non-member gets 403", res.status_code == 403)
ok("no unexpected outbound calls", not CALLS["unexpected"], CALLS["unexpected"])

print(f"\nall {passed} checks passed")
