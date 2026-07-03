import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as dt, timedelta

from backend.services import links
from backend.services.hotel_service import official_website, real_stay_options
from backend.services.image_service import first_image, image_for, nth_image

_client = None

# "itinerary" is no longer a starrable category — Rovyn builds the itinerary
# from the group's starred visits & events at their preferred pace.
CATEGORIES = ["flights", "stay", "visits", "events"]

PACES = ("very", "moderately", "slightly")  # how busy the trip should be


def recommended_picks(duration_days: int, pace: str) -> dict:
    """How many visits/events a traveler should aim to star, given their pace."""
    per_day = {"very": 1.5, "moderately": 1.0, "slightly": 0.6}.get(pace, 1.0)
    visits = max(2, round(duration_days * per_day))
    events = {"very": max(2, round(duration_days / 2)),
              "moderately": max(1, round(duration_days / 3)),
              "slightly": 1}.get(pace, 2)
    return {"visits": visits, "events": events}


def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic()
    return _client


def ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Preference aggregation
# ---------------------------------------------------------------------------

def _aggregate_preferences(responses: list[dict]) -> dict:
    all_vibes: list[str] = []
    all_accommodation: list[str] = []
    trip_styles: list[str] = []
    departure_cities: list[str] = []
    budgets_min, budgets_max = [], []
    dates_start, dates_end = [], []
    requirements: list[str] = []
    paces: list[str] = []

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

    return {
        "group_pace": group_pace,
        "group_size": len(responses),
        "budget_range": {"min": budget_min, "max": budget_max},
        "travel_dates": {"start": start, "end": end},
        "top_vibes": top_vibes,
        "preferred_accommodation": top_accommodation,
        "accommodation_votes": dict(accommodation_counts),
        "trip_style": trip_style,
        "departure_cities": sorted(departure_cities),
        "special_requirements": requirements,
    }


def _duration_days(dates: dict) -> int:
    try:
        start = dt.strptime(dates["start"], "%Y-%m-%d")
        end = dt.strptime(dates["end"], "%Y-%m-%d")
        return max((end - start).days, 1)
    except (ValueError, KeyError, TypeError):
        return 5


def _parse_json_reply(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Tolerate prose around the JSON payload; try the shape the reply starts
    # with first, so an object containing arrays parses as the object
    pairs = (("{", "}"), ("[", "]")) if text[:1] == "{" else (("[", "]"), ("{", "}"))
    for opener, closer in pairs:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(text)


def _ask_claude(prompt: str, max_tokens: int = 8192):
    """Streamed request with one connection-error retry.

    Long generations (final-plan weaves take 30-90s of thinking) leave a
    non-streaming HTTP connection idle, and idle connections get dropped
    ('server disconnected without a response'). Streaming keeps bytes moving.
    """
    from anthropic import APIConnectionError, APITimeoutError

    last_err = None
    # Attempt 1: adaptive thinking. Attempt 2: thinking off — covers both
    # transient connection drops AND the model spending the entire token
    # budget on thinking and returning no text at all.
    for thinking in ({"type": "adaptive"}, None):
        try:
            kwargs = {
                "model": "claude-opus-4-8",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if thinking:
                kwargs["thinking"] = thinking
            with _get_client().messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
            text = next((b.text for b in message.content if b.type == "text"), "")
            if not text.strip():
                last_err = RuntimeError("The model returned an empty completion (token budget spent on thinking).")
                continue
            return _parse_json_reply(text)
        except (APIConnectionError, APITimeoutError) as exc:
            last_err = exc
    raise last_err


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

def get_destination_recommendations(responses: list[dict], trip_name: str = "") -> list[dict]:
    prefs = _aggregate_preferences(responses)

    if ai_available():
        recommendations = _ai_recommendations(prefs, trip_name)
    else:
        recommendations = _sample_recommendations(prefs)

    for rec in recommendations:
        rec["image"] = first_image(
            f"{rec.get('name', '')} {rec.get('country', '')} travel",
            rec.get("name", ""),
        )
    return [_attach_links(rec, prefs) for rec in recommendations]


def _ai_recommendations(prefs: dict, trip_name: str = "") -> list[dict]:
    budget = prefs["budget_range"]
    dates = prefs["travel_dates"]
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"
    departures = ", ".join(prefs["departure_cities"]) if prefs["departure_cities"] else "unknown"

    trip_style_note = {
        "multi-city": "The group wants to explore MULTIPLE cities/regions within one country — favor destinations with easy internal travel and suggest a route.",
        "single-city": "The group prefers to stay based in ONE city with day trips at most.",
    }.get(prefs["trip_style"], "The group has no strong preference between one base city or a multi-city route.")

    name_note = ""
    if trip_name.strip():
        name_note = (
            f'\n- The group named this trip "{trip_name.strip()}". If the name references a specific '
            "place, region, country or season, treat that as a STRONG signal of intent — at least 2-3 "
            "of your recommendations should honor it (the named place itself, plus close alternatives "
            "in the same region), unless it clearly conflicts with the group's budget or dates."
        )

    prompt = f"""You are an expert travel planner. A group of {prefs['group_size']} people wants to travel together.

Group profile:
- Budget per person: ${budget['min']}–${budget['max']} USD (total trip, flights excluded)
- Travel window: {dates['start']} to {dates['end']}
- Departure cities of members: {departures}
- Desired vibes (most voted first): {vibes}
- {trip_style_note}
- Preferred accommodation: {prefs['preferred_accommodation']}
- Special requirements: {requirements}{name_note}

Recommend exactly 5 travel destinations that suit this entire group. Weigh nature/outdoors, nightlife/party, and cultural interests according to the vibes above, and factor in reasonable flight access from the departure cities.

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
    "best_activities": ["Activity 1", "Activity 2", "Activity 3"],
    "suggested_route": "For multi-city trips: City A (2 nights) -> City B (3 nights); otherwise the base city",
    "best_time_to_visit": "Month or season note"
  }}
]"""

    return _ask_claude(prompt, max_tokens=4096)


def _sample_recommendations(prefs: dict) -> list[dict]:
    """Deterministic demo data used when ANTHROPIC_API_KEY is not configured."""
    budget = prefs["budget_range"]
    lo = budget["min"] or 500
    hi = budget["max"] or 2000
    samples = [
        ("Lisbon", "Portugal", 94, "Sun-drenched hills, tiled streets and late-night fado.",
         ["Alfama old town", "Day trip to Sintra", "Miradouro sunsets", "Pastéis de Belém"],
         "Lisbon (3 nights) -> Lagos, Algarve (2 nights)"),
        ("Barcelona", "Spain", 90, "Beach mornings, Gaudí afternoons, nightlife until sunrise.",
         ["Sagrada Família", "Barceloneta beach", "El Born tapas crawl", "Montjuïc views"],
         "Barcelona (4 nights) -> Costa Brava (1 night)"),
        ("Mexico City", "Mexico", 87, "World-class food and culture at a friendly price point.",
         ["Chapultepec park", "Teotihuacán pyramids", "Roma Norte food scene", "Lucha libre night"],
         "Mexico City (3 nights) -> Oaxaca (2 nights)"),
        ("Split", "Croatia", 85, "Adriatic swims, island hops and Roman ruins in one trip.",
         ["Diocletian's Palace", "Hvar island hop", "Krka waterfalls", "Old-town bars"],
         "Split (3 nights) -> Hvar (2 nights)"),
        ("Chiang Mai", "Thailand", 82, "Temples, jungle treks and night markets on a backpacker budget.",
         ["Old-city temples", "Doi Inthanon trek", "Sunday night market", "Thai cooking class"],
         "Chiang Mai (3 nights) -> Pai (2 nights)"),
    ]
    recs = []
    for name, country, score, tagline, highlights, route in samples:
        recs.append({
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
        })
    return recs


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
    return options


def _attach_option_links(options: dict, prefs: dict, destination: str) -> dict:
    dates = options.get("dates", {})
    check_in = dates.get("start", "")
    check_out = dates.get("end", "")
    adults = max(prefs["group_size"], 1)
    for opt in options.get("stay", []):
        opt["links"] = links.stay_links(destination, check_in, check_out, adults)
    for opt in options.get("visits", []) + options.get("events", []):
        opt["links"] = links.activity_links(destination, check_in, check_out)
    for city, flights in (options.get("flights") or {}).items():
        url = links.expedia_flights(city, destination, check_in, check_out, adults)
        for opt in flights:
            opt["link"] = url
    return options


def generate_trip_options(destination: str, responses: list[dict], trip_name: str = "") -> dict:
    prefs = _aggregate_preferences(responses)
    dates = dict(prefs["travel_dates"])
    duration = _duration_days(dates)

    # The three slow, independent fetches run concurrently: the big options
    # prompt, the per-city flights prompt, and the live hotel search.
    with ThreadPoolExecutor(max_workers=3) as pool:
        base_future = pool.submit(
            _ai_trip_options if ai_available() else (lambda d, p, _t: _sample_trip_options(d, p)),
            destination, prefs, trip_name,
        )
        flights_future = pool.submit(_flight_options, destination, prefs)
        stays_future = pool.submit(
            real_stay_options, destination, dates.get("start", ""), dates.get("end", ""),
            prefs["group_size"], duration, "", prefs["accommodation_votes"],
        )
        options = base_future.result()
        live_stays = stays_future.result()
        options["flights"] = flights_future.result()

    options["destination"] = options.get("destination") or destination
    options.setdefault("dates", dates)

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

    def stay_image(opt, idx):
        # Don't query hotel NAMES: Pexels fuzzy-matches them to the same
        # generic destination photo. Deal each hotel a different photo (by
        # index) from the shared pool instead.
        return (nth_image(f"{opt.get('type', 'hotel')} {town}", idx)
                or nth_image(f"hotel {town}", idx)
                or nth_image(town, idx))

    jobs = [(options, "image", (f"{destination} travel", town))]
    fns = []
    for idx, opt in enumerate(options.get("stay", [])):
        if not opt.get("image"):  # real hotels carry their own photo
            fns.append((opt, "image", (lambda o=opt, i=idx: stay_image(o, i))))
    for opt in options.get("visits", []):
        jobs.append((opt, "image", (
            f"{opt.get('name', '')} {town}",
            f"{town} {opt.get('category', '')}".strip(),
            town,
        )))
    for opt in options.get("events", []):
        jobs.append((opt, "image", (f"{opt.get('name', '')}", f"{town} evening")))

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


def _flight_options(destination: str, prefs: dict) -> dict:
    cities = prefs["departure_cities"] or ["Your city"]
    dates = prefs["travel_dates"]
    raw = {}
    if ai_available():
        try:
            raw = _ai_flights(destination, cities, dates, prefs["group_size"])
        except Exception:
            raw = {}  # never let flight generation sink the whole board

    # Normalize: every departure city gets options under ITS exact name,
    # matching AI keys case-insensitively and filling gaps with samples.
    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    raw_by_norm = {norm(k): v for k, v in raw.items() if isinstance(v, list) and v}
    flights = {}
    for city in cities:
        flights[city] = raw_by_norm.get(norm(city)) or _sample_flights_for_city(city, destination, dates)
    return flights


def _ai_flights(destination: str, cities: list[str], dates: dict, group_size: int) -> dict:
    keys = ", ".join(f'"{c}"' for c in cities)
    prompt = f"""You are a flight-search expert. For EACH departure city below, produce exactly 3 realistic round-trip flight options to {destination} for these dates: {dates['start']} to {dates['end']}. Use plausible airlines, routes (with real airport codes), times and prices for each specific origin.

Departure cities: {', '.join(cities)}

The three options per city should cover different trade-offs: fastest, cheapest, best times.

Respond with ONLY valid JSON — an object whose keys are EXACTLY these strings: {keys}
{{
  "{cities[0]}": [
    {{"airline": "string", "route": "JFK → SPU (via VIE)", "depart": "{dates['start']} 09:40",
      "return": "{dates['end']} 17:10", "stops": 1, "duration": "11h 20m",
      "est_price_per_person": 640, "why": "one line on the trade-off"}}
  ]
}}"""
    result = _ask_claude(prompt, max_tokens=4096)
    return result if isinstance(result, dict) else {}


def _sample_flights_for_city(city: str, destination: str, dates: dict) -> list[dict]:
    return [
        {"airline": "Lufthansa", "route": f"{city} → {destination} (1 stop)",
         "depart": f"{dates['start']} 08:30", "return": f"{dates['end']} 18:45",
         "stops": 1, "duration": "9h 10m", "est_price_per_person": 620,
         "why": "Best balance of price and schedule"},
        {"airline": "Budget carrier combo", "route": f"{city} → {destination} (2 stops)",
         "depart": f"{dates['start']} 06:05", "return": f"{dates['end']} 22:30",
         "stops": 2, "duration": "13h 40m", "est_price_per_person": 380,
         "why": "Cheapest — early start, long day"},
        {"airline": "Turkish Airlines", "route": f"{city} → {destination} (1 stop)",
         "depart": f"{dates['start']} 11:20", "return": f"{dates['end']} 15:10",
         "stops": 1, "duration": "8h 05m", "est_price_per_person": 760,
         "why": "Most comfortable times, checked bag included"},
    ]


def _option_pool_sizes(duration: int) -> tuple[int, int]:
    """How many visit/event options to offer, scaled to the stay length so even
    a very-busy group has enough to choose from."""
    visits = min(12, max(6, round(duration * 1.5) + 2))
    events = min(6, max(4, duration // 2 + 2))
    return visits, events


def _ai_trip_options(destination: str, prefs: dict, trip_name: str) -> dict:
    budget = prefs["budget_range"]
    dates = prefs["travel_dates"]
    duration = _duration_days(dates)
    n_visits, n_events = _option_pool_sizes(duration)
    vibes = ", ".join(prefs["top_vibes"]) if prefs["top_vibes"] else "any"
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"
    acc_votes = ", ".join(f"{k}: {v} vote(s)" for k, v in prefs["accommodation_votes"].items()) or "no data"

    prompt = f"""You are an expert travel planner assembling a menu of concrete options for a group trip to {destination}. The group will STAR the options they like — Rovyn then builds the day-by-day itinerary from their starred picks — so every option must be distinct, real and independently doable (real hotel names, real sights).

Trip: "{trip_name or destination}"
Group profile:
- Group size: {prefs['group_size']}
- Budget per person: ${budget['min']}–${budget['max']} USD total (flights excluded)
- Dates: {dates['start']} to {dates['end']} ({duration} days)
- Vibes (most voted first): {vibes}
- Accommodation votes across the group: {acc_votes} — offer options spanning the styles people asked for
- Special requirements: {requirements}

Produce options in THREE categories:
1. "stay" — exactly 4 accommodation options covering different styles/price points that match the group's votes.
2. "visits" — exactly {n_visits} sights/attractions/day-trips worth visiting (each independently bookable or free). Cover different interests and durations so both packed and relaxed travelers find matches.
3. "events" — exactly {n_events} time-specific experiences during the dates (concerts, festivals, food tours, boat parties, classes…). If nothing specific is verifiable, propose typical seasonal events and mark date_hint accordingly.

Respond with ONLY valid JSON (no markdown):
{{
  "destination": "{destination}",
  "country": "string",
  "dates": {{"start": "{dates['start']}", "end": "{dates['end']}"}},
  "stay": [
    {{"name": "string", "type": "boutique hotel", "area": "neighborhood", "price_per_night": 180,
      "total_per_person": 450, "rating": 4.6, "amenities": ["...", "..."],
      "why": "one sentence on who this suits"}}
  ],
  "visits": [
    {{"name": "string", "description": "1-2 sentences", "duration": "2h", "cost_per_person": 25, "category": "culture"}}
  ],
  "events": [
    {{"name": "string", "description": "1-2 sentences", "date_hint": "evenings in July / July 18", "cost_per_person": 40}}
  ]
}}"""

    return _ask_claude(prompt, max_tokens=12000)


def _sample_trip_options(destination: str, prefs: dict) -> dict:
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


def select_winners(options: dict, tallies: dict, prefs: dict | None = None) -> tuple[dict, list[dict]]:
    """Deterministic majority-rule resolution of the group's stars.

    Returns (selection, decisions). Selection holds the chosen option objects;
    decisions hold the human-readable 'why' for each category.
    """
    prefs = prefs or {}
    decisions = []

    stay_counts = tallies.get("stay", {}).get("counts", {})
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
    decisions.append({
        "category": "flights",
        "choice": "One flight per departure city",
        "rationale": "Flights are personal — each city's travelers picked their own. Most-starred per city: "
                     + "; ".join(flight_notes) + ". Your check reflects your own city's flight.",
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
        items = [{"name": o.get("name"), "stars": counts.get(o.get("id"), 0)} for o in starred]
        decisions.append({"category": cat, "choice": f"{len(starred)} selected", "rationale": rationale, "items": items})
        if cat == "visits":
            visits = starred
        else:
            events = starred

    pace_label = {"very": "very busy", "moderately": "moderately busy", "slightly": "slightly busy"}.get(group_pace, "moderately busy")
    decisions.append({
        "category": "itinerary",
        "choice": "Built from your picks",
        "rationale": f"Rovyn assembled the day-by-day schedule from the group's starred visits and events at a {pace_label} pace "
                     f"(the group's most common answer). Every slot can still be edited before approval.",
    })

    selection = {
        "stay": stay,
        "flights": chosen_flights,
        "visits": visits,
        "events": events,
    }
    return selection, decisions


def compute_costs(selection: dict, prefs: dict, duration: int) -> tuple[dict, dict]:
    """Deterministic per-person cost math shared by the plan and the check."""
    stay_cost = round(selection.get("stay", {}).get("total_per_person") or 0)
    visits_cost = round(sum(v.get("cost_per_person") or 0 for v in selection.get("visits", [])))
    events_cost = round(sum(e.get("cost_per_person") or 0 for e in selection.get("events", [])))
    meals = 50 * duration
    transport = 80
    shared_total = stay_cost + visits_cost + events_cost + meals + transport
    cost_per_person = {
        "stay": stay_cost,
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
        "stay": plan.get("stay", {}),
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

    # Accuracy: link the chosen stay's official website when discoverable
    if selection.get("stay", {}).get("name") and not selection["stay"].get("official_website"):
        selection["stay"]["official_website"] = official_website(selection["stay"]["name"], destination)

    if ai_available():
        try:
            woven = _ai_weave_plan(destination, prefs, selection, dates, duration)
            if isinstance(woven, list):  # model replied with a bare days array
                woven = {"days": woven}
            if not isinstance(woven, dict) or not woven.get("days"):
                raise ValueError("malformed weave response")
        except Exception:
            # Finalizing must never fail outright: fall back to the
            # deterministic weave — same picks, simpler day arrangement.
            woven = _sample_weave_plan(destination, prefs, selection, dates, duration)
            woven["tips"] = [
                "The AI scheduler was unreachable, so Rovyn arranged your picks automatically — "
                "drag anything you'd like to rearrange, or use “Request a change”.",
            ] + [t for t in woven.get("tips", []) if "sample mode" not in t]
    else:
        woven = _sample_weave_plan(destination, prefs, selection, dates, duration)

    return {
        "destination": destination,
        "country": options.get("country", ""),
        "image": options.get("image"),
        "dates": dates,
        "duration_days": duration,
        "summary": woven.get("summary", f"The group's plan for {destination}."),
        "decisions": decisions,
        "stay": selection["stay"],
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


def _pace_note(pace: str) -> str:
    return {
        "very": "The group wants to be VERY busy: fill mornings, afternoons and most evenings; free time is the exception.",
        "moderately": "The group wants a MODERATE pace: one or two anchors per day, keep some slots easy or free.",
        "slightly": "The group wants a RELAXED trip: at most one anchor per day, plenty of free time, slow mornings.",
    }.get(pace, "Keep a moderate pace.")


def _ai_weave_plan(destination: str, prefs: dict, selection: dict, dates: dict, duration: int) -> dict:
    visits = selection["visits"]
    events = selection["events"]
    requirements = "; ".join(prefs["special_requirements"]) if prefs["special_requirements"] else "none"

    prompt = f"""You are building the day-by-day itinerary for a group trip to {destination}, {dates.get('start')} to {dates.get('end')} ({duration} days). The group starred these picks; they are LOCKED — every single one must appear exactly once. Fill remaining slots with light, fitting suggestions (breakfast spots, strolls, beach time) or "Free time".

Pace: {_pace_note(prefs.get('group_pace', 'moderately'))}

Must-include visits:
{json.dumps([{k: v.get(k) for k in ('name', 'duration', 'cost_per_person')} for v in visits], indent=1)}

Must-include events (respect their date_hint when placing them; evening-flavored events go in evening slots):
{json.dumps([{k: e.get(k) for k in ('name', 'date_hint', 'cost_per_person')} for e in events], indent=1)}

Special requirements: {requirements}

Rules: day 1 is arrival — its morning (or afternoon, if flights land late) must be "Arrival & hotel check-in" and the day kept light. The last day is departure — its morning must be "Hotel check-out & departure" with nothing scheduled after. These fixed moments anchor the schedule; travelers can rearrange everything else later. Don't overpack any day. Give the itinerary a short evocative title. Also write a warm 2-3 sentence summary mentioning how the group's votes shaped the trip, and 3 practical local tips.

Respond with ONLY valid JSON:
{{
  "title": "string",
  "summary": "string",
  "days": [{{"day": 1, "date": "YYYY-MM-DD", "theme": "string", "morning": "string", "afternoon": "string", "evening": "string"}}],
  "tips": ["tip 1", "tip 2", "tip 3"]
}}"""

    return _ask_claude(prompt, max_tokens=12000)


def _sample_weave_plan(destination: str, prefs: dict, selection: dict, dates: dict, duration: int) -> dict:
    try:
        start = dt.strptime(dates.get("start", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        start = dt.now()

    town = destination.split(",")[0].strip()
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

    revised = _ask_claude(prompt, max_tokens=16000)
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
    result = _ask_claude(prompt, max_tokens=16000)
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

    return _ask_claude(prompt, max_tokens=8192)


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
