"""Strip personal data before anything is logged or persisted as audit.

Provider payloads carry what airlines and hotels need (dates of birth, phones,
emails, card hints). The audit trail needs to prove WHAT was called with WHICH
reference and WHAT came back — never who the traveler is. ``redact`` walks any
JSON-like structure and removes those fields; passenger blocks collapse to
their ids; request headers are never passed in at all.
"""
import hashlib
import json

_DROP_KEYS = {
    "born_on", "gender", "title", "phone", "phone_number", "email", "identity_documents",
    "identitydocuments", "guestpayment", "customer_details", "customer_email", "last4",
    "last_4_digits", "brand", "card", "payment_method_details", "billinginfo", "billing_details",
    "holder", "given_name", "family_name", "firstname", "lastname", "name_on_card", "payee_first_name",
    "payee_last_name", "address", "receipt_email", "authorization", "x-api-key",
}
_PASSENGER_KEYS = ("passengers", "guests")


def redact(value):
    """A deep copy of ``value`` with personal fields removed."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lower = str(key).lower()
            if lower in _DROP_KEYS:
                continue
            if lower in _PASSENGER_KEYS and isinstance(item, list):
                out[key] = [{"id": p.get("id") or p.get("passenger_id") or p.get("occupancyNumber"),
                             "user_id": p.get("user_id")} if isinstance(p, dict) else "…"
                            for p in item]
                continue
            out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def request_hash(payload) -> str:
    """A stable fingerprint of a request body, so the audit trail can prove
    which payload was sent without storing the payload."""
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def redacted_json(value, limit: int = 20000) -> str:
    return json.dumps(redact(value), default=str)[:limit]
