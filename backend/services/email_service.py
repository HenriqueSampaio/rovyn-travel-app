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
                 total: float, group_size: int, confirmations: list[dict] | None = None,
                 estimates: list[dict] | None = None, demo: bool = True) -> str:
    """The receipt: what was charged (only provider-booked items), what stays
    an estimate, and every confirmation Rovyn holds for the group."""
    from html import escape

    def rows_for(entries):
        return "".join(
            f"<tr><td style='padding:8px 0;border-bottom:1px solid #eee'>{escape(str(i['label']))}</td>"
            f"<td style='padding:8px 0;border-bottom:1px solid #eee;text-align:right'>${i['amount']:,.2f}</td></tr>"
            for i in entries
        )

    trip_name, destination, traveler = escape(trip_name), escape(destination), escape(traveler)
    est_block = ""
    if estimates:
        est_block = f"""
      <p style="margin-top:22px;font-weight:bold">Not charged by Rovyn (estimates you pay on the spot)</p>
      <table style="width:100%;border-collapse:collapse">{rows_for(estimates)}</table>"""
    conf_block = ""
    if confirmations:
        lines = "".join(
            f"<li style='padding:4px 0'>{escape(str(c.get('title', '')))} — "
            f"<strong>{escape(str(c.get('confirmation') or 'confirmation pending'))}</strong>"
            f"{' (' + escape(str(c.get('provider'))) + ')' if c.get('provider') else ''}</li>"
            for c in confirmations
        )
        conf_block = f"""
      <p style="margin-top:22px;font-weight:bold">Your reservations</p>
      <ul style="padding-left:18px">{lines}</ul>"""
    demo_note = ("<p style=\"color:#7C877F;font-size:13px\">This is a Rovyn test receipt — no real money moved.</p>"
                 if demo else "")
    return f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#23312B">
      <h1 style="font-weight:400">Rovyn</h1>
      <p>Hi {traveler},</p>
      <p>Your payment for <strong>{trip_name}</strong> — {destination} — is confirmed.
         Shared costs are split equally across all {group_size} travelers; flights are
         priced from each traveler's own departure city.</p>
      <table style="width:100%;border-collapse:collapse;margin:24px 0">
        {rows_for(items)}
        <tr><td style="padding:12px 0;font-weight:bold">Total charged</td>
            <td style="padding:12px 0;text-align:right;font-weight:bold">${total:,.2f}</td></tr>
      </table>{est_block}{conf_block}
      {demo_note}
    </div>
    """


def cancellation_html(trip_name: str, item_title: str, by: str, refund: float) -> str:
    from html import escape
    return f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#23312B">
      <h1 style="font-weight:400">Rovyn</h1>
      <p><strong>{escape(item_title)}</strong> on <strong>{escape(trip_name)}</strong> was cancelled by {escape(by)}.</p>
      <p>{'Each traveler is being refunded their share — ' + f'${refund:,.2f}' + ' in total goes back to the group.' if refund > 0 else 'This rate was non-refundable, so nothing comes back.'}</p>
      <p style="color:#7C877F;font-size:13px">The item is back on the booking checklist for the group to book another way.</p>
    </div>
    """


def attention_html(trip_name: str, reason: str) -> str:
    from html import escape
    return f"""
    <div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#23312B">
      <h1 style="font-weight:400">Rovyn</h1>
      <p>The booking for <strong>{escape(trip_name)}</strong> stopped and needs a person to look at it.</p>
      <p>{escape(reason)}</p>
      <p>No further charges, refunds or cancellations were issued automatically. Rovyn support will reconcile
         what was booked against what was charged and get back to the group.</p>
    </div>
    """
