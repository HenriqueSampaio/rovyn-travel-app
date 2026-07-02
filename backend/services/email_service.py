"""Transactional email via Resend's HTTP API.

Works without any extra dependency (urllib). If RESEND_API_KEY is not set the
send is *simulated*: the receipt still lives in the app, and the API response
says so, so the UI can tell the user honestly.
"""
import json
import os
import ssl
import urllib.error
import urllib.request

import certifi

# macOS system Pythons often lack root CAs; certifi's bundle works everywhere
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def email_available() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def send_email(to: str, subject: str, html: str) -> dict:
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        return {"sent": False, "simulated": True,
                "note": "Set RESEND_API_KEY in Rovyn/.env to deliver real emails."}

    payload = json.dumps({
        "from": os.environ.get("EMAIL_FROM", "Rovyn <onboarding@resend.dev>"),
        "to": [to],
        "subject": subject,
        "html": html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as res:
            body = json.loads(res.read().decode() or "{}")
        return {"sent": True, "simulated": False, "id": body.get("id")}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return {"sent": False, "simulated": False, "error": str(exc)}


def receipt_html(trip_name: str, destination: str, traveler: str, items: list[dict],
                 total: float, group_size: int) -> str:
    rows = "".join(
        f"<tr><td style='padding:8px 0;border-bottom:1px solid #eee'>{i['label']}</td>"
        f"<td style='padding:8px 0;border-bottom:1px solid #eee;text-align:right'>${i['amount']:,.2f}</td></tr>"
        for i in items
    )
    return f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#23312B">
      <h1 style="font-weight:400">Rovyn</h1>
      <p>Hi {traveler},</p>
      <p>Your payment for <strong>{trip_name}</strong> — {destination} — is confirmed.
         The trip cost is split equally across all {group_size} travelers; flights are
         priced from each traveler's own departure city.</p>
      <table style="width:100%;border-collapse:collapse;margin:24px 0">
        {rows}
        <tr><td style="padding:12px 0;font-weight:bold">Total charged</td>
            <td style="padding:12px 0;text-align:right;font-weight:bold">${total:,.2f}</td></tr>
      </table>
      <p style="color:#7C877F;font-size:13px">This is a Rovyn demo receipt — no real money moved.</p>
    </div>
    """
