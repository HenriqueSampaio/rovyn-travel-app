"""Deep-link builders for external booking platforms.

Airbnb/Booking.com have no open consumer API and Expedia's requires partner
credentials, so the MVP generates pre-filled search links (destination, dates,
party size baked in). Swap these for real API calls when credentials exist.
"""
from urllib.parse import quote, quote_plus


def airbnb_stays(destination: str, check_in: str, check_out: str, adults: int) -> str:
    return (
        f"https://www.airbnb.com/s/{quote(destination)}/homes"
        f"?checkin={check_in}&checkout={check_out}&adults={adults}"
    )


def booking_stays(destination: str, check_in: str, check_out: str, adults: int) -> str:
    return (
        "https://www.booking.com/searchresults.html"
        f"?ss={quote_plus(destination)}&checkin={check_in}&checkout={check_out}"
        f"&group_adults={adults}&no_rooms=1"
    )


def expedia_flights(origin: str, destination: str, depart: str, return_: str, adults: int) -> str:
    def us_date(iso: str) -> str:
        parts = iso.split("-")
        if len(parts) == 3:
            return f"{parts[1]}/{parts[2]}/{parts[0]}"
        return iso

    leg1 = f"from:{origin},to:{destination},departure:{us_date(depart)}TANYT"
    leg2 = f"from:{destination},to:{origin},departure:{us_date(return_)}TANYT"
    return (
        "https://www.expedia.com/Flights-Search?trip=roundtrip"
        f"&leg1={quote(leg1)}&leg2={quote(leg2)}"
        f"&passengers=adults:{adults}&mode=search"
    )


def getyourguide_tours(destination: str, date_from: str, date_to: str) -> str:
    return (
        f"https://www.getyourguide.com/s/?q={quote_plus(destination)}"
        f"&date_from={date_from}&date_to={date_to}"
    )


def viator_tours(destination: str) -> str:
    return f"https://www.viator.com/searchResults/all?text={quote_plus(destination)}"


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
