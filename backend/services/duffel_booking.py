"""Duffel flight orders for the booking prototype.

Duffel sells under its own IATA/ARC accreditation; Rovyn collects the
traveler's money on its own Stripe and pays Duffel from a prepaid balance
(``payments[].type = "balance"``). Test tokens (``duffel_test_…``) have an
unlimited balance, so the entire sequence runs for free:

    GET  /air/offers/{id}              re-read the offer the board stored
    POST /air/offer_requests           re-search when it expired (same slices)
    POST /air/orders  type=hold        reserve without money, when the airline allows it
    POST /air/payments type=balance    pay the held order after the cards are captured
    POST /air/orders  type=instant     instant-only offers: created after capture
    POST /air/order_cancellations      two-step cancellation, refund lands in the balance

Orders and payments may take airlines two minutes; they are never retried
blindly — a timeout is resolved by reading the order back.

The request helper is private to this module so it can be swapped for the
shared Duffel client from the options-breadth work without touching callers.
"""
import gzip
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_DUFFEL_BASE = "https://api.duffel.com"
ORDER_TIMEOUT = 130  # airlines may take ~120 s to confirm


class DuffelError(Exception):
    def __init__(self, message: str, code: str | None = None, status: int | None = None,
                 request_id: str | None = None):
        super().__init__(message)
        self.code, self.status, self.request_id = code, status, request_id


def duffel_token() -> str:
    return (os.environ.get("DUFFEL_ACCESS_TOKEN") or "").strip()


def duffel_mode() -> str:
    token = duffel_token()
    if not token:
        return "off"
    return "test" if token.startswith("duffel_test_") else "live"


def duffel_available() -> bool:
    return duffel_mode() != "off"


def assert_live_mode(obj: dict, money_mode: str, what: str = "Duffel object") -> None:
    """A test-mode settlement must only ever touch test-mode orders, and vice versa."""
    if not isinstance(obj, dict) or "live_mode" not in obj:
        return
    if bool(obj.get("live_mode")) != (money_mode == "live"):
        raise DuffelError(f"{what} live_mode={obj.get('live_mode')} does not match settlement money mode {money_mode}", "mode_mismatch")


def _duffel_request(method: str, path: str, body: dict | None = None,
                    params: dict | None = None, timeout: int = 30):
    """One Duffel call: Bearer token, Duffel-Version v2, gzip-aware, the
    ``{"data": …}`` envelope unwrapped and ``errors[]`` raised as DuffelError."""
    url = _DUFFEL_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {
        "Authorization": f"Bearer {duffel_token()}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "Rovyn/1.0 (group trip planner)",
    }
    data = None
    if body is not None:
        data = json.dumps({"data": body}).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    def decode(res) -> dict:
        raw = res.read()
        if (res.headers.get("Content-Encoding") or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode() or "{}")

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as res:
            payload = decode(res)
    except urllib.error.HTTPError as exc:
        try:
            payload = decode(exc)
        except Exception:
            payload = {}
        errors = payload.get("errors") or [{}]
        first = errors[0] if errors else {}
        raise DuffelError(first.get("message") or f"Duffel HTTP {exc.code}", first.get("code"),
                          exc.code, (payload.get("meta") or {}).get("request_id")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Transport failure: the outcome is unknown — callers read the order back, never retry.
        raise DuffelError(f"Duffel unreachable: {exc}", "network", None) from None
    if payload.get("errors"):
        first = payload["errors"][0]
        raise DuffelError(first.get("message") or "Duffel error", first.get("code"), None,
                          (payload.get("meta") or {}).get("request_id"))
    return payload.get("data")


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------

def get_offer(offer_id: str) -> dict:
    return _duffel_request("GET", f"/air/offers/{offer_id}", params={"return_available_services": "false"})


def offer_expired(offer: dict, margin_seconds: int = 120) -> bool:
    expires = (offer or {}).get("expires_at") or ""
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc).timestamp() + margin_seconds >= when.timestamp()


def search_offers(slices: list[dict], passengers: int, cabin_class: str = "economy",
                  max_connections: int = 1, supplier_timeout_ms: int = 20000) -> dict:
    """A fresh offer request for the same itinerary shape. Returns the offer
    request (``id``, ``passengers[].id``, ``offers[]``)."""
    body = {
        "slices": [{"origin": s["origin"], "destination": s["destination"],
                    "departure_date": s["departure_date"]} for s in slices],
        "passengers": [{"type": "adult"} for _ in range(max(1, min(int(passengers), 9)))],
        "cabin_class": cabin_class,
        "max_connections": max_connections,
    }
    return _duffel_request("POST", "/air/offer_requests", body,
                           params={"return_offers": "true", "supplier_timeout": supplier_timeout_ms},
                           timeout=60) or {}


def itinerary_signature(offer: dict) -> str:
    """Carrier + flight number + departure for every segment — the identity of
    an itinerary across searches and suppliers."""
    parts = []
    for sl in offer.get("slices") or []:
        for seg in sl.get("segments") or []:
            carrier = ((seg.get("marketing_carrier") or {}).get("iata_code") or "")
            number = seg.get("marketing_carrier_flight_number") or ""
            parts.append(f"{carrier}{number}@{(seg.get('departing_at') or '')[:16]}")
    return "|".join(sorted(parts))


def per_person_amount(offer: dict) -> float:
    total = float(offer.get("total_amount") or 0)
    n = max(1, len(offer.get("passengers") or []))
    return total / n


def operating_carriers(offer: dict) -> list[str]:
    names = []
    for sl in offer.get("slices") or []:
        for seg in sl.get("segments") or []:
            name = (seg.get("operating_carrier") or {}).get("name")
            if name and name not in names:
                names.append(name)
    return names


# ---------------------------------------------------------------------------
# Orders & payments
# ---------------------------------------------------------------------------

def passenger_payload(passenger_id: str, profile: dict, email: str) -> dict:
    return {
        "id": passenger_id,
        "given_name": profile.get("given_name") or "",
        "family_name": profile.get("family_name") or "",
        "born_on": profile.get("born_on") or "",
        "gender": profile.get("gender") or "",
        "title": profile.get("title") or "",
        "email": email,
        "phone_number": profile.get("phone") or "",
    }


def create_hold_order(offer_id: str, passengers: list[dict], metadata: dict) -> dict:
    body = {"type": "hold", "selected_offers": [offer_id], "passengers": passengers,
            "metadata": metadata}
    return _duffel_request("POST", "/air/orders", body, timeout=ORDER_TIMEOUT)


def create_instant_order(offer_id: str, passengers: list[dict], amount: str, currency: str,
                         metadata: dict) -> dict:
    body = {"type": "instant", "selected_offers": [offer_id], "passengers": passengers,
            "payments": [{"type": "balance", "amount": amount, "currency": currency}],
            "metadata": metadata}
    return _duffel_request("POST", "/air/orders", body, timeout=ORDER_TIMEOUT)


def pay_order_from_balance(order_id: str, amount: str, currency: str) -> dict:
    body = {"order_id": order_id, "payment": {"type": "balance", "amount": amount, "currency": currency}}
    return _duffel_request("POST", "/air/payments", body, timeout=ORDER_TIMEOUT)


def get_order(order_id: str) -> dict:
    return _duffel_request("GET", f"/air/orders/{order_id}")


def list_recent_orders(limit: int = 200) -> list[dict]:
    """The account's most recent orders (newest first) — used to find a hold
    whose creation call timed out, by the metadata Rovyn sent with it."""
    return _duffel_request("GET", "/air/orders", params={"limit": limit, "sort": "-created_at"}) or []


def cancel_order(order_id: str) -> dict:
    """Two-step: create the pending cancellation, then confirm it. The refund
    goes to Rovyn's balance — the traveler is refunded on Stripe by us."""
    pending = _duffel_request("POST", "/air/order_cancellations", {"order_id": order_id}, timeout=60)
    cid = (pending or {}).get("id")
    if not cid:
        raise DuffelError("Duffel returned no cancellation id")
    return _duffel_request("POST", f"/air/order_cancellations/{cid}/actions/confirm", {}, timeout=60)


def order_documents(order: dict) -> dict:
    tickets = [{"number": d.get("unique_identifier"), "type": d.get("type"),
                "passenger_ids": d.get("passenger_ids") or []}
               for d in order.get("documents") or [] if d.get("unique_identifier")]
    return {"booking_reference": order.get("booking_reference"), "tickets": tickets,
            "operated_by": operating_carriers(order), "order_id": order.get("id")}
