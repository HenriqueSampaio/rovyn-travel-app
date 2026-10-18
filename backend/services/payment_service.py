"""Stripe over the standard library — Rovyn's side of the money.

Rovyn is the merchant of record: each traveler pays their share to Rovyn by
card on a Stripe-hosted Checkout page, and Rovyn buys from suppliers itself.
Three rules shape everything here (see docs/PAYMENTS-MOR-SPEC.md):

- Cards are AUTHORIZED, not charged, until every traveler has committed
  (``capture_method=manual``). Cancelling an authorization is free; refunding
  a captured charge forfeits Stripe's processing fee.
- Card data never touches this server — Checkout is hosted by Stripe, which
  keeps Rovyn in PCI SAQ A scope.
- Without ``STRIPE_SECRET_KEY`` every call has a *simulated* twin so the whole
  flow is demonstrable with no money and no account, clearly labeled as such.

Real-money credentials are refused at boot unless ``ROVYN_LIVE_MONEY=1``
(allow-list: only ``sk_test_`` / ``sand_`` / ``duffel_test_`` or empty pass
without it) — the legal and operational prerequisites are in docs/MOR-RUNBOOK.md.
"""
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_STRIPE_BASE = "https://api.stripe.com"
DEFAULT_API_VERSION = "2026-09-30.endive"
# Stripe's default validity of an online card authorization. Extended
# authorizations (up to 30 days) need interchange-plus access, so the group
# window is planned around this figure unless Stripe says otherwise per charge.
AUTH_HOLD_DAYS = 7
_RETRYABLE = (409, 429, 500, 502, 503, 504)


class StripeError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None,
                 kind: str | None = None, param: str | None = None):
        super().__init__(message)
        self.status, self.code, self.kind, self.param = status, code, kind, param


# ---------------------------------------------------------------------------
# Configuration & the live-money guard
# ---------------------------------------------------------------------------

def stripe_key() -> str:
    return (os.environ.get("STRIPE_SECRET_KEY") or "").strip()


def stripe_mode() -> str:
    """'off' (simulated), 'test' (sk_test_…) or 'live' (sk_live_…)."""
    key = stripe_key()
    if not key:
        return "off"
    return "live" if key.startswith("sk_live_") else "test"


def stripe_available() -> bool:
    return stripe_mode() != "off"


def provider_mode() -> str:
    """How money moves in this deployment: simulated | test | live."""
    mode = stripe_mode()
    return "simulated" if mode == "off" else mode


def live_money_allowed() -> bool:
    return (os.environ.get("ROVYN_LIVE_MONEY") or "").strip() == "1"


# Allow-list, not deny-list: without the explicit opt-in only credentials that
# provably cannot move money are accepted. Empty means "simulated".
_TEST_PREFIXES = {
    "STRIPE_SECRET_KEY": ("sk_test_",),
    "LITEAPI_BOOK_API_KEY": ("sand_",),
    "DUFFEL_ACCESS_TOKEN": ("duffel_test_",),
}


def offending_credentials() -> list[str]:
    """Which configured credentials are NOT provably test credentials."""
    found = []
    for name, prefixes in _TEST_PREFIXES.items():
        value = (os.environ.get(name) or "").strip()
        if value and not value.startswith(prefixes):
            found.append(f"{name} is not a {' / '.join(prefixes)} test credential")
    if (os.environ.get("LITEAPI_BOOK_MODE") or "").strip().lower() == "live":
        found.append("LITEAPI_BOOK_MODE=live")
    return found


def refuse_live_keys() -> None:
    """Called at import of backend.main (and directly by tests): real-money
    credentials need ROVYN_LIVE_MONEY=1, set deliberately after the runbook."""
    problems = offending_credentials()
    if problems and not live_money_allowed():
        raise RuntimeError(
            "Refusing to start with real-money credentials: " + "; ".join(problems)
            + ". Complete docs/MOR-RUNBOOK.md, then set ROVYN_LIVE_MONEY=1 deliberately."
        )


enforce_live_money_guard = refuse_live_keys  # older name


def money_mode_now() -> str:
    """The one money mode this process runs in: simulated | test | live."""
    if offending_credentials() and live_money_allowed():
        return "live"
    return "test" if stripe_key().startswith("sk_test_") else "simulated"


def assert_livemode(obj: dict, money_mode: str, what: str = "Stripe object") -> None:
    """A test-mode settlement must only ever see test-mode objects, and vice versa."""
    if not isinstance(obj, dict) or "livemode" not in obj:
        return
    if bool(obj.get("livemode")) != (money_mode == "live"):
        raise StripeError(f"{what} livemode={obj.get('livemode')} does not match settlement money mode {money_mode}")


_sleep = time.sleep  # monkeypatched by tests


# ---------------------------------------------------------------------------
# HTTP — form-encoded, Basic auth, idempotent retries
# ---------------------------------------------------------------------------

def flatten_params(params, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's nested form encoding: {"a": {"b": [1]}} -> [("a[b][0]", "1")]."""
    out: list[tuple[str, str]] = []
    if isinstance(params, dict):
        for key, value in params.items():
            if value is None:
                continue
            out.extend(flatten_params(value, f"{prefix}[{key}]" if prefix else str(key)))
    elif isinstance(params, (list, tuple)):
        for i, value in enumerate(params):
            out.extend(flatten_params(value, f"{prefix}[{i}]"))
    elif isinstance(params, bool):
        out.append((prefix, "true" if params else "false"))
    else:
        out.append((prefix, str(params)))
    return out


def _request(method: str, path: str, params: dict | None = None, *,
             idempotency_key: str | None = None, timeout: int = 30, attempts: int = 3) -> dict:
    key = stripe_key()
    if not key:
        raise StripeError("Stripe is not configured (STRIPE_SECRET_KEY is unset).")
    url = _STRIPE_BASE + path
    body = None
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(flatten_params(params))
    else:
        body = urllib.parse.urlencode(flatten_params(params or {})).encode()
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{key}:".encode()).decode(),
        "Stripe-Version": (os.environ.get("STRIPE_API_VERSION") or DEFAULT_API_VERSION).strip(),
        "User-Agent": "Rovyn/1.0 (group trip planner)",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if idempotency_key and method != "GET":
        headers["Idempotency-Key"] = idempotency_key[:255]

    last_err: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            _sleep(0.8 * attempt + random.random() * 0.4)
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as res:
                return json.loads(res.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                err = (json.loads(raw) or {}).get("error") or {}
            except json.JSONDecodeError:
                err = {"message": raw[:300]}
            last_err = StripeError(err.get("message") or f"Stripe HTTP {exc.code}", exc.code,
                                   err.get("code"), err.get("type"), err.get("param"))
            if exc.code not in _RETRYABLE:
                raise last_err
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = StripeError(f"Stripe unreachable: {exc}")
    raise last_err  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Checkout Sessions, PaymentIntents, refunds
# ---------------------------------------------------------------------------

def create_checkout_session(*, amount_cents: int, currency: str, product_name: str,
                            description: str, success_url: str, cancel_url: str,
                            client_reference_id: str, customer_email: str | None,
                            metadata: dict, expires_in_seconds: int = 24 * 3600,
                            statement_suffix: str | None = None) -> dict:
    """One hosted Checkout Session per traveler share, authorizing only.

    ``payment_intent_data[capture_method]=manual`` holds the money without
    charging; the extended-authorization request is honored only on accounts
    with interchange-plus access and silently ignored otherwise."""
    expires_at = int(time.time()) + max(1800, min(int(expires_in_seconds), 24 * 3600))
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": currency.lower(),
                "unit_amount": int(amount_cents),
                "product_data": {"name": product_name[:250], "description": description[:500]},
            },
            "quantity": 1,
        }],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": client_reference_id[:200],
        "customer_email": customer_email or None,
        "expires_at": expires_at,
        "submit_type": "book",
        "metadata": metadata,
        "payment_intent_data": {
            "capture_method": "manual",
            "description": description[:1000],
            "metadata": metadata,
            **({"statement_descriptor_suffix": statement_suffix[:22]} if statement_suffix else {}),
        },
        "payment_method_options": {"card": {"request_extended_authorization": "if_available"}},
    }
    return _request("POST", "/v1/checkout/sessions", params,
                    idempotency_key=f"cs:{client_reference_id}:{secrets.token_hex(8)}")


def retrieve_session(session_id: str) -> dict:
    return _request("GET", f"/v1/checkout/sessions/{session_id}",
                    {"expand": ["payment_intent", "payment_intent.latest_charge"]})


def expire_session(session_id: str, idempotency_key: str | None = None) -> dict:
    return _request("POST", f"/v1/checkout/sessions/{session_id}/expire", {},
                    idempotency_key=idempotency_key or f"expire:{session_id}")


def retrieve_payment_intent(pi_id: str) -> dict:
    return _request("GET", f"/v1/payment_intents/{pi_id}", {"expand": ["latest_charge"]})


def capture_payment_intent(pi_id: str, amount_cents: int | None = None,
                           idempotency_key: str | None = None) -> dict:
    params = {"amount_to_capture": int(amount_cents)} if amount_cents is not None else {}
    return _request("POST", f"/v1/payment_intents/{pi_id}/capture", params,
                    idempotency_key=idempotency_key or f"capture:{pi_id}", timeout=60)


def cancel_payment_intent(pi_id: str, reason: str = "requested_by_customer",
                          idempotency_key: str | None = None) -> dict:
    return _request("POST", f"/v1/payment_intents/{pi_id}/cancel",
                    {"cancellation_reason": reason}, idempotency_key=idempotency_key or f"cancel:{pi_id}")


def refund_payment_intent(pi_id: str, amount_cents: int, *, idempotency_key: str,
                          reason: str = "requested_by_customer") -> dict:
    """``idempotency_key`` is the persisted refunds row id — the row exists
    before Stripe is called, so a retry can never double-refund."""
    return _request("POST", "/v1/refunds",
                    {"payment_intent": pi_id, "amount": int(amount_cents), "reason": reason},
                    idempotency_key=idempotency_key, timeout=60)


def payment_intent_of(session: dict) -> dict | None:
    """The PaymentIntent object behind a session (expanded or fetched)."""
    pi = session.get("payment_intent")
    if isinstance(pi, dict):
        return pi
    if isinstance(pi, str) and pi:
        return retrieve_payment_intent(pi)
    return None


def capture_before_of(pi: dict) -> str:
    """When the authorization lapses — from the charge when Stripe says so,
    otherwise the default hold. Always ISO-8601 UTC."""
    charge = pi.get("latest_charge") if isinstance(pi, dict) else None
    card = (((charge or {}).get("payment_method_details") or {}).get("card") or {}) if isinstance(charge, dict) else {}
    ts = card.get("capture_before")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    return (datetime.now(timezone.utc) + timedelta(days=AUTH_HOLD_DAYS)).isoformat(timespec="seconds")


def extended_auth_status(pi: dict) -> str | None:
    charge = pi.get("latest_charge") if isinstance(pi, dict) else None
    if not isinstance(charge, dict):
        return None
    card = ((charge.get("payment_method_details") or {}).get("card") or {})
    return ((card.get("extended_authorization") or {}).get("status"))


# ---------------------------------------------------------------------------
# Webhook signatures (Stripe-Signature: t=<ts>,v1=<hex>[,v1=…][,v0=…])
# ---------------------------------------------------------------------------

def parse_signature_header(header: str) -> tuple[int | None, list[str]]:
    timestamp, v1s = None, []
    for item in (header or "").split(","):
        key, _, value = item.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1" and value:
            v1s.append(value)
    return timestamp, v1s


def verify_webhook_signature(payload: bytes, header: str, secret: str, *,
                             tolerance: int = 300, now: float | None = None) -> bool:
    """HMAC-SHA256 over ``<t>.<raw body>`` with the endpoint secret, compared in
    constant time, and the timestamp within ``tolerance`` seconds."""
    if not secret or not header:
        return False
    timestamp, v1s = parse_signature_header(header)
    if timestamp is None or not v1s:
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        return False
    signed = str(timestamp).encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in v1s)


def sign_webhook_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """The header Stripe would send — used by tests and local tooling."""
    ts = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(secret.encode(), str(ts).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


# ---------------------------------------------------------------------------
# Simulated twins — no key, no money, same shapes
# ---------------------------------------------------------------------------

def simulated_authorization(user_id: str) -> dict:
    return {
        "id": f"sim_pi_{secrets.token_hex(6)}",
        "status": "requires_capture",
        "capture_before": (datetime.now(timezone.utc) + timedelta(days=AUTH_HOLD_DAYS)).isoformat(timespec="seconds"),
        "simulated": True,
    }
