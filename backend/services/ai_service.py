import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as dt, timedelta

from backend.services import links, llm
from backend.services.hotel_service import official_website, real_stay_options
from backend.services.image_service import first_image, image_for, nth_image

# "itinerary" is no longer a starrable category — Rovyn builds the itinerary
# from the group's starred visits & events at their preferred pace.
CATEGORIES = ["flights", "stay", "visits", "events"]


def categories_for(trip_mode: str | None) -> list[str]:
    """The starrable sections of a trip's board, in checklist order. Multi-city
    trips add "transport" — the train/flight legs between the route's cities."""
    if (trip_mode or "") == "multi":
        return ["flights", "transport", "stay", "visits", "events"]
    return CATEGORIES

PACES = ("very", "moderately", "slightly")  # how busy the trip should be


def recommended_picks(duration_days: int, pace: str) -> dict:
    """How many visits/events a traveler should aim to star, given their pace."""
    per_day = {"very": 1.5, "moderately": 1.0, "slightly": 0.6}.get(pace, 1.0)
    visits = max(2, round(duration_days * per_day))
    events = {"very": max(2, round(duration_days / 2)),
              "moderately": max(1, round(duration_days / 3)),
              "slightly": 1}.get(pace, 2)
    return {"visits": visits, "events": events}


def ai_available() -> bool:
    return llm.available()


# ---------------------------------------------------------------------------
# Preference aggregation
# ---------------------------------------------------------------------------

def _same_town(a: str, b: str) -> bool:
    """'Nice, France' and 'Nice' name the same town."""
    town = lambda s: (s or "").split(",")[0].strip().casefold()
    return bool(town(a)) and town(a) == town(b)


def _aggregate_preferences(responses: list[dict]) -> dict:
    all_vibes: list[str] = []
    all_accommodation: list[str] = []
    trip_styles: list[str] = []
    departure_cities: list[str] = []
    return_by_city: dict[str, str] = {}
    budgets_min, budgets_max = [], []
    dates_start, dates_end = [], []
    requirements: list[str] = []
    paces: list[str] = []
    pick_names: list[str] = []      # multi-city wishlist, first-seen casing
    pick_counts: Counter = Counter()

    for r in responses:
        if r.get("pace") in PACES:
            paces.append(r["pace"])
        all_vibes.extend(r.get("vibes", []))
        all_accommodation.append(r.get("accommodation_type", ""))
        style = r.get("trip_style", "no-preference")
        if style and style != "no-preference":
            trip_styles.append(style)
        city = r.get("departure_city", "").strip()
        if city:
            # "new york" and "New York" are the same place — keep first-seen casing
            if not any(c.casefold() == city.casefold() for c in departure_cities):
                departure_cities.append(city)
            ret = (r.get("return_city") or "").strip()
            if ret and ret.casefold() != city.casefold():
                # First traveler from a city to name a different way home wins;
                # everyone else from that city defaults to a plain round trip.
                canonical = next(c for c in departure_cities if c.casefold() == city.casefold())
                return_by_city.setdefault(canonical, ret)
        seen_picks = set()
        for pick in r.get("multi_city_picks") or []:
            pick = (pick or "").strip()
            key = pick.casefold()
            if not pick or key in seen_picks:  # one vote per traveler per city
                continue
            seen_picks.add(key)
            if not any(p.casefold() == key for p in pick_names):
                pick_names.append(pick)
            pick_counts[key] += 1
        budgets_min.append(r.get("budget_min", 0))
        budgets_max.append(r.get("budget_max", 0))
        if r.get("travel_dates_start"):
            dates_start.append(r["travel_dates_start"])
        if r.get("travel_dates_end"):
            dates_end.append(r["travel_dates_end"])
        req = r.get("special_requirements", "").strip()
        if req:
            requirements.append(req)

    vibe_counts = Counter(all_vibes)
    top_vibes = [v for v, _ in vibe_counts.most_common()]
    accommodation_counts = Counter(a for a in all_accommodation if a)
    top_accommodation = accommodation_counts.most_common(1)[0][0] if accommodation_counts else "hotel"

    # Budget the whole group can afford: highest floor to lowest ceiling.
    # If members' ranges don't overlap, fall back to the full span.
    budget_min = max(budgets_min) if budgets_min else 0
    budget_max = min(budgets_max) if budgets_max else 0
    if budget_min > budget_max:
        budget_min, budget_max = min(budgets_min), max(budgets_max)

    # Date window everyone can make: latest start to earliest end,
    # falling back to the widest window if there is no overlap.
    start = max(dates_start) if dates_start else "flexible"
    end = min(dates_end) if dates_end else "flexible"
    if dates_start and dates_end and start > end:
        start, end = min(dates_start), max(dates_end)

    style_counts = Counter(trip_styles)
    trip_style = style_counts.most_common(1)[0][0] if style_counts else "no-preference"

    pace_counts = Counter(paces)
    group_pace = pace_counts.most_common(1)[0][0] if pace_counts else "moderately"

    # Multi-city wishlist, most-wanted first (stable sort keeps first-seen order on ties)
    city_votes = sorted(
        ({"city": name, "count": pick_counts[name.casefold()]} for name in pick_names),
        key=lambda v: -v["count"],
    )

    # The group's travel shape: strict majority of expressed styles decides;
    # a tie tips multi-city only when someone actually named cities to visit.
    multi_n, single_n = trip_styles.count("multi-city"), trip_styles.count("single-city")
    if multi_n > single_n or (multi_n and multi_n == single_n and city_votes):
        trip_mode = "multi"
    else:
        trip_mode = "single"

    return {
        "group_pace": group_pace,
        "group_size": len(responses),
        "budget_range": {"min": budget_min, "max": budget_max},
        "travel_dates": {"start": start, "end": end},
        "top_vibes": top_vibes,
        "preferred_accommodation": top_accommodation,
        "accommodation_votes": dict(accommodation_counts),
        "trip_style": trip_style,
        "trip_mode": trip_mode,
        "city_votes": city_votes,
        "departure_cities": sorted(departure_cities),
        "return_by_city": return_by_city,
        "special_requirements": requirements,
    }


def _duration_days(dates: dict) -> int:
    try:
        start = dt.strptime(dates["start"], "%Y-%m-%d")
        end = dt.strptime(dates["end"], "%Y-%m-%d")
        return max((end - start).days, 1)
    except (ValueError, KeyError, TypeError):
        return 5


def _ask(prompt: str, max_tokens: int = 8192, tier: str = "standard"):
    """One JSON generation on the tier's model (see services/llm.py for the
    fast/standard split and provider selection)."""
    return llm.generate_json(prompt, tier=tier, max_tokens=max_tokens)


def _attach_links(rec: dict, prefs: dict) -> dict:
    dates = prefs["travel_dates"]
    check_in = dates["start"] if dates["start"] != "flexible" else ""
    check_out = dates["end"] if dates["end"] != "flexible" else ""
    adults = max(prefs["group_size"], 1)
    place = f"{rec.get('name', '')}, {rec.get('country', '')}".strip(", ")

    rec["links"] = {
        "stays": links.stay_links(place, check_in, check_out, adults),
        "activities": links.activity_links(place, check_in, check_out),
        "flights": {
            city: links.expedia_flights(city, rec.get("name", ""), check_in, check_out, adults)
            for city in prefs["departure_cities"]
        },
    }
    return rec


# ---------------------------------------------------------------------------
# Destination recommendations
# ---------------------------------------------------------------------------

def get_destination_recommendations(responses: list[dict], trip_name: str = "",
                                    exclude: list[str] | None = None) -> list[dict]:
    """exclude lists destination names already offered — "give us more ideas"
    re-runs this and must come back with five different places."""
    prefs = _aggregate_preferences(responses)
    exclude = [e for e in (exclude or []) if (e or "").strip()]

    if ai_available():
        recommendations = _ai_recommendations(prefs, trip_name, exclude)
    else:
        recommendations = _sample_recommendations(prefs, exclude)
    # The model is asked to avoid repeats, never trusted to
    seen = {e.casefold() for e in exclude}
    recommendations = [r for r in recommendations if (r.get("name") or "").casefold() not in seen]
    _finalize_rec_routes(recommendations, prefs)

    for rec in recommendations:
        rec["image"] = first_image(
            f"{rec.get('name', '')} {rec.get('country', '')} travel",
            rec.get("name", ""),
        )
    return [_attach_links(rec, prefs) for rec in recommendations]


def _want_rec_routes(prefs: dict) -> bool:
    """Multi-city groups get route-shaped recommendations — unless the trip is
    too short to move between cities (plan_route applies the same floor)."""
    return prefs["trip_mode"] == "multi" and _duration_days(prefs["travel_dates"]) >= 4


def _finalize_rec_routes(recommendations: list[dict], prefs: dict) -> None:
    """Never trust the model with the route math: normalize each rec's route
    (dedupe, stop cap, nights summing to the trip) and rebuild the readable
    line to match. Recs whose route can't be salvaged — and every rec of a
    single-base group — carry no route at all."""
    duration = _duration_days(prefs["travel_dates"])
    for rec in recommendations:
        raw, cities = rec.get("route"), []
        if _want_rec_routes(prefs) and isinstance(raw, list):
            for c in raw:
                if not isinstance(c, dict):
                    continue
                c = dict(c)
                city = (c.get("city") or "").strip()
                # Per-city hotel search & links downstream need "City, Country"
                if city and "," not in city and rec.get("country"):
                    c["city"] = f"{city}, {rec['country']}"
                cities.append(c)
            cities = _normalize_route_cities(cities, "", duration)
        if len(cities) < 2:
            rec.pop("route", None)
            continue
        rec["route"] = cities
        rec["suggested_route"] = " -> ".join(
            f"{c['city']} ({c['nights']} night{'s' if c['nights'] != 1 else ''})" for c in cities
        )


def _ai_recommendations(prefs: dict, trip_name: str = "", exclude: list[str] | None = None) -> list[dict]:
    budget = prefs["budget_range"]
    dates = prefs["travel_dates"]
    duration = _duration_days(dates)
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"
    departures = ", ".join(prefs["departure_cities"]) if prefs["departure_cities"] else "unknown"

    want_routes = _want_rec_routes(prefs)
    if want_routes:
        max_cities = max(2, min(4, duration // 2))
        wishes = "; ".join(f"{v['city']} — {v['count']} traveler(s) want this" for v in prefs["city_votes"]) or \
            "no specific requests"
        trip_style_note = (
            f"The group voted to explore MULTIPLE cities — every recommendation must BE a multi-city route of "
            f"2-{max_cities} stops with easy internal travel (one country or a compact region), not a single base.\n"
            f"- Dream-city wishlist (with votes): {wishes}. Where geography reasonably allows, weave the most-wished "
            f"cities into your routes — a wished city should appear on at least one of the five."
        )
        route_field = '\n    "route": [{"city": "Athens, Greece", "nights": 2, "why": "one short clause"}],'
        route_rule = (
            f'\nEvery "route" lists the ordered stops: each city label MUST be "City, Country", '
            f"and nights MUST total exactly {duration}."
        )
    else:
        trip_style_note = {
            "multi-city": "The group likes moving between cities, but this window is short — favor one well-placed base with easy day trips.",
            "single-city": "The group prefers to stay based in ONE city with day trips at most.",
        }.get(prefs["trip_style"], "The group has no strong preference between one base city or a multi-city route.")
        route_field, route_rule = "", ""

    name_note = ""
    if trip_name.strip():
        name_note = (
            f'\n- The group named this trip "{trip_name.strip()}". If the name references a specific '
            "place, region, country or season, treat that as a STRONG signal of intent — at least 2-3 "
            "of your recommendations should honor it (the named place itself, plus close alternatives "
            "in the same region), unless it clearly conflicts with the group's budget or dates."
        )

    exclude_note = ""
    if exclude:
        exclude_note = (
            f"\n- The group already saw and passed on: {', '.join(exclude)}. Recommend 5 COMPLETELY "
            "DIFFERENT destinations — no repeats, and reach for different countries or regions where sensible."
        )

    traveler_line = ("A solo traveler is planning a trip." if prefs["group_size"] == 1
                     else f"A group of {prefs['group_size']} people wants to travel together.")
    prompt = f"""You are an expert travel planner. {traveler_line}

Group profile:
- Budget per person: ${budget['min']}–${budget['max']} USD (total trip, flights excluded)
- Travel window: {dates['start']} to {dates['end']} ({duration} nights)
- Departure cities of members: {departures}
- Desired vibes (most voted first): {vibes}
- {trip_style_note}
- Preferred accommodation: {prefs['preferred_accommodation']}
- Special requirements: {requirements}{name_note}{exclude_note}

Recommend exactly 5 travel destinations that suit this entire group. Weigh nature/outdoors, nightlife/party, and cultural interests according to the vibes above, and factor in reasonable flight access from the departure cities. "why_it_matches" must tie each pick to THIS group's vibes, budget and dates.{route_rule}

Respond with ONLY a valid JSON array (no markdown, no extra text):
[
  {{
    "name": "City or Region Name",
    "country": "Country",
    "match_score": 95,
    "tagline": "One evocative sentence capturing the vibe",
    "description": "2-3 sentences about why this destination is special",
    "why_it_matches": "Specific explanation of how this fits the group's preferences",
    "highlights": ["Highlight 1", "Highlight 2", "Highlight 3", "Highlight 4"],
    "estimated_budget_per_person": {{"min": 600, "max": 1200}},
    "best_activities": ["Activity 1", "Activity 2", "Activity 3"],{route_field}
    "suggested_route": "For multi-city trips: City A (2 nights) -> City B (3 nights); otherwise the base city",
    "best_time_to_visit": "Month or season note"
  }}
]"""

    return _ask(prompt, max_tokens=6000 if want_routes else 4096)


def _sample_recommendations(prefs: dict, exclude: list[str] | None = None) -> list[dict]:
    """Deterministic demo data used when ANTHROPIC_API_KEY is not configured.
    The pool holds two batches' worth so "give us more ideas" works offline."""
    budget = prefs["budget_range"]
    lo = budget["min"] or 500
    hi = budget["max"] or 2000
    samples = [
        ("Lisbon", "Portugal", 94, "Sun-drenched hills, tiled streets and late-night fado.",
         ["Alfama old town", "Day trip to Sintra", "Miradouro sunsets", "Pastéis de Belém"],
         "Lisbon (3 nights) -> Lagos, Algarve (2 nights)", "Lagos, Portugal"),
        ("Barcelona", "Spain", 90, "Beach mornings, Gaudí afternoons, nightlife until sunrise.",
         ["Sagrada Família", "Barceloneta beach", "El Born tapas crawl", "Montjuïc views"],
         "Barcelona (4 nights) -> Costa Brava (1 night)", "Girona, Spain"),
        ("Mexico City", "Mexico", 87, "World-class food and culture at a friendly price point.",
         ["Chapultepec park", "Teotihuacán pyramids", "Roma Norte food scene", "Lucha libre night"],
         "Mexico City (3 nights) -> Oaxaca (2 nights)", "Oaxaca, Mexico"),
        ("Split", "Croatia", 85, "Adriatic swims, island hops and Roman ruins in one trip.",
         ["Diocletian's Palace", "Hvar island hop", "Krka waterfalls", "Old-town bars"],
         "Split (3 nights) -> Hvar (2 nights)", "Hvar, Croatia"),
        ("Chiang Mai", "Thailand", 82, "Temples, jungle treks and night markets on a backpacker budget.",
         ["Old-city temples", "Doi Inthanon trek", "Sunday night market", "Thai cooking class"],
         "Chiang Mai (3 nights) -> Pai (2 nights)", "Pai, Thailand"),
        ("Athens", "Greece", 89, "Ancient stones by day, Aegean ferries and rooftop bars by night.",
         ["Acropolis at opening time", "Plaka tavernas", "Ferry to the Cyclades", "Lycabettus sunset"],
         "Athens (3 nights) -> Santorini (2 nights)", "Santorini, Greece"),
        ("Kyoto", "Japan", 88, "Temples at dawn, izakayas at night, bullet-train side trips.",
         ["Fushimi Inari gates", "Arashiyama bamboo grove", "Gion at dusk", "Nishiki market"],
         "Kyoto (3 nights) -> Osaka (2 nights)", "Osaka, Japan"),
        ("Budapest", "Hungary", 86, "Thermal baths, ruin bars and grand river views on a kind budget.",
         ["Széchenyi baths", "Ruin-bar crawl", "Danube at night", "Castle-hill walk"],
         "Budapest (3 nights) -> Vienna (2 nights)", "Vienna, Austria"),
        ("Cartagena", "Colombia", 84, "Caribbean color, walled-city nights and island beach days.",
         ["Walled old town", "Rosario Islands boat day", "Getsemaní street life", "Sunset at Café del Mar"],
         "Cartagena (3 nights) -> Santa Marta (2 nights)", "Santa Marta, Colombia"),
        ("Marrakesh", "Morocco", 83, "Souks, riads and desert stars a short hop from Europe.",
         ["Jemaa el-Fnaa at dusk", "Jardin Majorelle", "Atlas foothills day trip", "Hammam afternoon"],
         "Marrakesh (3 nights) -> Essaouira (2 nights)", "Essaouira, Morocco"),
    ]
    skip = {(e or "").casefold() for e in exclude or []}
    samples = [s for s in samples if s[0].casefold() not in skip][:5]
    recs = []
    for name, country, score, tagline, highlights, route, partner in samples:
        rec = {
            "name": name,
            "country": country,
            "match_score": score,
            "tagline": tagline,
            "description": f"{name} balances the group's mix of interests with a budget-friendly cost of living.",
            "why_it_matches": "Sample recommendation — set ANTHROPIC_API_KEY in Rovyn/.env for AI-personalized matches based on your questionnaires.",
            "highlights": highlights,
            "estimated_budget_per_person": {"min": lo, "max": hi},
            "best_activities": highlights[:3],
            "suggested_route": route,
            "best_time_to_visit": "Spring or early autumn",
            "source": "sample",
        }
        if _want_rec_routes(prefs):
            # Same shape as AI multi-mode recs: anchor + any wished cities that
            # land in this rec's country + the classic pairing. Normalization
            # (_finalize_rec_routes) dedupes and balances the nights.
            wished = [v["city"] for v in prefs["city_votes"]
                      if v["city"].casefold().endswith(f", {country.casefold()}")]
            rec["route"] = (
                [{"city": f"{name}, {country}", "nights": 3, "why": "The anchor — most to see and do."}]
                + [{"city": w, "nights": 2, "why": "On the group's wishlist."} for w in wished]
                + [{"city": partner, "nights": 2, "why": "The classic pairing."}]
            )
        recs.append(rec)
    return recs


# ---------------------------------------------------------------------------
# Multi-city: nearby-city ideas & route planning
# ---------------------------------------------------------------------------

def suggest_nearby_cities(anchor: str, trip_name: str = "", limit: int = 8) -> list[dict]:
    """City ideas around the trip's destination, for the questionnaire's
    multi-city picker. AI when available; otherwise the biggest same-country
    cities from the offline GeoNames set. [] when there's nothing to anchor on —
    suggestion-mode trips pick their destination later, so guessing a region
    here would only bias the group's wishlist."""
    from backend.services.places_service import cities_in_country

    anchor = (anchor or "").strip()
    if not anchor:
        return []
    if ai_available():
        hint = f' The trip is named "{trip_name.strip()}" — honor any place it references.' if trip_name.strip() else ""
        prompt = f"""You suggest add-on cities for a group building a multi-city trip. The group's anchor destination is {anchor}.{hint}

Suggest exactly {limit} cities travelers commonly combine with it in one trip (day-trip towns count only if worth an overnight). Prefer places reachable by train or a short flight. Never repeat the anchor itself.

Respond with ONLY a valid JSON array:
[{{"city": "Nice", "country": "France", "travel_hint": "5h30 by TGV / 1h20 flight from Paris", "why": "one short clause on what it adds to the trip"}}]"""
        try:
            raw = _ask(prompt, max_tokens=2048, tier="fast")
            out = []
            for s in raw if isinstance(raw, list) else []:
                city = (s.get("city") or "").strip()
                if not city or _same_town(city, anchor):
                    continue
                country = (s.get("country") or "").strip()
                out.append({
                    "city": city,
                    "country": country,
                    "label": f"{city}, {country}" if country else city,
                    "travel_hint": (s.get("travel_hint") or "").strip(),
                    "why": (s.get("why") or "").strip(),
                })
            if out:
                return out[:limit]
        except Exception:
            pass  # fall through to the offline list
    town, _, country = [p.strip() for p in (anchor.partition(","))]
    return cities_in_country(country, limit=limit, exclude={town}) if country else []


def _route_with_dates(cities: list[dict], dates: dict, duration: int, reason: str) -> dict:
    """Attach calendar windows, day ranges and travel legs to a city sequence.
    Day numbers follow the plan's convention: day 1..duration, one per night."""
    try:
        start = dt.strptime(dates.get("start", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        start = None
    cum = 0
    for c in cities:
        c["town"] = c["city"].split(",")[0].strip()
        c["day_start"] = cum + 1
        c["day_end"] = cum + c["nights"]
        if start:
            c["start"] = (start + timedelta(days=cum)).strftime("%Y-%m-%d")
            c["end"] = (start + timedelta(days=cum + c["nights"])).strftime("%Y-%m-%d")
        else:
            c["start"], c["end"] = "", ""
        cum += c["nights"]
    legs = [
        {
            "key": f"{a['town']} → {b['town']}",
            "from": a["town"], "to": b["town"],
            "from_city": a["city"], "to_city": b["city"],
            "date": b["start"], "day": b["day_start"],
        }
        for a, b in zip(cities, cities[1:])
    ]
    return {"cities": cities, "legs": legs, "reason": reason, "duration_days": duration}


def _normalize_route_cities(raw_cities: list[dict], anchor: str, duration: int) -> list[dict]:
    """Sanitize an AI (or fallback) city list: dedupe, ensure the anchor is on
    the route (when one is given — recommendation routes have none, their name
    may be a region label rather than a city), cap the stop count, and force
    nights to sum exactly to the trip."""
    max_cities = max(2, min(4, duration // 2))
    cities: list[dict] = []
    for c in raw_cities:
        name = (c.get("city") or "").strip()
        if not name or any(_same_town(name, existing["city"]) for existing in cities):
            continue
        nights = c.get("nights")
        cities.append({
            "city": name,
            "nights": max(1, int(nights)) if isinstance(nights, (int, float)) else 2,
            "why": (c.get("why") or "").strip(),
        })
    if anchor and not any(_same_town(c["city"], anchor) for c in cities):
        cities.insert(0, {"city": anchor, "nights": 2, "why": "The trip's chosen destination."})
    cities = cities[:max_cities]
    # Nights must sum to the trip's night count — trim/grow from the ends
    diff = duration - sum(c["nights"] for c in cities)
    i = 0
    while diff != 0 and cities:
        c = cities[i % len(cities)]
        if diff > 0:
            c["nights"] += 1
            diff -= 1
        elif c["nights"] > 2 or (len(cities) == 1 and c["nights"] > 1):
            c["nights"] -= 1
            diff += 1
        elif len(cities) > 2 and all(x["nights"] <= 2 for x in cities):
            dropped = cities.pop()  # too many stops for these dates
            diff += dropped["nights"]
        i += 1
        if i > 100:  # can't balance (degenerate input) — bail to even split
            base, rem = divmod(duration, len(cities))
            for j, c in enumerate(cities):
                c["nights"] = max(1, base + (1 if j < rem else 0))
            break
    return cities


def _ai_route(anchor: str, prefs: dict, duration: int, trip_name: str) -> dict | None:
    votes = "; ".join(f"{v['city']} — {v['count']} traveler(s) want this" for v in prefs["city_votes"]) or \
            "no specific requests — you choose the classic pairing"
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    dates = prefs["travel_dates"]
    max_cities = max(2, min(4, duration // 2))
    prompt = f"""You are planning the ROUTE of a multi-city group trip anchored at {anchor} ({dates['start']} to {dates['end']}, {duration} nights). Trip name: "{trip_name or anchor}".

The group's city wishlist (with votes): {votes}
Group vibes: {vibes}

Design the best route of 2-{max_cities} stops INCLUDING the anchor. Honor the most-voted wishes when geography allows, order the stops to avoid backtracking, and allocate nights (minimum 2 per stop, total exactly {duration}). More nights go to denser cities.

Respond with ONLY valid JSON:
{{"reason": "1-2 sentences on why this route and split", "cities": [{{"city": "Paris, France", "nights": 3, "why": "one short clause"}}]}}"""
    result = _ask(prompt, max_tokens=2048)
    return result if isinstance(result, dict) and result.get("cities") else None


def plan_route(destination: str, prefs: dict, trip_name: str = "",
               seed_route: list | None = None) -> dict | None:
    """The multi-city route: ordered cities, nights each, dates and travel legs.
    None when the group prefers one home base or the trip is too short to move.
    seed_route is the structured route off the recommendation the group voted
    for — when sane, it IS the route, so the board delivers the pitch they chose."""
    duration = _duration_days(prefs["travel_dates"])
    if prefs.get("trip_mode") != "multi" or duration < 4:
        return None

    seed = [c for c in seed_route or [] if isinstance(c, dict) and (c.get("city") or "").strip()]
    if seed:
        cities = _normalize_route_cities(seed, "", duration)
        if len(cities) >= 2:
            return _route_with_dates(cities, prefs["travel_dates"], duration,
                                     "The route the group voted for on the destination ballot.")

    raw, reason = None, ""
    if ai_available():
        try:
            raw = _ai_route(destination, prefs, duration, trip_name)
        except Exception:
            raw = None
    if raw:
        cities = _normalize_route_cities(raw["cities"], destination, duration)
        reason = (raw.get("reason") or "").strip()
    else:
        wished = [{"city": v["city"], "nights": 2} for v in prefs["city_votes"]
                  if not _same_town(v["city"], destination)]
        if not wished:
            # Nothing specific was requested — pair the anchor with the
            # country's next-biggest city so the route still exists offline.
            from backend.services.places_service import cities_in_country
            town, _, country = [p.strip() for p in destination.partition(",")]
            nearby = cities_in_country(country, limit=1, exclude={town}) if country else []
            wished = [{"city": n["label"], "nights": 2} for n in nearby]
        cities = _normalize_route_cities(
            [{"city": destination, "nights": 2}] + wished, destination, duration)
        reason = "Built from the group's picks — most-requested cities first, anchored at the chosen destination."
    if len(cities) < 2:
        return None  # no second city to travel to — plan a single-base trip
    return _route_with_dates(cities, prefs["travel_dates"], duration, reason)


# ---------------------------------------------------------------------------
# Planning board: option generation (flights / stay / itinerary / visits / events)
# ---------------------------------------------------------------------------

def _city_slug(city: str) -> str:
    return "".join(ch for ch in city.lower() if ch.isalnum()) or "city"


def _assign_option_ids(options: dict) -> dict:
    for cat in ("stay", "visits", "events"):
        for i, opt in enumerate(options.get(cat, [])):
            opt["id"] = f"{cat}-{i + 1}"
    for city, flights in (options.get("flights") or {}).items():
        for i, opt in enumerate(flights):
            opt["id"] = f"flight-{_city_slug(city)}-{i + 1}"
            opt["departure_city"] = city
    for leg_key, leg_options in (options.get("transport") or {}).items():
        for i, opt in enumerate(leg_options):
            opt["id"] = f"transport-{_city_slug(leg_key)}-{i + 1}"
            opt["leg"] = leg_key
    return options


def _attach_option_links(options: dict, prefs: dict, destination: str) -> dict:
    dates = options.get("dates", {})
    check_in = dates.get("start", "")
    check_out = dates.get("end", "")
    adults = max(prefs["group_size"], 1)
    route = options.get("route")
    seg_by_town = {c["town"].casefold(): c for c in (route or {}).get("cities", [])}

    def city_context(opt) -> tuple[str, str, str]:
        """(place, check_in, check_out) for an option — its route city's segment
        window on multi-city trips, the whole trip otherwise."""
        seg = seg_by_town.get((opt.get("city") or "").split(",")[0].strip().casefold())
        if seg:
            return seg["city"], seg.get("start") or check_in, seg.get("end") or check_out
        return destination, check_in, check_out

    for opt in options.get("stay", []):
        place, c_in, c_out = city_context(opt)
        opt["links"] = links.stay_links(place, c_in, c_out, adults)
    for opt in options.get("visits", []) + options.get("events", []):
        place, c_in, c_out = city_context(opt)
        opt["links"] = links.activity_links(place, c_in, c_out)

    # Flights are round trips: into the route's first city, home from its last
    # (same city when the trip is single-base), landing at each traveler's own
    # return city when they gave one.
    arrive_place = route["cities"][0]["city"] if route else destination
    return_from = route["cities"][-1]["city"] if route else ""
    for city, flights in (options.get("flights") or {}).items():
        return_to = prefs.get("return_by_city", {}).get(city, "")
        url = links.expedia_flights(city, arrive_place, check_in, check_out, adults,
                                    return_from=return_from, return_to=return_to)
        for opt in flights:
            opt["link"] = url
    for leg in (route or {}).get("legs", []):
        r2r = links.rome2rio(leg["from_city"], leg["to_city"])
        for opt in (options.get("transport") or {}).get(leg["key"], []):
            opt["link"] = r2r
    return options


def generate_trip_options(destination: str, responses: list[dict], trip_name: str = "",
                          suggested_route: list | None = None) -> dict:
    prefs = _aggregate_preferences(responses)
    dates = dict(prefs["travel_dates"])
    duration = _duration_days(dates)

    # Multi-city groups get a route first (cities, order, nights) — everything
    # else (stays per city, transport legs, open-jaw flights) hangs off it.
    route = plan_route(destination, prefs, trip_name, seed_route=suggested_route)

    def city_stays_in_sequence() -> dict[str, list]:
        """Live hotels per route city, searched one city at a time — parallel
        per-city calls brush LiteAPI's rate limit and whole cities silently
        fall back to AI estimates. The anchor's country doubles as a hint for
        wishlist cities typed without one."""
        country_hint = destination.split(",")[-1].strip() if "," in destination else ""
        found = {}
        for c in route["cities"]:
            try:
                found[c["town"]] = real_stay_options(
                    c["city"], c.get("start", ""), c.get("end", ""),
                    prefs["group_size"], max(c["nights"], 1),
                    country_hint, prefs["accommodation_votes"],
                )
            except Exception:
                found[c["town"]] = []
        return found

    # The slow, independent fetches run concurrently: the visits/events menu,
    # the stay menu, the per-city flights prompt, transport legs, and the live
    # hotel search — the board is ready when the slowest of them lands, not
    # when their sum does.
    with ThreadPoolExecutor(max_workers=6) as pool:
        if ai_available():
            base_future = pool.submit(_ai_activity_options, destination, prefs, trip_name, route)
            ai_stay_future = pool.submit(_ai_stay_options, destination, prefs, trip_name, route)
        else:
            base_future = pool.submit(_sample_trip_options, destination, prefs, route)
            ai_stay_future = None
        flights_future = pool.submit(_flight_options, destination, prefs, route)
        transport_future = pool.submit(_transport_options, route, prefs) if route else None
        if route:
            city_stays_future = pool.submit(city_stays_in_sequence)
        else:
            stays_future = pool.submit(
                real_stay_options, destination, dates.get("start", ""), dates.get("end", ""),
                prefs["group_size"], duration, "", prefs["accommodation_votes"],
            )
        options = base_future.result()
        if ai_stay_future is not None:
            try:
                options["stay"] = ai_stay_future.result()
            except Exception:
                options["stay"] = []  # _ensure_categories backfills below
        options["flights"] = flights_future.result()
        if transport_future:
            options["transport"] = transport_future.result()

        if route:
            options["route"] = route
            options["trip_mode"] = "multi"
            _ensure_categories(options, destination, prefs, route)
            # Per city: live hotels for that segment's dates when available,
            # otherwise the AI's suggestions for that city.
            try:
                live_by_town = city_stays_future.result()
            except Exception:
                live_by_town = {}
            ai_stays = options.get("stay") or []
            merged, any_live = [], False
            for c in route["cities"]:
                live = live_by_town.get(c["town"]) or []
                chosen = live[:3] if live else [o for o in ai_stays if _same_town(o.get("city", ""), c["city"])][:3]
                any_live = any_live or bool(live)
                for o in chosen:
                    o["city"] = c["city"]
                merged.extend(chosen)
            if merged:
                options["stay"] = merged
            else:  # the AI ignored the per-city instruction — spread what it gave
                for i, o in enumerate(ai_stays):
                    o["city"] = route["cities"][i % len(route["cities"])]["city"]
                options["stay"] = ai_stays
            # No city may end up without a stay to star
            for c in route["cities"]:
                if not any(_same_town(s.get("city", ""), c["city"]) for s in options["stay"]):
                    options["stay"].extend(_sample_city_stays(c, prefs))
            if any_live:
                options["stay_source"] = "liteapi"
                options["stay_all_live"] = all(o.get("live_price") for o in options["stay"])
        else:
            options["trip_mode"] = "single"
            _ensure_categories(options, destination, prefs, None)
            live_stays = stays_future.result()

    options["destination"] = options.get("destination") or destination
    options.setdefault("dates", dates)

    if not route:
        # Destinations like "Porto Cervo, Sardinia" name a region, not a country —
        # retry the live-hotel search with the AI's country answer as the hint.
        if not live_stays and options.get("country"):
            live_stays = real_stay_options(
                destination, dates.get("start", ""), dates.get("end", ""),
                prefs["group_size"], duration, country_hint=options["country"],
                type_votes=prefs["accommodation_votes"],
            )
        if live_stays:
            options["stay"] = live_stays
            options["stay_source"] = "liteapi"

    options = _assign_option_ids(options)
    options = _attach_option_links(options, prefs, destination)
    options = _attach_images(options, destination)
    return options


def _attach_images(options: dict, destination: str) -> dict:
    """Photos for the destination and every stay/visit/event option.

    Fetched once here and stored with the options, so the UI never queries
    the image API. All lookups are best-effort — None just means no photo.
    """
    town = destination.split(",")[0].strip()

    def opt_town(opt) -> str:
        # Multi-city options carry their own city — photos should match it
        return (opt.get("city") or "").split(",")[0].strip() or town

    def stay_image(opt, idx):
        # Don't query hotel NAMES: Pexels fuzzy-matches them to the same
        # generic destination photo. Deal each hotel a different photo (by
        # index) from the shared pool instead.
        t = opt_town(opt)
        return (nth_image(f"{opt.get('type', 'hotel')} {t}", idx)
                or nth_image(f"hotel {t}", idx)
                or nth_image(t, idx))

    jobs = [(options, "image", (f"{destination} travel", town))]
    fns = []
    for idx, opt in enumerate(options.get("stay", [])):
        if not opt.get("image"):  # real hotels carry their own photo
            fns.append((opt, "image", (lambda o=opt, i=idx: stay_image(o, i))))
    for opt in options.get("visits", []):
        jobs.append((opt, "image", (
            f"{opt.get('name', '')} {opt_town(opt)}",
            f"{opt_town(opt)} {opt.get('category', '')}".strip(),
            opt_town(opt),
        )))
    for opt in options.get("events", []):
        jobs.append((opt, "image", (f"{opt.get('name', '')}", f"{opt_town(opt)} evening")))

    # ~20 sequential Pexels lookups were a large slice of board-generation
    # time; fetch them concurrently instead.
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [(target, key, pool.submit(first_image, *queries)) for target, key, queries in jobs]
        futures += [(target, key, pool.submit(fn)) for target, key, fn in fns]
        for target, key, fut in futures:
            try:
                target[key] = fut.result()
            except Exception:
                target[key] = None
    return options


def _flight_options(destination: str, prefs: dict, route: dict | None = None) -> dict:
    cities = prefs["departure_cities"] or ["Your city"]
    dates = prefs["travel_dates"]
    raw = {}
    if ai_available():
        try:
            raw = _ai_flights(destination, cities, dates, prefs["group_size"], route,
                              prefs.get("return_by_city", {}))
        except Exception:
            raw = {}  # never let flight generation sink the whole board

    # Normalize: every departure city gets options under ITS exact name,
    # matching AI keys case-insensitively and filling gaps with samples.
    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    raw_by_norm = {norm(k): v for k, v in raw.items() if isinstance(v, list) and v}
    flights = {}
    for city in cities:
        flights[city] = raw_by_norm.get(norm(city)) or _sample_flights_for_city(
            city, destination, dates, route, prefs.get("return_by_city", {}).get(city, ""))
    return flights


def _ai_flights(destination: str, cities: list[str], dates: dict, group_size: int,
                route: dict | None = None, return_by_city: dict | None = None) -> dict:
    keys = ", ".join(f'"{c}"' for c in cities)
    arrive = route["cities"][0]["city"] if route else destination
    leave_from = route["cities"][-1]["city"] if route else destination
    shape = (
        f"an OPEN-JAW round trip: fly INTO {arrive} on {dates['start']} and fly HOME FROM {leave_from} on {dates['end']} "
        f"(the group travels overland between them)"
        if route and not _same_town(arrive, leave_from)
        else f"a round trip to {destination}, out {dates['start']}, back {dates['end']}"
    )
    returns = "; ".join(
        f"travelers from {c} fly home to {r}" for c, r in (return_by_city or {}).items()
    )
    return_note = f"\nDifferent return airports: {returns}. Everyone else returns to their departure city." if returns \
        else "\nEvery traveler returns to their own departure city."

    prompt = f"""You are a flight-search expert. For EACH departure city below, produce exactly 3 realistic flight options for {shape}. Use plausible airlines, routes (with real airport codes), times and prices for each specific origin. Every option covers BOTH directions — price is the full round trip per person.{return_note}

Departure cities: {', '.join(cities)}

The three options per city should cover different trade-offs: fastest, cheapest, best times.

Respond with ONLY valid JSON — an object whose keys are EXACTLY these strings: {keys}
{{
  "{cities[0]}": [
    {{"airline": "string", "route": "JFK → SPU (via VIE)", "return_route": "SPU → JFK (via FCO)",
      "depart": "{dates['start']} 09:40", "return": "{dates['end']} 17:10", "stops": 1,
      "duration": "11h 20m", "est_price_per_person": 640, "why": "one line on the trade-off"}}
  ]
}}"""
    result = _ask(prompt, max_tokens=4096, tier="fast")
    return result if isinstance(result, dict) else {}


def _sample_flights_for_city(city: str, destination: str, dates: dict,
                             route: dict | None = None, return_to: str = "") -> list[dict]:
    arrive = (route["cities"][0]["town"] if route else destination.split(",")[0].strip())
    leave_from = (route["cities"][-1]["town"] if route else destination.split(",")[0].strip())
    home = return_to or city
    return [
        {"airline": "Lufthansa", "route": f"{city} → {arrive} (1 stop)",
         "return_route": f"{leave_from} → {home} (1 stop)",
         "depart": f"{dates['start']} 08:30", "return": f"{dates['end']} 18:45",
         "stops": 1, "duration": "9h 10m", "est_price_per_person": 620,
         "why": "Best balance of price and schedule"},
        {"airline": "Budget carrier combo", "route": f"{city} → {arrive} (2 stops)",
         "return_route": f"{leave_from} → {home} (2 stops)",
         "depart": f"{dates['start']} 06:05", "return": f"{dates['end']} 22:30",
         "stops": 2, "duration": "13h 40m", "est_price_per_person": 380,
         "why": "Cheapest — early start, long day"},
        {"airline": "Turkish Airlines", "route": f"{city} → {arrive} (1 stop)",
         "return_route": f"{leave_from} → {home} (1 stop)",
         "depart": f"{dates['start']} 11:20", "return": f"{dates['end']} 15:10",
         "stops": 1, "duration": "8h 05m", "est_price_per_person": 760,
         "why": "Most comfortable times, checked bag included"},
    ]


def _transport_options(route: dict | None, prefs: dict) -> dict:
    """2-3 ways to make each leg of a multi-city route (train / flight / bus /
    ferry). A starrable category of its own — the group votes leg by leg."""
    if not route:
        return {}
    raw = {}
    if ai_available():
        try:
            raw = _ai_transport(route, prefs)
        except Exception:
            raw = {}  # never let transport generation sink the whole board
    out = {}
    for leg in route["legs"]:
        leg_options = raw.get(leg["key"])
        if not isinstance(leg_options, list) or not leg_options:
            leg_options = _sample_transport_for_leg(leg)
        out[leg["key"]] = leg_options
    return out


def _ai_transport(route: dict, prefs: dict) -> dict:
    legs_desc = "\n".join(
        f'- "{leg["key"]}": {leg["from_city"]} to {leg["to_city"]} on {leg["date"] or "the transfer day"}'
        for leg in route["legs"]
    )
    keys = ", ".join(f'"{leg["key"]}"' for leg in route["legs"])
    first = route["legs"][0]
    prompt = f"""You are an intercity travel expert planning the legs of a multi-city group trip for {prefs['group_size']} people.

Legs to cover (each on its own date, one-way):
{legs_desc}

For EACH leg produce 2-3 realistic options spanning the sensible modes for that pair (train, flight, bus, ferry, rental-car — only what actually operates there). Real carriers, plausible departure times, durations and one-way prices per person. Morning departures preferred — the group checks out of the hotel that day.

Respond with ONLY valid JSON — an object whose keys are EXACTLY: {keys}
{{
  "{first['key']}": [
    {{"mode": "train", "carrier": "TGV inOui", "depart": "09:12", "duration": "1h 56m",
      "est_price_per_person": 45, "why": "one line on the trade-off"}}
  ]
}}"""
    result = _ask(prompt, max_tokens=4096, tier="fast")
    return result if isinstance(result, dict) else {}


def _sample_transport_for_leg(leg: dict) -> list[dict]:
    return [
        {"mode": "train", "carrier": "Intercity rail", "depart": "09:15", "duration": "2h 40m",
         "est_price_per_person": 45, "why": "City center to city center — no airport hassle"},
        {"mode": "flight", "carrier": "Regional airline", "depart": "07:50", "duration": "1h 05m",
         "est_price_per_person": 75, "why": "Fastest in the air — add time for the airports"},
        {"mode": "bus", "carrier": "FlixBus", "depart": "08:30", "duration": "4h 10m",
         "est_price_per_person": 22, "why": "The cheapest seat between the two"},
    ]


def _option_pool_sizes(duration: int) -> tuple[int, int]:
    """How many visit/event options to offer, scaled to the stay length so even
    a very-busy group has enough to choose from."""
    visits = min(12, max(6, round(duration * 1.5) + 2))
    events = min(6, max(4, duration // 2 + 2))
    return visits, events


def _ensure_categories(options: dict, destination: str, prefs: dict, route: dict | None) -> None:
    """Make the board impossible to break: whatever shape the model returned,
    every category ends up a non-empty list of dicts.

    - dict-shaped categories ({"Nice": [...]}) are flattened with the city stamped;
    - non-dict entries are dropped;
    - an empty visits/events gets ONE small targeted retry, then sample data;
    - an empty stay falls back to samples (live hotels may still replace them).
    """
    for cat in ("stay", "visits", "events"):
        val = options.get(cat)
        if isinstance(val, dict):
            flat = []
            for city_key, items in val.items():
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict):
                        item.setdefault("city", city_key)
                        flat.append(item)
            options[cat] = flat
        elif isinstance(val, list):
            options[cat] = [item for item in val if isinstance(item, dict)]
        else:
            options[cat] = []

    sample = None
    for cat in ("visits", "events"):
        if options[cat]:
            continue
        if ai_available():
            try:
                options[cat] = _ai_category_retry(destination, prefs, route, cat)
            except Exception:
                options[cat] = []
        if not options[cat]:
            sample = sample or _sample_trip_options(destination, prefs, route)
            options[cat] = sample[cat]
    if not options["stay"]:
        sample = sample or _sample_trip_options(destination, prefs, route)
        options["stay"] = sample["stay"]


def _ai_category_retry(destination: str, prefs: dict, route: dict | None, cat: str) -> list[dict]:
    """A small, single-purpose prompt to refill one missing board category."""
    dates = prefs["travel_dates"]
    duration = _duration_days(dates)
    n_visits, n_events = _option_pool_sizes(duration)
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    city_note, city_field = "", ""
    if route:
        towns = ", ".join(f'"{c["town"]}"' for c in route["cities"])
        city_note = (f" The trip is a multi-city route ({towns}) — every item needs a \"city\" field "
                     f"set to exactly one of those towns, spread roughly by nights spent there.")
        city_field = '"city": "town", '
    if cat == "visits":
        ask = f'exactly {n_visits} real sights/attractions/day-trips worth visiting'
        schema = f'{{{city_field}"name": "string", "description": "1-2 sentences", "duration": "2h", "cost_per_person": 25, "category": "culture"}}'
    else:
        ask = f'exactly {n_events} time-specific experiences during the dates (concerts, festivals, food tours, classes…)'
        schema = f'{{{city_field}"name": "string", "description": "1-2 sentences", "date_hint": "evenings in July / July 18", "cost_per_person": 40}}'
    prompt = f"""For a group trip to {destination}, {dates['start']} to {dates['end']}, vibes: {vibes} — produce {ask}.{city_note}

Respond with ONLY a valid JSON array:
[{schema}]"""
    result = _ask(prompt, max_tokens=4096, tier="fast")
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Per-option detail sheets (opened from a board card, cached on the trip)
# ---------------------------------------------------------------------------


def _group_profile_block(destination: str, prefs: dict, trip_name: str) -> str:
    budget = prefs["budget_range"]
    dates = prefs["travel_dates"]
    duration = _duration_days(dates)
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"
    return f"""Trip: "{trip_name or destination}"
Group profile:
- Group size: {prefs['group_size']}
- Budget per person: ${budget['min']}–${budget['max']} USD total (flights excluded)
- Dates: {dates['start']} to {dates['end']} ({duration} days)
- Vibes (most voted first): {vibes}
- Special requirements: {requirements}"""


def _route_menu_block(route: dict | None, what: str) -> tuple[str, str]:
    """(rules block, per-item city field) for a multi-city menu prompt."""
    if not route:
        return "", ""
    route_desc = "; ".join(
        f"{c['city']} — {c['nights']} night(s), {c.get('start') or '?'} to {c.get('end') or '?'}"
        for c in route["cities"]
    )
    towns = ", ".join(f'"{c["town"]}"' for c in route["cities"])
    block = f"""
THIS IS A MULTI-CITY TRIP. The route is LOCKED: {route_desc}.
{what} must carry a "city" field set to EXACTLY one of: {towns}."""
    return block, '"city": "one of the route towns", '


# The stay menu and the visits/events menu used to ship in one very large
# generation — the single slowest request in the app. They are independent,
# so they run as two smaller parallel calls (see generate_trip_options).

def _ai_stay_options(destination: str, prefs: dict, trip_name: str, route: dict | None = None) -> list[dict]:
    acc_votes = ", ".join(f"{k}: {v} vote(s)" for k, v in prefs["accommodation_votes"].items()) or "no data"
    route_block, city_field = _route_menu_block(route, "Every stay")
    if route:
        stay_line = (f'2-3 options PER CITY (so {2 * len(route["cities"])}-{3 * len(route["cities"])} total), each '
                     'priced for that city\'s night count only ("total_per_person" covers just those nights).')
    else:
        stay_line = "exactly 4 options covering different styles/price points that match the group's votes."

    prompt = f"""You are an expert travel planner listing accommodation options for a group trip to {destination}. The group will STAR the ones they like, so every option must be a real, distinct, bookable property (real hotel/hostel/apartment names).

{_group_profile_block(destination, prefs, trip_name)}
- Accommodation votes across the group: {acc_votes} — offer options spanning the styles people asked for{route_block}

Produce {stay_line}

Respond with ONLY valid JSON (no markdown):
{{
  "stay": [
    {{{city_field}"name": "string", "type": "boutique hotel", "area": "neighborhood", "price_per_night": 180,
      "total_per_person": 450, "rating": 4.6, "amenities": ["...", "..."],
      "why": "one sentence on who this suits"}}
  ]
}}"""
    result = _ask(prompt, max_tokens=8000 if route else 4096)
    return result.get("stay", []) if isinstance(result, dict) else []


def _ai_activity_options(destination: str, prefs: dict, trip_name: str, route: dict | None = None) -> dict:
    dates = prefs["travel_dates"]
    n_visits, n_events = _option_pool_sizes(_duration_days(dates))
    route_block, city_field = _route_menu_block(route, "Every visit and event")
    if route:
        route_block += ("\n- Spread visits and events across the cities roughly in proportion to nights there."
                        "\n- Events must plausibly run while the group is IN their city (see the per-city date windows above).")

    prompt = f"""You are an expert travel planner assembling a menu of concrete activities for a group trip to {destination}. The group will STAR the options they like — Rovyn then builds the day-by-day itinerary from their starred picks — so every option must be distinct, real and independently doable (real sights, real venues).

{_group_profile_block(destination, prefs, trip_name)}{route_block}

Produce options in TWO categories:
1. "visits" — exactly {n_visits} sights/attractions/day-trips worth visiting (each independently bookable or free). Cover different interests and durations so both packed and relaxed travelers find matches.
2. "events" — exactly {n_events} time-specific experiences during the dates (concerts, festivals, food tours, boat parties, classes…). If nothing specific is verifiable, propose typical seasonal events and mark date_hint accordingly.

Respond with ONLY valid JSON (no markdown):
{{
  "destination": "{destination}",
  "country": "string",
  "dates": {{"start": "{dates['start']}", "end": "{dates['end']}"}},
  "visits": [
    {{{city_field}"name": "string", "description": "1-2 sentences", "duration": "2h", "cost_per_person": 25, "category": "culture"}}
  ],
  "events": [
    {{{city_field}"name": "string", "description": "1-2 sentences", "date_hint": "evenings in July / July 18", "cost_per_person": 40}}
  ]
}}"""
    return _ask(prompt, max_tokens=10000 if route else 8000)


def _sample_city_stays(c: dict, prefs: dict) -> list[dict]:
    """Two placeholder stays for one route city — the deterministic floor when
    neither live hotels nor AI suggestions covered it."""
    t, nights = c["town"], max(c["nights"], 1)
    group = max(prefs["group_size"], 1)
    return [
        {"city": c["city"], "name": f"Boutique Hotel {t}", "type": "boutique hotel",
         "area": "Old town", "price_per_night": 190,
         "total_per_person": 190 * nights // 2, "rating": 4.7,
         "amenities": ["Rooftop terrace", "Breakfast included"],
         "why": f"Charming and central for your {nights} night(s) in {t}."},
        {"city": c["city"], "name": f"{t} Apartment for the Group",
         "type": "airbnb/vacation rental", "area": "Center",
         "price_per_night": 360, "total_per_person": 360 * nights // group,
         "rating": 4.8, "amenities": ["Full kitchen", "Washer"],
         "why": "The whole group under one roof."},
    ]


def _sample_trip_options(destination: str, prefs: dict, route: dict | None = None) -> dict:
    """Deterministic demo options used when ANTHROPIC_API_KEY is not configured."""
    dates = prefs["travel_dates"]
    duration = min(_duration_days(dates), 7)
    n_visits, n_events = _option_pool_sizes(duration)
    town = destination.split(",")[0].strip()

    visit_pool = [
        {"name": f"Old town walking tour of {town}", "description": "Two hours with a local guide through the historic core.", "duration": "2h", "cost_per_person": 25, "category": "culture"},
        {"name": "Boat trip to nearby islands", "description": "Full-day hop with swim stops and lunch aboard.", "duration": "7h", "cost_per_person": 85, "category": "nature"},
        {"name": "Hilltop fortress at sunset", "description": "Short climb, the best panorama in town.", "duration": "1.5h", "cost_per_person": 10, "category": "views"},
        {"name": "Local wine & olive tasting", "description": "Family-run estate just outside town.", "duration": "3h", "cost_per_person": 45, "category": "food"},
        {"name": "Hidden beaches by kayak", "description": "Guided paddle to coves you can't reach on foot.", "duration": "4h", "cost_per_person": 55, "category": "adventure"},
        {"name": "Market morning & food stalls", "description": "Graze the farmers' market like a local.", "duration": "2h", "cost_per_person": 15, "category": "food"},
        {"name": f"{town} museum of local history", "description": "Compact and well-curated — an hour well spent.", "duration": "1.5h", "cost_per_person": 12, "category": "culture"},
        {"name": "Sunrise viewpoint hike", "description": "Early start, unforgettable light, coffee after.", "duration": "3h", "cost_per_person": 0, "category": "nature"},
        {"name": "Bike loop along the coast", "description": "Easy riding, plenty of swim stops.", "duration": "4h", "cost_per_person": 30, "category": "adventure"},
        {"name": "Artisan quarter & workshops", "description": "Meet the makers, take something home.", "duration": "2h", "cost_per_person": 0, "category": "shopping"},
        {"name": "Cooking class with a local family", "description": "Shop the market, cook the classics, eat together.", "duration": "4h", "cost_per_person": 65, "category": "food"},
        {"name": "Cliff-jumping cove", "description": "For the brave — locals show you the safe spots.", "duration": "2h", "cost_per_person": 0, "category": "adventure"},
    ]
    event_pool = [
        {"name": "Open-air summer concert", "description": "Live music in the main square.", "date_hint": "most evenings in season", "cost_per_person": 30},
        {"name": "Full-moon beach party", "description": "DJs on the sand until late.", "date_hint": "check local listings", "cost_per_person": 25},
        {"name": "Traditional festival night", "description": "Folklore, food stalls and fireworks.", "date_hint": "weekend during your stay", "cost_per_person": 0},
        {"name": "Sunset sailing with drinks", "description": "Two-hour catamaran cruise, drinks included.", "date_hint": "daily at 18:30", "cost_per_person": 60},
        {"name": "Night food market", "description": "Street food, live bands, local crowd.", "date_hint": "Fridays & Saturdays", "cost_per_person": 15},
        {"name": "Open-air cinema under the stars", "description": "Classics with subtitles, beanbags and wine.", "date_hint": "Tuesdays at 21:00", "cost_per_person": 10},
    ]

    if route:
        # Two stays per route city, priced for that city's nights; visits and
        # events dealt across the cities in proportion to nights spent there.
        stay = []
        for c in route["cities"]:
            stay.extend(_sample_city_stays(c, prefs))
        visits = [dict(v) for v in visit_pool[:n_visits]]
        events = [dict(e) for e in event_pool[:n_events]]
        weights = [max(c["nights"], 1) for c in route["cities"]]
        for items in (visits, events):
            i = 0
            for c, w in zip(route["cities"], weights):
                share = max(1, round(len(items) * w / sum(weights)))
                for item in items[i:i + share]:
                    item["city"] = c["city"]
                i += share
            for item in items:  # anything past the rounding goes to the anchor
                item.setdefault("city", route["cities"][0]["city"])
        return {
            "destination": destination,
            "country": "",
            "dates": {"start": dates["start"], "end": dates["end"]},
            "source": "sample",
            "stay": stay,
            "visits": visits,
            "events": events,
        }

    return {
        "destination": destination,
        "country": "",
        "dates": {"start": dates["start"], "end": dates["end"]},
        "source": "sample",
        "stay": [
            {"name": f"Boutique Hotel {town}", "type": "boutique hotel", "area": "Old town",
             "price_per_night": 190, "total_per_person": 190 * duration // 2, "rating": 4.7,
             "amenities": ["Rooftop terrace", "Breakfast included"], "why": "Charming and central — for the design lovers."},
            {"name": "Seaside Villa with Pool", "type": "airbnb/vacation rental", "area": "Waterfront",
             "price_per_night": 420, "total_per_person": 420 * duration // max(prefs["group_size"], 1), "rating": 4.9,
             "amenities": ["Private pool", "Full kitchen", "Sea view"], "why": "The whole group under one roof."},
            {"name": "Central 4-star Hotel", "type": "4-star hotel", "area": "Center",
             "price_per_night": 150, "total_per_person": 150 * duration // 2, "rating": 4.4,
             "amenities": ["Pool", "Gym", "AC"], "why": "Reliable comfort, easy logistics."},
            {"name": "Old Town Hostel — private rooms", "type": "hostel", "area": "Old town",
             "price_per_night": 55, "total_per_person": 55 * duration, "rating": 4.2,
             "amenities": ["Social bar", "Kitchen"], "why": "Cheapest way to stay central."},
        ],
        "visits": visit_pool[:n_visits],
        "events": event_pool[:n_events],
    }


# ---------------------------------------------------------------------------
# Final plan: deterministic majority-rule selection + AI narrative assembly
# ---------------------------------------------------------------------------

def _tally_summary(tally: dict) -> tuple[int, int, int]:
    """Returns (active_voters, no_preference, total_members)."""
    return tally.get("active_voters", 0), tally.get("no_preference", 0), tally.get("total_members", 0)


def _rationale(label: str, stars: int, tally: dict, kind: str) -> str:
    active, nopref, total = _tally_summary(tally)
    parts = []
    if active > 0 and stars > 0:
        pct = round(100 * stars / active)
        parts.append(f"{stars} of {active} traveler{'s' if active != 1 else ''} with an opinion ({pct}%) starred {label}")
    elif stars == 0:
        parts.append(f"No {kind} received stars, so Rovyn picked the best overall fit: {label}")
    if nopref:
        parts.append(f"{nopref} said they don't mind")
    return "; ".join(parts) + "."


def _pick_winner(option_list: list[dict], counts: dict) -> tuple[dict, int]:
    """Most-starred option; ties and zero-star cases fall back to list order."""
    if not option_list:
        return {}, 0
    best = max(option_list, key=lambda o: (counts.get(o.get("id"), 0), -option_list.index(o)))
    return best, counts.get(best.get("id"), 0)


def _route_town(route: dict, city: str) -> str:
    """Map an option's city label onto a route town; unknowns land on the anchor."""
    for c in route["cities"]:
        if _same_town(c["city"], city):
            return c["town"]
    return route["cities"][0]["town"]


def select_winners(options: dict, tallies: dict, prefs: dict | None = None) -> tuple[dict, list[dict]]:
    """Deterministic majority-rule resolution of the group's stars.

    Returns (selection, decisions). Selection holds the chosen option objects;
    decisions hold the human-readable 'why' for each category.
    """
    prefs = prefs or {}
    decisions = []
    route = options.get("route")

    stay_counts = tallies.get("stay", {}).get("counts", {})
    stay, stays = {}, []
    if route:
        # One stay per route city — each city's most-starred option wins there.
        stay_pool = options.get("stay", [])
        stay_notes, stay_items = [], []
        for c in route["cities"]:
            city_pool = [o for o in stay_pool if _same_town(o.get("city", ""), c["city"])] or stay_pool
            winner, stars = _pick_winner(city_pool, stay_counts)
            winner = dict(winner)
            winner["city"] = c["city"]
            winner["check_in"], winner["check_out"] = c.get("start", ""), c.get("end", "")
            winner["nights"] = c["nights"]
            stays.append(winner)
            stay_items.append({"name": f"{c['town']} — {winner.get('name', '—')}", "stars": stars})
            stay_notes.append(f"{c['town']}: “{winner.get('name')}”"
                              + (f" ({stars} star{'s' if stars != 1 else ''})" if stars else " (best overall fit)"))
        decisions.append({
            "category": "stay",
            "choice": f"One stay per city — {len(stays)} in total",
            "rationale": "The most-starred stay won in each city: " + "; ".join(stay_notes) + ".",
            "items": stay_items,
        })
    else:
        stay, stay_stars = _pick_winner(options.get("stay", []), stay_counts)
        decisions.append({
            "category": "stay",
            "choice": stay.get("name", "—"),
            "rationale": _rationale(f"“{stay.get('name')}” ({stay.get('type')})", stay_stars, tallies.get("stay", {}), "stay"),
        })

    flight_counts = tallies.get("flights", {}).get("counts", {})
    chosen_flights = {}
    flight_notes = []
    for city, city_options in (options.get("flights") or {}).items():
        chosen, stars = _pick_winner(city_options, flight_counts)
        chosen_flights[city] = chosen
        note = f"{city}: {chosen.get('airline', '?')} (${chosen.get('est_price_per_person', 0)} est.)"
        if stars:
            note += f" — {stars} star{'s' if stars != 1 else ''}"
        flight_notes.append(note)
    flight_shape = ""
    if route and len(route["cities"]) > 1:
        flight_shape = (f" Every option is a round trip that lands in {route['cities'][0]['town']} "
                        f"and flies home from {route['cities'][-1]['town']}.")
    decisions.append({
        "category": "flights",
        "choice": "One round trip per departure city",
        "rationale": "Flights are personal — each city's travelers picked their own. Most-starred per city: "
                     + "; ".join(flight_notes) + f". Your check reflects your own city's flight.{flight_shape}",
    })

    # Transport between the route's cities: the most-starred way wins each leg.
    chosen_transport = []
    if route:
        transport_counts = tallies.get("transport", {}).get("counts", {})
        transport_notes = []
        for leg in route["legs"]:
            leg_options = (options.get("transport") or {}).get(leg["key"], [])
            winner, stars = _pick_winner(leg_options, transport_counts)
            chosen_transport.append({**winner, "leg": leg["key"], "from": leg["from"],
                                     "to": leg["to"], "date": leg["date"], "day": leg["day"]})
            note = f"{leg['key']}: {winner.get('mode', '?')} ({winner.get('carrier', '')})".strip()
            if stars:
                note += f" — {stars} star{'s' if stars != 1 else ''}"
            transport_notes.append(note)
        if chosen_transport:
            decisions.append({
                "category": "transport",
                "choice": f"{len(chosen_transport)} leg{'s' if len(chosen_transport) != 1 else ''} between cities",
                "rationale": "Leg by leg, the group's most-starred way to travel won: "
                             + "; ".join(transport_notes) + ".",
            })

    # Visits & events are additive: everything starred makes the plan,
    # most-loved first (capped so the trip stays feasible).
    duration = _duration_days(options.get("dates", {}))
    group_pace = prefs.get("group_pace", "moderately")
    caps = recommended_picks(duration, group_pace)
    for cat, label in (("visits", "visit"), ("events", "event")):
        cap = max(caps[cat], 3)
        counts = tallies.get(cat, {}).get("counts", {})
        pool = options.get(cat, [])
        starred = sorted(
            [o for o in pool if counts.get(o.get("id"), 0) > 0],
            key=lambda o: -counts.get(o.get("id"), 0),
        )[:cap]
        if not starred:
            starred = pool[:3]
            rationale = f"No {label}s were starred, so Rovyn kept the three most essential:"
        else:
            rationale = f"Every {label} the group starred made the plan, most-loved first:"

        # Multi-city time-frame check: the group is only in each city for its
        # segment, so a city can't host more evening events than it has nights.
        # When two group favorites collide, the most-starred one wins the slot.
        if route and cat == "events" and starred:
            by_town: dict[str, list] = {}
            for e in starred:
                by_town.setdefault(_route_town(route, e.get("city", "")), []).append(e)
            nights_by_town = {c["town"]: max(c["nights"], 1) for c in route["cities"]}
            kept_ids, conflict_notes = set(), []
            for town_name, town_events in by_town.items():
                capacity = nights_by_town.get(town_name, 1)
                ranked = sorted(town_events, key=lambda o: -counts.get(o.get("id"), 0))
                kept_ids.update(o.get("id") for o in ranked[:capacity])
                for dropped in ranked[capacity:]:
                    kept_names = " and ".join(f"“{o.get('name')}”" for o in ranked[:capacity])
                    conflict_notes.append(
                        f"the group is only in {town_name} for {capacity} night{'s' if capacity != 1 else ''}, "
                        f"so “{dropped.get('name')}” ({counts.get(dropped.get('id'), 0)}★) couldn't fit — "
                        f"{kept_names} won the evening{'s' if capacity != 1 else ''}"
                    )
            if conflict_notes:
                starred = [e for e in starred if e.get("id") in kept_ids]
                rationale += " Time-frame check: " + "; ".join(conflict_notes) + "."

        items = [{"name": o.get("name"), "stars": counts.get(o.get("id"), 0)} for o in starred]
        decisions.append({"category": cat, "choice": f"{len(starred)} selected", "rationale": rationale, "items": items})
        if cat == "visits":
            visits = starred
        else:
            events = starred

    pace_label = {"very": "very busy", "moderately": "moderately busy", "slightly": "slightly busy"}.get(group_pace, "moderately busy")
    itinerary_note = ""
    if route:
        towns = " → ".join(c["town"] for c in route["cities"])
        itinerary_note = f" Days follow the route ({towns}); each visit and event is scheduled while the group is in its city."
    decisions.append({
        "category": "itinerary",
        "choice": "Built from your picks",
        "rationale": f"Rovyn assembled the day-by-day schedule from the group's starred visits and events at a {pace_label} pace "
                     f"(the group's most common answer). Every slot can still be edited before approval.{itinerary_note}",
    })

    selection = {
        "stay": stay,
        "stays": stays,
        "transport": chosen_transport,
        "flights": chosen_flights,
        "visits": visits,
        "events": events,
    }
    return selection, decisions


def compute_costs(selection: dict, prefs: dict, duration: int) -> tuple[dict, dict]:
    """Deterministic per-person cost math shared by the plan and the check."""
    stays = selection.get("stays") or []
    if stays:  # multi-city: the sum of each city's stay
        stay_cost = round(sum(s.get("total_per_person") or 0 for s in stays))
    else:
        stay_cost = round((selection.get("stay") or {}).get("total_per_person") or 0)
    intercity = round(sum(t.get("est_price_per_person") or 0 for t in selection.get("transport") or []))
    visits_cost = round(sum(v.get("cost_per_person") or 0 for v in selection.get("visits", [])))
    events_cost = round(sum(e.get("cost_per_person") or 0 for e in selection.get("events", [])))
    meals = 50 * duration
    transport = 80
    shared_total = stay_cost + intercity + visits_cost + events_cost + meals + transport
    cost_per_person = {
        "stay": stay_cost,
        "intercity_transport": intercity,
        "visits": visits_cost,
        "events": events_cost,
        "meals_estimate": meals,
        "transport_estimate": transport,
        "shared_total": shared_total,
    }
    flight_cost_by_city = {
        city: round(f.get("est_price_per_person") or 0)
        for city, f in (selection.get("flights") or {}).items()
    }
    return cost_per_person, flight_cost_by_city


def recompute_plan_costs(plan: dict) -> dict:
    """Re-derive cost_per_person after slot edits change visits/events."""
    selection = {
        "stay": plan.get("stay") or {},
        "stays": plan.get("stays") or [],
        "transport": plan.get("transport") or [],
        "flights": plan.get("flights", {}),
        "visits": plan.get("visits", []),
        "events": plan.get("events", []),
    }
    duration = plan.get("duration_days") or len(plan.get("itinerary", {}).get("days", [])) or 5
    cost_per_person, flight_cost_by_city = compute_costs(selection, {}, duration)
    plan["cost_per_person"] = cost_per_person
    plan["flight_cost_by_city"] = flight_cost_by_city
    return plan


def generate_final_plan(destination: str, responses: list[dict], options: dict, tallies: dict) -> dict:
    prefs = _aggregate_preferences(responses)
    duration = _duration_days(options.get("dates", prefs["travel_dates"]))
    selection, decisions = select_winners(options, tallies, prefs)
    cost_per_person, flight_cost_by_city = compute_costs(selection, prefs, duration)
    dates = options.get("dates", prefs["travel_dates"])
    route = options.get("route")

    # Accuracy: link each chosen stay's official website when discoverable
    for chosen_stay in (selection.get("stays") or ([selection["stay"]] if selection.get("stay") else [])):
        if chosen_stay.get("name") and not chosen_stay.get("official_website"):
            chosen_stay["official_website"] = official_website(
                chosen_stay["name"], chosen_stay.get("city") or destination)

    if ai_available():
        try:
            woven = _ai_weave_plan(destination, prefs, selection, dates, duration, route)
            if isinstance(woven, list):  # model replied with a bare days array
                woven = {"days": woven}
            if not isinstance(woven, dict) or not woven.get("days"):
                raise ValueError("malformed weave response")
        except Exception:
            # Finalizing must never fail outright: fall back to the
            # deterministic weave — same picks, simpler day arrangement.
            woven = _sample_weave_plan(destination, prefs, selection, dates, duration, route)
            woven["tips"] = [
                "The AI scheduler was unreachable, so Rovyn arranged your picks automatically — "
                "drag anything you'd like to rearrange, or use “Request a change”.",
            ] + [t for t in woven.get("tips", []) if "sample mode" not in t]
    else:
        woven = _sample_weave_plan(destination, prefs, selection, dates, duration, route)

    if route:  # the calendar is code's job — every day carries its route city
        _stamp_day_cities(woven.get("days", []), route)

    plan = {
        "destination": destination,
        "country": options.get("country", ""),
        "image": options.get("image"),
        "dates": dates,
        "duration_days": duration,
        # The band everyone agreed to in the questionnaire (highest floor to
        # lowest ceiling; flights excluded) — the UI compares the final costs
        # against it so the group sees whether the plan fits the budget.
        "budget_per_person": prefs["budget_range"],
        "trip_mode": "multi" if route else "single",
        "summary": woven.get("summary", f"The group's plan for {destination}."),
        "decisions": decisions,
        "flights": selection["flights"],
        "itinerary": {
            "title": woven.get("title") or f"Your days in {destination.split(',')[0].strip()}",
            "days": woven.get("days", []),
        },
        "visits": selection["visits"],
        "events": selection["events"],
        "cost_per_person": cost_per_person,
        "flight_cost_by_city": flight_cost_by_city,
        "tips": woven.get("tips", []),
        "revision_notes": [],
        "source": "sample" if not ai_available() else "ai",
    }
    if route:
        plan["route"] = route
        plan["stays"] = selection["stays"]
        plan["transport"] = selection["transport"]
    else:
        plan["stay"] = selection["stay"]
    return plan


def _stamp_day_cities(days: list[dict], route: dict) -> None:
    """Force each day's "city" to the route's town for that day number — the
    model writes the schedule, but the route's calendar is not negotiable."""
    town_by_day = {}
    for c in route["cities"]:
        for day_num in range(c["day_start"], c["day_end"] + 1):
            town_by_day[day_num] = c["town"]
    for i, day in enumerate(days):
        day["city"] = town_by_day.get(day.get("day", i + 1)) or route["cities"][-1]["town"]


def _pace_note(pace: str) -> str:
    return {
        "very": "The group wants to be VERY busy: fill mornings, afternoons and most evenings; free time is the exception.",
        "moderately": "The group wants a MODERATE pace: one or two anchors per day, keep some slots easy or free.",
        "slightly": "The group wants a RELAXED trip: at most one anchor per day, plenty of free time, slow mornings.",
    }.get(pace, "Keep a moderate pace.")


def _transfer_slot_text(leg: dict) -> str:
    """The fixed morning slot of a transfer day, e.g.
    "Check out & train to Lyon — TGV inOui, dep 09:12, 1h 56m"."""
    mode = leg.get("mode") or "travel"
    parts = [p for p in (leg.get("carrier"),
                         f"dep {leg['depart']}" if leg.get("depart") else "",
                         leg.get("duration")) if p]
    detail = f" — {', '.join(parts)}" if parts else ""
    return f"Check out & {mode} to {leg['to']}{detail}"


def _ai_weave_plan(destination: str, prefs: dict, selection: dict, dates: dict,
                   duration: int, route: dict | None = None) -> dict:
    visits = selection["visits"]
    events = selection["events"]
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"

    if route:
        seg_lines = "\n".join(
            f"- {c['town']}: days {c['day_start']}–{c['day_end']} ({c.get('start') or '?'} to {c.get('end') or '?'}), "
            f"staying at {s.get('name', 'the chosen stay')}"
            for c, s in zip(route["cities"], selection.get("stays") or [{}] * len(route["cities"]))
        )
        transfer_lines = "\n".join(
            f"- Day {t['day']} morning MUST be exactly: \"{_transfer_slot_text(t)}\" — then check-in in {t['to']}; keep that day light"
            for t in selection.get("transport") or []
        )
        first_town = route["cities"][0]["town"]
        last_town = route["cities"][-1]["town"]
        route_block = f"""
THE ROUTE IS LOCKED — the group moves between cities on fixed days:
{seg_lines}

Transfer days (fixed moments, never move them):
{transfer_lines}

Every day object must include "city" set to that day's town exactly as written above. Day 1 is the arrival in {first_town}; the last day is check-out and departure home from {last_town}. Each visit and event below carries its "city" — schedule it ONLY on a day the group is in that city."""
        arrival_rule = f'day 1 is arrival in {first_town} — its morning (or afternoon, if flights land late) must be "Arrival in {first_town} & hotel check-in" and the day kept light'
        departure_rule = f'The last day is departure — its morning must be "Hotel check-out & departure" from {last_town} with nothing scheduled after'
        day_schema = '{"day": 1, "date": "YYYY-MM-DD", "city": "town name", "theme": "string", "morning": "string", "afternoon": "string", "evening": "string"}'
        item_keys = ("name", "city", "duration", "cost_per_person")
        event_keys = ("name", "city", "date_hint", "cost_per_person")
    else:
        route_block = ""
        arrival_rule = 'day 1 is arrival — its morning (or afternoon, if flights land late) must be "Arrival & hotel check-in" and the day kept light'
        departure_rule = 'The last day is departure — its morning must be "Hotel check-out & departure" with nothing scheduled after'
        day_schema = '{"day": 1, "date": "YYYY-MM-DD", "theme": "string", "morning": "string", "afternoon": "string", "evening": "string"}'
        item_keys = ("name", "duration", "cost_per_person")
        event_keys = ("name", "date_hint", "cost_per_person")

    prompt = f"""You are building the day-by-day itinerary for a group trip to {destination}, {dates.get('start')} to {dates.get('end')} ({duration} days). The group starred these picks; they are LOCKED — every single one must appear exactly once. Fill remaining slots with light, fitting suggestions (breakfast spots, strolls, beach time) or "Free time".
{route_block}
Pace: {_pace_note(prefs.get('group_pace', 'moderately'))}

Must-include visits:
{json.dumps([{k: v.get(k) for k in item_keys} for v in visits], indent=1)}

Must-include events (respect their date_hint when placing them; evening-flavored events go in evening slots):
{json.dumps([{k: e.get(k) for k in event_keys} for e in events], indent=1)}

Special requirements: {requirements}

Rules: {arrival_rule}. {departure_rule}. These fixed moments anchor the schedule; travelers can rearrange everything else later. Don't overpack any day. Give the itinerary a short evocative title. Also write a warm 2-3 sentence summary mentioning how the group's votes shaped the trip, and 3 practical local tips.

Respond with ONLY valid JSON:
{{
  "title": "string",
  "summary": "string",
  "days": [{day_schema}],
  "tips": ["tip 1", "tip 2", "tip 3"]
}}"""

    return _ask(prompt, max_tokens=12000)


def _sample_weave_plan(destination: str, prefs: dict, selection: dict, dates: dict,
                       duration: int, route: dict | None = None) -> dict:
    try:
        start = dt.strptime(dates.get("start", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        start = dt.now()

    town = destination.split(",")[0].strip()

    if route:
        return _sample_weave_multi(prefs, selection, start, duration, route)

    days = [{
        "day": i + 1,
        "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
        "theme": "Arrival & first stroll" if i == 0 else ("Departure day" if i == duration - 1 else f"Exploring {town}"),
        "morning": "Arrival & hotel check-in" if i == 0
                   else ("Hotel check-out & departure" if i == duration - 1 else "Café breakfast & slow start"),
        "afternoon": "Neighborhood wander" if i == 0 else "Free time",
        "evening": "Welcome dinner" if i == 0 else "Free evening",
    } for i in range(max(duration, 1))]

    # Distribute starred visits into morning/afternoon slots (skip arrival day),
    # and events into evenings — density follows the group's pace.
    slots = []
    for i in range(1, len(days)):
        if i == len(days) - 1:
            continue  # departure day stays light
        slots.extend([(i, "afternoon"), (i, "morning")])
    for j, visit in enumerate(selection["visits"]):
        if j < len(slots):
            i, period = slots[j]
            days[i][period] = f"{visit['name']} — {visit.get('description', '')}"
    evening_days = list(range(1, max(len(days) - 1, 1)))
    for j, event in enumerate(selection["events"]):
        if j < len(evening_days):
            days[evening_days[j]]["evening"] = f"{event['name']} — {event.get('description', '')}"

    return {
        "title": f"Your days in {town}",
        "summary": f"Rovyn built this schedule from the group's starred picks in {town}, "
                   f"staying at {selection['stay'].get('name', 'your chosen stay')} — every starred visit and event has a slot.",
        "days": days,
        "tips": [
            "This plan was assembled in sample mode — add ANTHROPIC_API_KEY to Rovyn/.env for a smarter weave.",
            "Book starred activities 3–4 weeks ahead for group availability.",
            "Keep one afternoon free — the best group memories are unplanned.",
        ],
    }


def _sample_weave_multi(prefs: dict, selection: dict, start, duration: int, route: dict) -> dict:
    """Deterministic multi-city weave: fixed arrival/transfer/departure moments,
    then each city's starred picks distributed across that city's own days."""
    transfers = {t["day"]: t for t in selection.get("transport") or []}
    days = []
    for c in route["cities"]:
        for day_num in range(c["day_start"], c["day_end"] + 1):
            i = day_num - 1
            day = {
                "day": day_num,
                "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
                "city": c["town"],
                "theme": f"Exploring {c['town']}",
                "morning": "Café breakfast & slow start",
                "afternoon": "Free time",
                "evening": "Free evening",
            }
            if day_num == 1:
                day.update(theme=f"Arrival in {c['town']}",
                           morning=f"Arrival in {c['town']} & hotel check-in",
                           afternoon="Neighborhood wander", evening="Welcome dinner")
            elif day_num in transfers:
                leg = transfers[day_num]
                day.update(theme=f"On to {leg['to']}",
                           morning=_transfer_slot_text(leg),
                           afternoon=f"Check in & first stroll in {leg['to']}")
            if day_num == duration:
                day.update(theme="Departure day",
                           morning="Hotel check-out & departure",
                           afternoon="Journey home", evening="Free evening")
            days.append(day)

    # Each city's picks go into that city's days: visits into free afternoons
    # then mornings, events into free evenings (never the departure day).
    by_day_num = {d["day"]: d for d in days}
    for c in route["cities"]:
        town_days = [by_day_num[n] for n in range(c["day_start"], c["day_end"] + 1)]
        open_days = [d for d in town_days
                     if d["day"] != 1 and d["day"] != duration and d["day"] not in transfers]
        slot_queue = [(d, "afternoon") for d in open_days] + [(d, "morning") for d in open_days]
        visits_here = [v for v in selection["visits"] if _route_town(route, v.get("city", "")) == c["town"]]
        for visit, (d, period) in zip(visits_here, slot_queue):
            d[period] = f"{visit['name']} — {visit.get('description', '')}"
        evening_queue = [d for d in town_days if d["day"] != duration]
        events_here = [e for e in selection["events"] if _route_town(route, e.get("city", "")) == c["town"]]
        for event, d in zip(events_here, evening_queue):
            d["evening"] = f"{event['name']} — {event.get('description', '')}"

    towns = " → ".join(c["town"] for c in route["cities"])
    stay_names = ", ".join(f"{s.get('name', 'your stay')} in {_route_town(route, s.get('city', ''))}"
                           for s in selection.get("stays") or [])
    return {
        "title": f"The route: {towns}",
        "summary": f"Rovyn built this multi-city schedule from the group's starred picks — {towns}, "
                   f"staying at {stay_names or 'the group’s chosen stays'}. "
                   "Every starred visit and event has a slot in its own city.",
        "days": days,
        "tips": [
            "This plan was assembled in sample mode — add ANTHROPIC_API_KEY to Rovyn/.env for a smarter weave.",
            "Book intercity trains and flights early — group seats sell out first.",
            "Pack light: you'll carry everything between cities.",
        ],
    }

# ---------------------------------------------------------------------------
# Final plan revision (free-text change requests)
# ---------------------------------------------------------------------------

class RevisionNotPossible(Exception):
    """The requested change can't be applied; str(exc) explains why, for the traveler."""


def _detach_heavy(plan: dict) -> tuple[dict, list]:
    """Deep-copy the plan and pull out image/link payloads (URLs are token-heavy
    and irrelevant to the revision). Returns (slim_plan, heavy_entries)."""
    plan = json.loads(json.dumps(plan))
    heavy = []

    def take(obj: dict, cat: str, ident):
        # URLs must never round-trip through the model: token-heavy AND the
        # model must not be able to rewrite where travelers click.
        for key in ("image", "links", "link", "booking_links", "official_website"):
            if isinstance(obj, dict) and key in obj:
                heavy.append((cat, ident, key, obj.pop(key)))

    take(plan, "plan", None)
    if isinstance(plan.get("stay"), dict):
        take(plan["stay"], "stay", None)
    for stay in plan.get("stays") or []:
        if isinstance(stay, dict):
            take(stay, "stays", stay.get("city") or stay.get("name"))
    for leg in plan.get("transport") or []:
        if isinstance(leg, dict):
            take(leg, "transport", leg.get("leg"))
    for cat in ("visits", "events"):
        for item in plan.get(cat, []):
            take(item, cat, item.get("id") or item.get("name"))
    for city, flight in (plan.get("flights") or {}).items():
        if isinstance(flight, dict):
            take(flight, "flights", city)
    return plan, heavy


def _reattach_heavy(plan: dict, heavy: list) -> dict:
    for cat, ident, key, val in heavy:
        if cat == "plan":
            target = plan
        elif cat == "stay":
            target = plan.get("stay")
        elif cat == "stays":
            target = next(
                (s for s in plan.get("stays") or []
                 if isinstance(s, dict) and (s.get("city") or s.get("name")) == ident),
                None,
            )
        elif cat == "transport":
            target = next(
                (t for t in plan.get("transport") or []
                 if isinstance(t, dict) and t.get("leg") == ident),
                None,
            )
        elif cat == "flights":
            target = (plan.get("flights") or {}).get(ident)
        else:
            target = next(
                (it for it in plan.get(cat, [])
                 if (it.get("id") or it.get("name")) == ident or it.get("name") == ident),
                None,
            )
        if isinstance(target, dict) and key not in target:
            target[key] = val
    return plan


def revise_final_plan(plan: dict, request: str, requester: str) -> dict:
    if not ai_available():
        raise RevisionNotPossible(
            "AI plan revisions need ANTHROPIC_API_KEY in Rovyn/.env. "
            "You can still reorder days, or the group can re-run the final plan."
        )

    slim, heavy = _detach_heavy(plan)

    prompt = f"""You maintain a finalized group trip plan. A traveler named {requester} requested this change:

"{request}"

Current plan JSON:
{json.dumps(slim, indent=1)}

If the change CANNOT reasonably be applied — it would move or remove fixed moments (check-in, check-out, arrival, departure), it contradicts the trip's dates or destination, it isn't about this trip, or it's unintelligible — do NOT return a plan. Respond with ONLY:
{{"impossible": "one friendly sentence explaining to the traveler why it doesn't fit and what they could try instead"}}

Otherwise apply the change faithfully but minimally — keep everything else identical (same schema, same keys). New visits/events get realistic cost_per_person and a spot in the day-by-day schedule. Append one short entry to "revision_notes" describing what changed and for whom (e.g. "Added the Botanical Garden on Day 3 — requested by Ana").

Respond with ONLY the complete updated plan as valid JSON (or the impossible object)."""

    revised = _ask(prompt, max_tokens=16000)
    if isinstance(revised, dict) and revised.get("impossible"):
        raise RevisionNotPossible(revised["impossible"])
    if not isinstance(revised, dict) or "days" not in revised.get("itinerary", {}):
        raise RuntimeError("The revision came back malformed — please try rephrasing the request.")

    revised = _reattach_heavy(revised, heavy)
    # New visits/events need ids (checklist keys depend on them) and photos
    town = (revised.get("destination") or "").split(",")[0].strip()
    for cat, fallback in (("visits", town), ("events", f"{town} evening")):
        existing_ids = {i.get("id") for i in revised.get(cat, []) if i.get("id")}
        counter = 0
        for item in revised.get(cat, []):
            if not item.get("id"):
                counter += 1
                new_id = f"{cat}-rev{counter}"
                while new_id in existing_ids:
                    counter += 1
                    new_id = f"{cat}-rev{counter}"
                item["id"] = new_id
                existing_ids.add(new_id)
            if not item.get("image"):
                item["image"] = first_image(f"{item.get('name', '')} {town}", fallback)

    # Never trust the model with money math or monotonic bookkeeping
    revised = recompute_plan_costs(revised)
    notes = plan.get("revision_notes", [])
    new_notes = revised.get("revision_notes", [])
    if len(new_notes) <= len(notes):
        new_notes = notes + [f"{requester}: {request}"]
    revised["revision_notes"] = new_notes
    return revised


# ---------------------------------------------------------------------------
# Copied-trip adaptation (social feed: "make this trip mine")
# ---------------------------------------------------------------------------

def _legacy_day_to_text(day: dict) -> dict:
    """Legacy itinerary days hold {activity, description} objects; final-plan days hold strings."""
    def txt(period):
        slot = day.get(period)
        if isinstance(slot, dict):
            return slot.get("activity", "") or ""
        return slot or ""
    return {
        "day": day.get("day"),
        "date": day.get("date"),
        "theme": day.get("theme", ""),
        "morning": txt("morning"),
        "afternoon": txt("afternoon"),
        "evening": txt("evening"),
    }


def normalize_snapshot(snapshot: dict) -> dict:
    """Coerce either snapshot kind (final_plan or legacy itinerary) into the final-plan shape."""
    kind = snapshot.get("kind", "final_plan")
    plan = snapshot.get("plan", {})
    if kind == "final_plan":
        return plan
    # Legacy single-shot itinerary → minimal final-plan shape
    days = [_legacy_day_to_text(d) for d in plan.get("days", [])]
    return {
        "destination": plan.get("destination", ""),
        "country": plan.get("country", ""),
        "dates": plan.get("dates", {}),
        "duration_days": plan.get("duration_days", len(days)),
        "summary": f"A trip to {plan.get('destination', '')}.",
        "decisions": [],
        "stay": plan.get("hotel", {}),
        "flights": {},
        "itinerary": {"title": f"{plan.get('destination', 'Trip')} itinerary", "days": days},
        "visits": [],
        "events": [],
        "cost_per_person": plan.get("cost_breakdown", {}),
        "flight_cost_by_city": {},
        "tips": plan.get("tips", []),
        "revision_notes": [],
    }


def _redate_days(days: list[dict], start: str) -> list[dict]:
    """Deterministic re-dating — never trust the model with calendar math."""
    try:
        first = dt.strptime(start, "%Y-%m-%d")
    except (ValueError, TypeError):
        return days
    out = []
    for i, day in enumerate(days):
        day = dict(day)
        day["day"] = i + 1
        day["date"] = (first + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append(day)
    return out


def adapt_copied_plan(snapshot: dict, start_date: str, end_date: str,
                      original_author: str = "a traveler") -> dict:
    """Check whether a copied itinerary works on the copier's dates and adapt it.

    Returns {"report": {...}, "plan": {...}} where plan is final_plan-shaped and
    report explains day-count changes, per-event availability, and warnings.
    """
    original = normalize_snapshot(snapshot)
    new_duration = _duration_days({"start": start_date, "end": end_date})
    original_days = original.get("itinerary", {}).get("days", [])
    original_duration = original.get("duration_days") or len(original_days) or new_duration

    if ai_available():
        result = _ai_adapt_plan(original, start_date, end_date, new_duration, original_duration)
    else:
        result = _sample_adapt_plan(original, start_date, end_date, new_duration, original_duration)

    plan = result.get("plan", {})
    plan["dates"] = {"start": start_date, "end": end_date}
    plan["duration_days"] = new_duration
    plan.setdefault("itinerary", {})["days"] = _redate_days(
        (plan.get("itinerary") or {}).get("days", []) or original_days, start_date
    )[:new_duration]
    notes = plan.setdefault("revision_notes", [])
    notes.append(
        f"Copied from {original_author}'s trip on the Rovyn feed and adapted to "
        f"{start_date} – {end_date} ({new_duration} days; the original ran {original_duration})."
    )

    report = result.get("report", {})
    report.setdefault("feasible", True)
    report["day_count"] = {"original": original_duration, "new": new_duration}
    return {"report": report, "plan": plan}


def _ai_adapt_plan(original: dict, start_date: str, end_date: str,
                   new_duration: int, original_duration: int) -> dict:
    prompt = f"""A traveler found this trip plan on a travel social feed and wants to copy it for their own dates. Your job is to CHECK feasibility and ADAPT the plan.

Original plan (ran {original.get('dates', {}).get('start')} to {original.get('dates', {}).get('end')}, {original_duration} days):
{json.dumps(original, indent=1)}

The copier's dates: {start_date} to {end_date} ({new_duration} days).

Do all of the following:
1. EVENTS — the original may include time-specific events (festivals, concerts, seasonal parties). For EACH item in "events", judge whether it plausibly runs during the new dates: "likely_available" (recurring/daily), "seasonal" (runs only part of the year — say which), "date_specific" (tied to the original dates, almost certainly NOT on), or "unknown". Drop date_specific events from the adapted plan and, where natural, suggest a comparable alternative in the day schedule.
2. DURATION — the new trip is {new_duration} days vs the original {original_duration}. If shorter, cut the least essential days/activities while keeping the plan's personality and must-sees. If longer, add well-paced extra days in the same spirit (don't pad with filler repetition).
3. SEASON — if the new dates fall in a clearly different season, adjust weather-dependent activities (beach days, snow, open-air venues) and note it in warnings.
4. STAY & COSTS — keep the same stay; recompute cost_per_person fields (including shared_total) for the new duration (meals ≈ $50/day; keep other unit costs).
5. Keep the exact same JSON schema for the plan as the original (same keys). Set "summary" to 2-3 warm sentences about the adapted trip.

Respond with ONLY valid JSON:
{{
  "report": {{
    "feasible": true,
    "duration_note": "one sentence on how the day count was handled",
    "events": [{{"name": "string", "status": "likely_available|seasonal|date_specific|unknown", "note": "one short sentence"}}],
    "changes": ["short bullet describing each meaningful change you made"],
    "warnings": ["anything the copier should verify or watch out for"]
  }},
  "plan": {{ ...the complete adapted plan, same schema as the original... }}
}}"""
    result = _ask(prompt, max_tokens=16000)
    if not isinstance(result, dict) or "plan" not in result:
        raise RuntimeError("The adaptation came back malformed — please try again.")
    return result


def _sample_adapt_plan(original: dict, start_date: str, end_date: str,
                       new_duration: int, original_duration: int) -> dict:
    """Deterministic adaptation used when ANTHROPIC_API_KEY is not configured."""
    plan = json.loads(json.dumps(original))  # deep copy
    days = plan.get("itinerary", {}).get("days", [])

    changes = []
    if new_duration < len(days):
        days = days[:new_duration]
        changes.append(f"Trimmed the last {original_duration - new_duration} day(s) to fit your shorter trip.")
    elif new_duration > len(days) and days:
        while len(days) < new_duration:
            days.append({
                "theme": "Free day",
                "morning": "Slow morning — revisit a favorite spot",
                "afternoon": "Open afternoon at your own pace",
                "evening": "Dinner somewhere you spotted earlier in the trip",
            })
        changes.append(f"Added {new_duration - original_duration} open day(s) — the original trip was shorter.")
    plan.setdefault("itinerary", {})["days"] = days

    event_reports = []
    kept_events = []
    for e in plan.get("events", []):
        hint = (e.get("date_hint") or "").lower()
        recurring = any(w in hint for w in ("daily", "every", "most", "evenings", "weekend"))
        status = "likely_available" if recurring else "unknown"
        event_reports.append({
            "name": e.get("name", "Event"),
            "status": status,
            "note": "Appears to run regularly." if recurring
                    else "Tied to specific dates in the original trip — verify local listings for yours.",
        })
        kept_events.append(e)
    plan["events"] = kept_events

    costs = plan.get("cost_per_person") or {}
    if "meals_estimate" in costs:
        costs["meals_estimate"] = 50 * new_duration
        costs["shared_total"] = sum(v for k, v in costs.items() if k != "shared_total" and isinstance(v, (int, float)))

    return {
        "report": {
            "feasible": True,
            "duration_note": changes[0] if changes else "Same length as the original — the schedule maps over directly.",
            "events": event_reports,
            "changes": changes or ["No structural changes needed."],
            "warnings": [
                "Adapted in sample mode — add ANTHROPIC_API_KEY to Rovyn/.env for a real availability check.",
                "Event dates and seasonal activities were not verified against your travel window.",
            ],
        },
        "plan": plan,
    }


# ---------------------------------------------------------------------------
# Legacy single-shot itinerary (kept for the old flow/endpoint)
# ---------------------------------------------------------------------------

def generate_itinerary(destination: str, responses: list[dict]) -> dict:
    prefs = _aggregate_preferences(responses)

    if ai_available():
        itinerary = _ai_itinerary(destination, prefs)
    else:
        itinerary = _sample_itinerary(destination, prefs)

    dates = itinerary.get("dates", {})
    check_in = dates.get("start", prefs["travel_dates"]["start"])
    check_out = dates.get("end", prefs["travel_dates"]["end"])
    adults = max(prefs["group_size"], 1)
    place = f"{destination}"

    itinerary["booking_links"] = {
        "stays": links.stay_links(place, check_in, check_out, adults),
        "activities": links.activity_links(place, check_in, check_out),
        "flights": {
            city: links.expedia_flights(city, destination, check_in, check_out, adults)
            for city in prefs["departure_cities"]
        },
    }
    return itinerary


def _ai_itinerary(destination: str, prefs: dict) -> dict:
    budget = prefs["budget_range"]
    dates = prefs["travel_dates"]
    duration_days = _duration_days(dates)
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"

    trip_style_note = {
        "multi-city": "Structure the itinerary as a multi-city route within the destination country.",
        "single-city": "Keep the group based in one city; day trips are fine.",
    }.get(prefs["trip_style"], "")

    prompt = f"""You are an expert travel planner building a detailed group itinerary for {destination}.

Group profile:
- Group size: {prefs['group_size']} people
- Total budget per person: ${budget['min']}–${budget['max']} USD (flights excluded)
- Dates: {dates['start']} to {dates['end']} ({duration_days} days)
- Interests: {vibes}
- Accommodation preference: {prefs['preferred_accommodation']}
- Special requirements: {requirements}
{f'- {trip_style_note}' if trip_style_note else ''}

Build a complete day-by-day itinerary with realistic activities, restaurants and costs. Recommend one specific real accommodation (a well-known hotel or a typical Airbnb listing style) matching the group's preference.

Respond with ONLY a valid JSON object (no markdown) with this exact structure:
{{
  "destination": "string",
  "country": "string",
  "duration_days": {duration_days},
  "dates": {{"start": "{dates['start']}", "end": "{dates['end']}"}},
  "hotel": {{
    "name": "string",
    "type": "string",
    "price_per_night": 0,
    "rating": 4.5,
    "amenities": ["..."],
    "total_hotel_cost_per_person": 0
  }},
  "days": [
    {{
      "day": 1,
      "date": "YYYY-MM-DD",
      "city": "City the group is in that day",
      "theme": "Arrival & First Impressions",
      "morning": {{"activity": "string", "description": "string", "cost_per_person": 0, "bookable": false}},
      "afternoon": {{"activity": "string", "description": "string", "cost_per_person": 0, "bookable": true}},
      "evening": {{"activity": "string", "description": "string", "cost_per_person": 0, "bookable": false}},
      "meals_budget": 50,
      "day_total_per_person": 0
    }}
  ],
  "total_cost_per_person": 0,
  "cost_breakdown": {{"accommodation": 0, "activities": 0, "meals_estimate": 0, "transport_estimate": 0}},
  "tips": ["Local tip 1", "Local tip 2", "Local tip 3"]
}}"""

    return _ask(prompt, max_tokens=8192)


def _sample_itinerary(destination: str, prefs: dict) -> dict:
    """Deterministic demo itinerary used when ANTHROPIC_API_KEY is not configured."""
    dates = prefs["travel_dates"]
    duration_days = min(_duration_days(dates), 7)
    try:
        start = dt.strptime(dates["start"], "%Y-%m-%d")
    except (ValueError, TypeError):
        start = dt.now()

    nightly = 140
    themes = ["Arrival & First Impressions", "Old Town & Landmarks", "Nature Escape",
              "Local Food Deep-Dive", "Beach or Hike Day", "Markets & Museums", "Farewell Highlights"]
    days = []
    for i in range(duration_days):
        date = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        days.append({
            "day": i + 1,
            "date": date,
            "city": destination,
            "theme": themes[i % len(themes)],
            "morning": {"activity": f"Guided walking tour of {destination}" if i == 1 else "Café breakfast & neighborhood stroll",
                        "description": "Ease into the day like a local.", "cost_per_person": 25 if i == 1 else 10, "bookable": i == 1},
            "afternoon": {"activity": "Nature excursion nearby" if i % 3 == 2 else "Museum or landmark visit",
                          "description": "Book ahead on GetYourGuide or Viator for the best slots.", "cost_per_person": 40, "bookable": True},
            "evening": {"activity": "Group dinner & nightlife" if i % 2 == 0 else "Sunset viewpoint & wine",
                        "description": "Reservations recommended for groups.", "cost_per_person": 35, "bookable": False},
            "meals_budget": 50,
            "day_total_per_person": 125,
        })

    accommodation_total = nightly * duration_days
    activities_total = sum(d["morning"]["cost_per_person"] + d["afternoon"]["cost_per_person"] + d["evening"]["cost_per_person"] for d in days)
    meals_total = 50 * duration_days

    return {
        "destination": destination,
        "country": "",
        "duration_days": duration_days,
        "dates": {"start": dates["start"], "end": dates["end"]},
        "hotel": {
            "name": f"Central stay in {destination}",
            "type": prefs["preferred_accommodation"],
            "price_per_night": nightly,
            "rating": 4.6,
            "amenities": ["Free WiFi", "Central location", "Group-friendly"],
            "total_hotel_cost_per_person": accommodation_total // max(prefs["group_size"], 1) if prefs["preferred_accommodation"].startswith("airbnb") else accommodation_total,
        },
        "days": days,
        "total_cost_per_person": accommodation_total + activities_total + meals_total,
        "cost_breakdown": {
            "accommodation": accommodation_total,
            "activities": activities_total,
            "meals_estimate": meals_total,
            "transport_estimate": 80,
        },
        "tips": [
            "This is a sample itinerary — set ANTHROPIC_API_KEY in Rovyn/.env for an AI-personalized plan.",
            "Book stays and key tours 4–6 weeks ahead for group availability.",
            "Use the Booking Hub links below to compare prices in one place.",
        ],
        "source": "sample",
    }
