"""Deep-link builders for external booking platforms.

Airbnb/Booking.com have no open consumer API and Expedia's requires partner
credentials, so the MVP generates pre-filled search links (destination, dates,
party size baked in). Swap these for real API calls when credentials exist.

Phase A monetization: when affiliate IDs are configured in the environment,
they are appended to every generated link so bookings made through Rovyn earn
commission. Links work identically without them.
"""
import os
from urllib.parse import quote, quote_plus, urlencode, urlparse


def safe_url(url) -> str | None:
    """Only http(s) URLs may reach an <a href> — anything else (javascript:,
    data:, garbage from an external API or a model) becomes None."""
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    return url.strip() if parsed.scheme in ("http", "https") and parsed.netloc else None


def _with_affiliate(url: str, params: dict) -> str:
    """Append affiliate params whose values are set; leave the URL alone otherwise."""
    present = {k: v for k, v in params.items() if v}
    if not present:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(present)


def airbnb_stays(destination: str, check_in: str, check_out: str, adults: int) -> str:
    # Airbnb has no affiliate program — plain deep link.
    return (
        f"https://www.airbnb.com/s/{quote(destination)}/homes"
        f"?checkin={check_in}&checkout={check_out}&adults={adults}"
    )


def booking_stays(destination: str, check_in: str, check_out: str, adults: int) -> str:
    url = (
        "https://www.booking.com/searchresults.html"
        f"?ss={quote_plus(destination)}&checkin={check_in}&checkout={check_out}"
        f"&group_adults={adults}&no_rooms=1"
    )
    return _with_affiliate(url, {"aid": os.environ.get("BOOKING_AFFILIATE_AID")})


def expedia_flights(origin: str, destination: str, depart: str, return_: str, adults: int) -> str:
    def us_date(iso: str) -> str:
        parts = iso.split("-")
        if len(parts) == 3:
            return f"{parts[1]}/{parts[2]}/{parts[0]}"
        return iso

    leg1 = f"from:{origin},to:{destination},departure:{us_date(depart)}TANYT"
    leg2 = f"from:{destination},to:{origin},departure:{us_date(return_)}TANYT"
    url = (
        "https://www.expedia.com/Flights-Search?trip=roundtrip"
        f"&leg1={quote(leg1)}&leg2={quote(leg2)}"
        f"&passengers=adults:{adults}&mode=search"
    )
    return _with_affiliate(url, {"affcid": os.environ.get("EXPEDIA_AFFILIATE_ID")})


def getyourguide_tours(destination: str, date_from: str, date_to: str) -> str:
    url = (
        f"https://www.getyourguide.com/s/?q={quote_plus(destination)}"
        f"&date_from={date_from}&date_to={date_to}"
    )
    return _with_affiliate(url, {"partner_id": os.environ.get("GYG_PARTNER_ID")})


def viator_tours(destination: str) -> str:
    url = f"https://www.viator.com/searchResults/all?text={quote_plus(destination)}"
    return _with_affiliate(url, {
        "pid": os.environ.get("VIATOR_PARTNER_ID"),
        "mcid": "42383" if os.environ.get("VIATOR_PARTNER_ID") else None,
        "medium": "api" if os.environ.get("VIATOR_PARTNER_ID") else None,
    })


def stay_links(destination: str, check_in: str, check_out: str, adults: int) -> dict:
    return {
        "airbnb": airbnb_stays(destination, check_in, check_out, adults),
        "booking": booking_stays(destination, check_in, check_out, adults),
    }


def activity_links(destination: str, date_from: str, date_to: str) -> dict:
    return {
        "getyourguide": getyourguide_tours(destination, date_from, date_to),
        "viator": viator_tours(destination),
    }
