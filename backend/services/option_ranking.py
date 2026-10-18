"""Deterministic ranking, reasons and lanes for the planning board's stay and
flight menus — the "picked for you" layer of docs/OPTIONS-BREADTH-SPEC.md.

Everything here is a pure function of provider data and the group's
questionnaire: no I/O, no model calls, no clocks. Every reason is a sentence
assembled from numbers a provider returned (a price, a guest score, a
distance) and facts the group gave (votes, budget, vibes) — never a number or
a judgement a model made up. That is what lets a card say "picked for you"
and mean it, and what makes the whole layer unit-testable with fixtures.

Two layers, for stays and for flights alike:

  score_*        one candidate -> per-dimension scores, a match score, reasons
  pick_*_lanes   many scored candidates -> the diverse subset the board shows,
                 each stamped with the lane it won (Best match, Best value…)

The suppliers (hotel_service, flight_service, duffel_service) build plain
"record"/"facts" dicts from their own payloads and hand them here; the AI and
sample menus go through the same functions, so lanes and reasons exist in
every mode and a sample board reads like a live one. Dimensions a supplier
cannot inform (no guest score, no coordinates, no facilities) are left OUT
of the score rather than guessed at — a hotel is never marked down for data
we simply don't have.
"""
import math
import statistics
from collections import Counter

# ---------------------------------------------------------------------------
# Lodging budget band
# ---------------------------------------------------------------------------

MEALS_PER_DAY = 50        # the check's own meal estimate (ai_service.compute_costs)
LOCAL_TRANSPORT_FLAT = 80  # the check's own local-transport estimate
BAND_LOW_SHARE = 0.45     # a stay's share of what is left after meals/transport…
BAND_HIGH_SHARE = 0.65    # …leaving the rest for activities and events
SPLURGE_MIN_PER_NIGHT = 25  # below this ceiling a budget is too small to call anything a splurge


def lodging_band(members: list[tuple], nights: int) -> dict | None:
    """The per-person stay budget implied by the questionnaire, as a band.

    `members` holds one (budget_min, budget_max) pair per traveler; members
    who left the ceiling blank (0/None) are ignored. The questionnaire budget
    covers the whole trip, flights excluded; the check reserves $50/day for
    meals and $80 for local transport, so the stay can reasonably take 45–65 %
    of what remains. None when nobody gave a ceiling, the trip has no nights,
    or the ceiling is too small to mean anything — the budget dimension then
    stays out of the score and no card talks about budget."""
    nights = int(nights or 0)
    pairs = []
    for pair in members or []:
        try:
            lo, hi = float(pair[0] or 0), float(pair[1] or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if hi > 0:
            pairs.append((lo, hi))
    if nights <= 0 or not pairs:
        return None
    bmin = max(lo for lo, _ in pairs)
    bmax = min(hi for _, hi in pairs)
    if bmin > bmax:  # ranges don't overlap: fall back to the full span
        bmin, bmax = min(lo for lo, _ in pairs), max(hi for _, hi in pairs)
    low = max(0.0, bmin - MEALS_PER_DAY * nights - LOCAL_TRANSPORT_FLAT) * BAND_LOW_SHARE
    high = max(0.0, bmax - MEALS_PER_DAY * nights - LOCAL_TRANSPORT_FLAT) * BAND_HIGH_SHARE
    if high <= 0 or high / nights < SPLURGE_MIN_PER_NIGHT:
        return None
    if low > high:
        low = high * 0.6
    return {"low": int(round(low)), "high": int(round(high)), "nights": nights}


def band_for_nights(band: dict | None, nights: int) -> dict | None:
    """A city's share of the whole-trip band, proportional to its nights."""
    if not band or not nights:
        return None
    total = max(int(band.get("nights") or 1), 1)
    share = min(int(nights), total) / total
    return {"low": int(round(band["low"] * share)), "high": int(round(band["high"] * share)),
            "nights": int(nights)}


# ---------------------------------------------------------------------------
# Accommodation style — questionnaire vote vs property type
# ---------------------------------------------------------------------------

# Rovyn questionnaire accommodation vote -> (core LiteAPI hotelTypeIds, partial)
VOTE_TYPE_IDS = {
    "hostel": ({203, 225}, {208, 216, 218, 222}),
    "boutique hotel": ({208, 216, 218, 227}, {204}),
    "4-star hotel": ({204, 219, 231}, {206}),
    "luxury resort": ({206}, {204}),
    "airbnb/vacation rental": (
        {201, 207, 213, 219, 220, 222, 223, 228, 229, 230, 232}, {208, 216}),
}

# The same votes matched by words in a type label — AI, sample and Duffel
# stays carry labels ("boutique hotel", "villa"), not LiteAPI type ids.
VOTE_LABEL_WORDS = {
    "hostel": (("hostel", "capsule"),
               ("guest house", "guesthouse", "bed & breakfast", "b&b", "homestay", "inn")),
    "boutique hotel": (("boutique", "bed & breakfast", "b&b", "guest house", "guesthouse", "inn", "riad"),
                       ("hotel",)),
    "4-star hotel": (("4-star", "four-star", "aparthotel", "economy hotel"),
                     ("resort", "hotel")),
    "luxury resort": (("resort", "luxury", "5-star", "five-star"),
                      ("hotel",)),
    "airbnb/vacation rental": (
        ("apartment", "villa", "rental", "airbnb", "holiday home", "cottage", "condo",
         "chalet", "residence", "aparthotel", "country house", "farm", "gite", "homestay",
         "loft", "home"),
        ("guest house", "guesthouse", "bed & breakfast", "b&b")),
}

VOTE_LABELS = {  # how a vote reads in a sentence
    "hostel": "hostel",
    "boutique hotel": "boutique hotel",
    "4-star hotel": "four-star hotel",
    "luxury resort": "luxury resort",
    "airbnb/vacation rental": "apartment or villa",
}

# Property types that put a whole group under one roof
HOME_TYPE_IDS = {201, 207, 213, 219, 220, 223, 228, 229, 230, 232}
_HOME_WORDS = ("apartment", "villa", "holiday home", "cottage", "condo", "chalet",
               "residence", "aparthotel", "country house", "loft", "house", "rental")


def _match_vote(vote: str, type_id, label: str, stars) -> float:
    core_ids, partial_ids = VOTE_TYPE_IDS.get(vote, (set(), set()))
    lab = (label or "").casefold()
    if type_id is not None:
        match = 1.0 if type_id in core_ids else (0.5 if type_id in partial_ids else 0.0)
        plain_hotel = type_id == 204
    else:
        core_words, partial_words = VOTE_LABEL_WORDS.get(vote, ((), ()))
        if any(w in lab for w in core_words):
            match = 1.0
        elif any(w in lab for w in partial_words):
            match = 0.5
        else:
            match = 0.0
        plain_hotel = "hotel" in lab and "resort" not in lab and match < 1.0
    try:
        stars = float(stars or 0)
    except (TypeError, ValueError):
        stars = 0.0
    if match and vote == "4-star hotel" and stars and not (3.5 <= stars <= 4.5):
        match *= 0.6
    if match and vote == "luxury resort" and plain_hotel:
        match = 0.5 if stars >= 5 else 0.15
    return match


def style_affinity(votes: dict | None, type_id=None, label: str = "", stars=None) -> dict:
    """How well one property fits the group's accommodation votes.

    Returns the vote-share-weighted score (0..1) plus the single best-fitting
    vote — the one a reason sentence names — and whether that fit is a core
    match (a villa for an "apartment or villa" vote) or merely close."""
    votes = {k: v for k, v in (votes or {}).items() if v}
    total = sum(votes.values())
    if not total:
        return {"score": 0.0, "vote": None, "core": False, "votes": 0, "total": 0}
    score, best = 0.0, None
    for vote, count in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0])):
        m = _match_vote(vote, type_id, label, stars)
        score += (count / total) * m
        if m and (best is None or (m, count) > (best[1], best[2])):
            best = (vote, m, count)
    return {
        "score": round(score, 4),
        "vote": best[0] if best else None,
        "core": bool(best and best[1] >= 1.0),
        "votes": best[2] if best else 0,
        "total": total,
    }


def is_home_type(type_id=None, label: str = "") -> bool:
    if type_id is not None:
        return type_id in HOME_TYPE_IDS
    lab = (label or "").casefold()
    return any(w in lab for w in _HOME_WORDS)


# ---------------------------------------------------------------------------
# Vibes — facilities and location that match what the group asked for
# ---------------------------------------------------------------------------

_VIBE_WORDS = {
    "relaxation": ("spa", "pool", "wellness", "sauna", "hot tub", "jacuzzi", "massage", "hammam"),
    "beach": ("beachfront", "beach", "sea view", "ocean view", "waterfront", "seafront"),
    "nature": ("garden", "hiking", "mountain view", "lake", "forest", "countryside", "farm", "terrace"),
    "adventure": ("bike", "surf", "ski", "kayak", "diving", "hiking", "climbing"),
    "food": ("restaurant", "kitchen", "breakfast", "wine", "bar"),
    "nightlife": ("rooftop", "nightclub", "lounge", "bar"),
    "culture": ("historic", "palace", "heritage", "old town", "museum"),
    "shopping": ("shopping", "mall", "boutique"),
}
_CENTRAL_VIBES = ("nightlife", "food", "culture", "shopping")
_FAR_OK_VIBES = ("beach", "nature", "relaxation")
_VIBE_PHRASE = {
    "relaxation": "matches the group's relaxation vibe",
    "beach": "matches the group's beach vibe",
    "nature": "fits the nature vibe",
    "adventure": "fits the adventure vibe",
    "food": "for the food lovers",
    "nightlife": "close to the nightlife you voted for",
    "culture": "steps from the culture you voted for",
    "shopping": "right by the shopping",
}


def _vibe(record: dict, top_vibes: list[str]) -> tuple[float | None, dict | None]:
    """(score, reason) — None score when there is nothing to judge from.
    Only real property content counts: facilities and the description. A
    hotel NAME containing "Spa" is marketing, not a facility."""
    vibes = [v for v in (top_vibes or [])[:2] if v]
    facilities = [str(f) for f in (record.get("facilities") or []) if f]
    description = str(record.get("description") or "")
    distance = record.get("distance_km")
    if not vibes or not (facilities or description):
        return None, None
    haystack = " ".join(facilities + [description]).casefold()
    for rank, vibe in enumerate(vibes):
        strength = 1.0 if rank == 0 else 0.8
        hits = [w for w in _VIBE_WORDS.get(vibe, ()) if w in haystack]
        if hits:
            shown = " & ".join(h.capitalize() for h in dict.fromkeys(hits[:2]))
            return strength, {"kind": "vibe", "text": f"{shown} — {_VIBE_PHRASE[vibe]}",
                              "short": shown[:18]}
        if vibe in _CENTRAL_VIBES and distance is not None and distance <= 1.5:
            return strength, {"kind": "vibe", "text": f"Central — {_VIBE_PHRASE[vibe]}",
                              "short": "Central"}
    return 0.5, None


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def distance_km(lat1, lng1, lat2, lng2) -> float | None:
    """Great-circle distance; None when either point is missing."""
    try:
        la1, lo1, la2, lo2 = (math.radians(float(v)) for v in (lat1, lng1, lat2, lng2))
    except (TypeError, ValueError):
        return None
    d = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return round(2 * 6371.0 * math.asin(math.sqrt(min(1.0, d))), 3)


# ---------------------------------------------------------------------------
# Stays — one record -> score + reasons
# ---------------------------------------------------------------------------

STAY_WEIGHTS = {"style": 0.35, "budget": 0.25, "rating": 0.20, "location": 0.10, "vibe": 0.10}
MAX_REASONS = 4
MIN_KNOWN_DIMENSIONS = 3   # fewer and a match score would be mostly guesswork
ESTIMATED_SUPPLIERS = ("ai", "sample")


def _money(n) -> str:
    return f"${int(round(float(n))):,}"


def _budget(per_person_total, nights: int, band: dict | None) -> tuple[float | None, dict | None]:
    """Compared on the number the card already shows: the per-person total
    for this stay's nights, against the band for the same nights."""
    if per_person_total is None or not band:
        return None, None
    low, high = int(band.get("low") or 0), int(band.get("high") or 0)
    if high <= 0:
        return None, None
    total = float(per_person_total)
    nights_txt = f"{int(nights)} night{'s' if int(nights) != 1 else ''}"
    shown = _money(total)
    if total < low:
        return 0.85, {"kind": "budget", "short": "Under budget",
                      "text": f"{shown} per person for {nights_txt} — under your {_money(low)}–{_money(high)} stay budget"}
    if total <= high:
        return 1.0, {"kind": "budget", "short": "In budget",
                     "text": f"{shown} per person for {nights_txt} — inside your {_money(low)}–{_money(high)} stay budget"}
    ratio = total / high
    score = max(0.0, 1.0 - (ratio - 1.0) / 0.6)
    over = _money(total - high)
    if ratio <= 1.3:
        return score, {"kind": "budget", "short": "Stretch",
                       "text": f"{shown} per person for {nights_txt} — {over} over your {_money(high)} stay ceiling"}
    return score, {"kind": "budget", "short": "Splurge",
                   "text": f"{shown} per person for {nights_txt} — {over} per person over the group's ceiling"}


def _rating(rating10, count) -> tuple[float | None, dict | None]:
    if rating10 is None:
        return None, None
    r = float(rating10)
    n = int(count or 0)
    confidence = min(1.0, math.log10(n + 1) / 3) if n else 0.6
    score = max(0.0, min(1.0, r / 10)) * confidence
    shown = f"{r:.1f}/10"
    if r >= 9.0:
        text = f"Top-rated: {shown}" + (f" from {n:,} guest reviews" if n >= 20 else "")
        return score, {"kind": "rating", "text": text, "short": shown}
    if r >= 8.0 and n >= 20:
        return score, {"kind": "rating", "text": f"{shown} from {n:,} guest reviews", "short": shown}
    return score, None


def _location(distance, top_vibes: list[str]) -> tuple[float | None, dict | None]:
    if distance is None:
        return None, None
    far_ok = any(v in _FAR_OK_VIBES for v in (top_vibes or [])[:2])
    if distance <= 1.5:
        score = 1.0
    elif distance <= 4:
        score = 0.6
    else:
        score = 0.6 if far_ok else 0.3
    if distance < 0.25:
        return score, {"kind": "location", "text": "Right in the center", "short": "In the center"}
    if distance <= 2:
        minutes = max(1, round(distance * 1000 / 80))
        return score, {"kind": "location", "text": f"{minutes} min walk from the center",
                       "short": f"{minutes} min walk"}
    if distance <= 4:
        return score, {"kind": "location", "text": f"{distance:.1f} km from the center",
                       "short": f"{distance:.1f} km out"}
    return score, None


def _estimated(reason: dict | None, supplier: str) -> dict | None:
    """AI and sample menus carry invented numbers — say so on every reason
    that prints one."""
    if not reason or supplier not in ESTIMATED_SUPPLIERS:
        return reason
    if any(ch.isdigit() for ch in reason["text"]):
        return dict(reason, text=f"est. {reason['text']}")
    return reason


def score_stay(record: dict, prefs: dict | None, supplier: str = "liteapi") -> dict:
    """Score one stay record against the group.

    record keys (all optional except name): name, type_id (LiteAPI hotelTypeId),
    type_label, stars, rating10 (guest score /10), review_count, distance_km,
    per_person_total, nights, facilities (list[str]), description, group_size.
    prefs keys: accommodation_votes, lodging_band (already scaled to this
    stay's nights), top_vibes, group_size.

    Returns match_score (None when fewer than three dimensions carry real
    data, or for AI/sample menus), rank_score (always, for ordering), reasons,
    scores per dimension, dimensions_known, style_vote, style_core."""
    prefs = prefs or {}
    supplier = supplier or "liteapi"
    votes = prefs.get("accommodation_votes") or {}
    top_vibes = prefs.get("top_vibes") or []
    group = int(prefs.get("group_size") or record.get("group_size") or 1)
    nights = int(record.get("nights") or (prefs.get("lodging_band") or {}).get("nights") or 1)

    style = style_affinity(votes, record.get("type_id"), record.get("type_label") or "",
                           record.get("stars"))
    style_s = style["score"] if style["total"] else None
    style_reason = None
    if style["vote"]:
        label = (record.get("type_label") or VOTE_LABELS[style["vote"]]).strip()
        shown = f"{label[:1].upper()}{label[1:]}"
        if style["core"]:
            if style["total"] <= 1:
                text = f"{shown} — the style you asked for"
            else:
                text = f"{shown} — the style {style['votes']} of {style['total']} of you voted for"
        else:
            text = f"Close to the {VOTE_LABELS[style['vote']]} style you asked for"
        style_reason = {"kind": "style", "text": text, "short": shown[:18]}

    budget_s, budget_reason = _budget(record.get("per_person_total"), nights, prefs.get("lodging_band"))
    rating_s, rating_reason = _rating(record.get("rating10"), record.get("review_count"))
    location_s, location_reason = _location(record.get("distance_km"), top_vibes)
    vibe_s, vibe_reason = _vibe(record, top_vibes)

    scores = {"style": style_s, "budget": budget_s, "rating": rating_s,
              "location": location_s, "vibe": vibe_s}
    known = [k for k, v in scores.items() if v is not None]
    weight = sum(STAY_WEIGHTS[k] for k in known)
    rank = (sum(STAY_WEIGHTS[k] * scores[k] for k in known) / weight) if weight else 0.0
    contributions = {k: (STAY_WEIGHTS[k] * scores[k] if scores[k] is not None else -1) for k in STAY_WEIGHTS}

    # Ties (a 1.0 vibe hit and a 1.0 location both weigh 0.10) go to the vibe:
    # "Spa & Pool — matches your relaxation vibe" is the more personal line
    candidates = [(contributions["style"], style_reason), (contributions["budget"], budget_reason),
                  (contributions["rating"], rating_reason), (contributions["vibe"], vibe_reason),
                  (contributions["location"], location_reason)]
    ordered = [r for _, r in sorted(candidates, key=lambda c: -c[0]) if r]
    reasons = ordered[:MAX_REASONS]
    # A budget verdict is the one line a traveler always wants — it never gets
    # squeezed out by softer signals.
    if budget_reason and budget_reason not in reasons:
        reasons = reasons[:MAX_REASONS - 1] + [budget_reason]
    if group >= 4 and is_home_type(record.get("type_id"), record.get("type_label") or "") \
            and len(reasons) < MAX_REASONS:
        reasons.append({"kind": "group", "text": "Sleeps the whole group under one roof", "short": "Whole group"})
    reasons = [_estimated(r, supplier) for r in reasons]

    rank_score = int(round(100 * rank))
    emit = len(known) >= MIN_KNOWN_DIMENSIONS and supplier not in ESTIMATED_SUPPLIERS
    return {
        "match_score": rank_score if emit else None,
        "rank_score": rank_score,
        "reasons": reasons,
        "scores": scores,
        "dimensions_known": known,
        "style_vote": style["vote"],
        "style_core": style["core"],
    }


def why_line(lane: str | None, reasons: list[dict], extra: str = "") -> str:
    """The short `why` the card prints: the lane and the strongest reason."""
    text = reasons[0]["text"] if reasons else ""
    line = f"{lane} · {text}" if lane and text and len(f"{lane} · {text}") <= 88 else (text or lane or "")
    if extra:
        line = f"{line}. {extra}" if line else extra
    return line


# ---------------------------------------------------------------------------
# Stays — many scored records -> the diverse subset with lanes
# ---------------------------------------------------------------------------

STAY_LANES = ("Best match", "Your voted style", "Best value", "Top rated",
              "Closest to the center", "Splurge", "Budget pick", "Also fits")
FILL_LANE = "Also fits"


def stay_target_for(base: int, votes: dict | None) -> int:
    """Per-city target: enough slots for every voted style plus three."""
    voted = len([v for v in (votes or {}).values() if v])
    return max(int(base or 0), voted + 3)


def pick_stay_lanes(scored: list[dict], target: int, votes: dict | None = None,
                    band: dict | None = None, best_match_k: int | None = None) -> list[dict]:
    """Choose up to `target` records, each stamped with the lane it won.

    Items need: key, rank_score, per_person_total, rating10, review_count,
    distance_km, style_vote, style_core. Lanes are claimed in the order of
    STAY_LANES (the top Best match first, then one hotel per voted style,
    then value / rating / the remaining Best matches / location / splurge /
    budget, then "Also fits" to fill); a record is never claimed twice.
    Display order is claim order. Returns copies with "lane" set."""
    target = max(int(target or 0), 0)
    if not scored or not target:
        return []
    k = best_match_k if best_match_k is not None else (4 if target >= 10 else 2)
    by_key = {s["key"]: s for s in scored}
    taken: dict = {}  # key -> lane, insertion-ordered = display order

    def free():
        return [s for s in scored if s["key"] not in taken]

    def claim(lane: str, item: dict | None) -> None:
        if item is None or item["key"] in taken or len(taken) >= target:
            return
        taken[item["key"]] = lane

    def price(s):
        return s.get("per_person_total") if s.get("per_person_total") is not None else float("inf")

    def by_rank(items):
        return sorted(items, key=lambda s: (-(s.get("rank_score") or 0), price(s), s.get("name") or ""))

    def reviews(s):
        return int(s.get("review_count") or 0)

    # 1. The single best match
    top = by_rank(free())
    claim("Best match", top[0] if top else None)
    # 2. Every style that got a vote shows up at least once, most votes first
    votes = {v: c for v, c in (votes or {}).items() if c}
    covered = {by_key[key]["style_vote"] for key in taken if by_key[key].get("style_core")}
    for vote, _count in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0])):
        if vote in covered:
            continue
        pool = [s for s in free() if s.get("style_vote") == vote and s.get("style_core")]
        if pool:
            claim("Your voted style", by_rank(pool)[0])
            covered.add(vote)
    # 3. Best value — the cheapest place with a trustworthy guest score
    decent = [s for s in free() if s.get("rating10") is not None and s["rating10"] >= 7.5 and reviews(s) >= 20]
    claim("Best value", min(decent, key=lambda s: (price(s), -(s.get("rank_score") or 0))) if decent else None)
    # 4. Top rated
    rated = [s for s in free() if s.get("rating10") is not None and reviews(s) >= 20]
    claim("Top rated", max(rated, key=lambda s: (s["rating10"], reviews(s), s.get("rank_score") or 0)) if rated else None)
    # 5. The remaining best matches
    for item in by_rank(free())[:max(k - 1, 0)]:
        claim("Best match", item)
    # 6. Closest to the center — only when distances are known for most candidates
    with_distance = [s for s in scored if s.get("distance_km") is not None]
    if len(with_distance) * 2 >= len(scored):
        near = [s for s in free() if s.get("distance_km") is not None]
        claim("Closest to the center", min(near, key=lambda s: (s["distance_km"], -(s.get("rank_score") or 0))) if near else None)
    # 7. Splurge — the priciest well-rated place, within reason
    if band and band.get("high"):
        ceiling = band["high"]
        fancy = [s for s in free() if (s.get("rating10") or 0) >= 8.5
                 and ceiling < price(s) <= 2 * ceiling]
    else:
        fancy = [s for s in free() if (s.get("rating10") or 0) >= 8.5 and price(s) != float("inf")]
    claim("Splurge", max(fancy, key=lambda s: (price(s), s.get("rank_score") or 0)) if fancy else None)
    # 8. Budget pick — the cheapest, full stop
    rest = free()
    claim("Budget pick", min(rest, key=lambda s: (price(s), -(s.get("rank_score") or 0))) if rest else None)
    # 9. Also fits — the next-best scores until the target is reached
    for item in by_rank(free()):
        if len(taken) >= target:
            break
        claim(FILL_LANE, item)

    return [dict(by_key[key], lane=lane) for key, lane in taken.items()]


def splurge_reason(item: dict, band: dict | None) -> dict | None:
    """The numeric line a Splurge card leads with."""
    price = item.get("per_person_total")
    if price is None or not band or not band.get("high") or price <= band["high"]:
        return None
    return {"kind": "budget", "short": "Splurge",
            "text": f"{_money(price - band['high'])} per person over the group's ceiling"}


# ---------------------------------------------------------------------------
# Flights — facts -> roles, reasons, score
# ---------------------------------------------------------------------------

FLIGHT_ROLES = ("cheapest", "nonstop", "fastest", "balanced", "bags",
                "early_arrival", "no_early_start", "late_return", "one_airline")
ROLE_LANES = {
    "cheapest": "Cheapest", "nonstop": "Nonstop", "fastest": "Fastest", "balanced": "Best balance",
    "bags": "Bags included", "early_arrival": "Early arrival", "no_early_start": "No early start",
    "late_return": "Late return", "one_airline": "One airline",
}
_PACE_ROLE_ORDER = {
    "very": ("cheapest", "early_arrival", "late_return", "nonstop", "fastest", "balanced",
             "bags", "no_early_start", "one_airline"),
    "slightly": ("cheapest", "no_early_start", "nonstop", "balanced", "fastest",
                 "bags", "late_return", "early_arrival", "one_airline"),
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def flight_role_order(pace: str | None) -> tuple[str, ...]:
    """A very busy group wants to land early and leave late; a relaxed one
    wants no dawn alarm — the pace moves those roles up the claim order."""
    return _PACE_ROLE_ORDER.get(pace or "", FLIGHT_ROLES)


def hm(minutes) -> str:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return ""
    h, m = divmod(max(minutes, 0), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def parse_hhmm(when: str | None) -> int | None:
    """'2026-08-14 17:55' / '2026-08-14T17:55:00' -> minutes since midnight."""
    if not when:
        return None
    s = str(when).replace("T", " ")
    try:
        hh, mm = s.split(" ")[1][:5].split(":")
        return int(hh) * 60 + int(mm)
    except (IndexError, ValueError):
        return None


def short_date(iso: str | None) -> str:
    """'2026-11-20…' -> 'Nov 20'."""
    s = str(iso or "")[:10]
    try:
        _, m, d = s.split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}"
    except (ValueError, IndexError):
        return s


def _fmt_hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "?"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _similar(a: dict, b: dict) -> bool:
    """Near-identical price and door-to-door time read as a duplicate."""
    if a.get("key") and a.get("key") == b.get("key"):
        return True
    return (abs(a["price"] - b["price"]) < max(10, 0.02 * b["price"])
            and abs((a.get("minutes") or 0) - (b.get("minutes") or 0)) <= 20)


def flight_reasons(role: str | None, f: dict, floor_price: float, cheapest_minutes: int,
                   median_price: float, supplier: str = "liteapi") -> list[dict]:
    """Up to three reasons: what the role means for this itinerary, the price
    gap to the cheapest fare, and bags."""
    reasons: list[dict] = []
    price = float(f.get("price") or 0)
    fare_word = "live fare" if f.get("live") else "fare"
    text, short = "", ""
    if role == "cheapest":
        gap = int(round(median_price - price))
        text = (f"Cheapest {fare_word} — ${gap} under the median option" if gap >= 5
                else f"Cheapest {fare_word} on these dates")
        short = "Cheapest"
    elif role == "nonstop":
        text, short = f"Nonstop both ways — {hm(f.get('minutes'))} door to door", "Nonstop"
    elif role == "fastest":
        text, short = "Fastest routing on these dates", "Fastest"
    elif role == "balanced":
        saved = max(0, int(cheapest_minutes or 0) - int(f.get("minutes") or 0))
        hours = round(saved / 60)
        gap = int(round(price - floor_price))
        text = (f"Within ${gap} of the cheapest and {hours}h faster" if hours >= 1
                else f"Within ${gap} of the cheapest fare")
        short = "Balanced"
    elif role == "bags":
        text, short = "Checked bag included — no surprise fees", "Bag included"
    elif role == "early_arrival":
        when = _fmt_hhmm(f.get("out_arrive"))
        date = short_date(f.get("out_arrive_date"))
        text = (f"Lands {when} on {date} — most of day one in {f.get('dest_city') or 'town'}" if date
                else f"Lands {when} local — most of day one in {f.get('dest_city') or 'town'}")
        short = f"Lands {when}"
    elif role == "no_early_start":
        when = _fmt_hhmm(f.get("out_depart"))
        text, short = f"Departs {when} — no dawn alarm", f"Departs {when}"
    elif role == "late_return":
        when = _fmt_hhmm(f.get("back_depart"))
        text, short = f"Home flight at {when} — a full last day", f"Home at {when}"
    elif role == "one_airline":
        carrier = f.get("carrier") or "One airline"
        text, short = f"{carrier} both ways — one check-in, one app", "One airline"
    if text:
        reasons.append({"kind": "lane", "text": text, "short": short[:18]})
    if role != "cheapest" and price > floor_price + 0.5:
        gap = int(round(price - floor_price))
        reasons.append({"kind": "price", "text": f"${gap} more than the cheapest fare", "short": f"${gap} more"})
    if f.get("checked_bag") and role != "bags":
        reasons.append({"kind": "bags", "text": "Checked bag included", "short": "Bag included"})
    elif f.get("carry_on") and not f.get("checked_bag"):
        reasons.append({"kind": "bags", "text": "Carry-on only", "short": "Carry-on only"})
    return [_estimated(r, supplier) for r in reasons[:3]]


def flight_match_score(f: dict, floor_price: float, floor_minutes: int) -> int:
    price = float(f.get("price") or 0) or 1.0
    minutes = int(f.get("minutes") or 0) or 1
    return int(round(100 * (0.6 * min(1.0, (floor_price or 1) / price)
                            + 0.4 * min(1.0, (floor_minutes or 1) / minutes))))


def pick_flight_roles(facts: list[dict], limit: int, pace: str | None = None,
                      supplier: str = "liteapi") -> list[dict]:
    """Claim up to `limit` genuinely different itineraries, one per role.

    Each fact dict: key, price, minutes, stops, out_depart, out_arrive
    (minutes since midnight, local), out_arrive_date, back_depart,
    checked_bag, carry_on, single_carrier, carrier, dest_city, live.
    Every role ranks its qualifying itineraries and takes the FIRST one that
    is not already on the board and not a near-duplicate of one that is — a
    role is never lost because its favourite looked like the cheapest pick.
    Returns [{"index", "role", "lane", "reasons", "match_score"}] in claim
    order."""
    if not facts or not limit:
        return []
    prices = [float(f.get("price") or 0) for f in facts]
    minutes_all = [int(f.get("minutes") or 0) for f in facts]
    floor_price = min(prices)
    floor_minutes = min(minutes_all)
    median_price = statistics.median(prices)
    cheapest = min(range(len(facts)), key=lambda i: (prices[i], minutes_all[i]))
    cheapest_minutes = minutes_all[cheapest]

    picks: list[dict] = []
    taken_idx: set[int] = set()

    def claim(role: str, ordered: list[int]) -> None:
        if len(picks) >= limit:
            return
        for idx in ordered:
            if idx in taken_idx:
                continue
            f = facts[idx]
            if any(_similar(f, facts[p["index"]]) for p in picks):
                continue
            taken_idx.add(idx)
            picks.append({
                "index": idx,
                "role": role,
                "lane": ROLE_LANES[role],
                "reasons": flight_reasons(role, f, floor_price, cheapest_minutes, median_price, supplier),
                "match_score": flight_match_score(f, floor_price, floor_minutes),
            })
            return

    def free():
        return [i for i in range(len(facts)) if i not in taken_idx]

    def by_price(idxs):
        return sorted(idxs, key=lambda i: (prices[i], minutes_all[i]))

    f = facts
    for role in flight_role_order(pace):
        if role == "cheapest":
            claim(role, by_price(free()))
        elif role == "nonstop":
            claim(role, by_price([i for i in free() if f[i].get("stops") == 0]))
        elif role == "fastest":
            claim(role, sorted(free(), key=lambda i: (minutes_all[i], prices[i])))
        elif role == "balanced":
            fp, fm = floor_price or 1, floor_minutes or 1
            claim(role, sorted(free(), key=lambda i: prices[i] / fp + minutes_all[i] / fm))
        elif role == "bags":
            claim(role, by_price([i for i in free() if f[i].get("checked_bag")]))
        elif role == "early_arrival":
            # The earliest MORNING landing — a 00:40 touchdown is a night
            # arrival, not most of day one
            idxs = [i for i in free() if f[i].get("out_arrive") is not None
                    and 5 * 60 <= f[i]["out_arrive"] < 14 * 60]
            claim(role, sorted(idxs, key=lambda i: (f[i]["out_arrive"], prices[i])))
        elif role == "no_early_start":
            claim(role, by_price([
                i for i in free()
                if f[i].get("out_depart") is not None and f[i]["out_depart"] >= 9 * 60
                and f[i].get("out_arrive") is not None and 6 * 60 <= f[i]["out_arrive"] <= 22 * 60]))
        elif role == "late_return":
            claim(role, by_price([i for i in free() if f[i].get("back_depart") is not None
                                  and f[i]["back_depart"] >= 15 * 60]))
        elif role == "one_airline":
            claim(role, by_price([i for i in free() if f[i].get("single_carrier")]))
    return picks


def label_flights(facts: list[dict], pace: str | None = None, supplier: str = "ai") -> list[dict]:
    """Role/lane/reasons/score for EVERY itinerary of an already-fixed menu
    (the AI and sample flights): roles are claimed as usual; the rest get no
    lane but still carry the price gap, bags and a score."""
    if not facts:
        return []
    picks = {p["index"]: p for p in pick_flight_roles(facts, len(facts), pace, supplier)}
    prices = [float(f.get("price") or 0) for f in facts]
    minutes_all = [int(f.get("minutes") or 0) for f in facts]
    floor_price, floor_minutes = min(prices), min(minutes_all)
    median_price = statistics.median(prices)
    cheapest_minutes = minutes_all[min(range(len(facts)), key=lambda i: (prices[i], minutes_all[i]))]
    out = []
    for i, f in enumerate(facts):
        p = picks.get(i)
        if p:
            out.append({"role": p["role"], "lane": p["lane"], "reasons": p["reasons"],
                        "match_score": p["match_score"]})
        else:
            out.append({"role": None, "lane": None,
                        "reasons": flight_reasons(None, f, floor_price, cheapest_minutes, median_price, supplier),
                        "match_score": flight_match_score(f, floor_price, floor_minutes)})
    return out


# ---------------------------------------------------------------------------
# Board-schema helpers — AI/sample menus scored by the same rules
# ---------------------------------------------------------------------------

def parse_duration_minutes(text: str | None) -> int:
    """'9h 10m' / '45m' / '2h' -> minutes (0 when unparseable)."""
    if not text:
        return 0
    total, num = 0, ""
    for ch in str(text).casefold():
        if ch.isdigit():
            num += ch
        elif ch == "h" and num:
            total += int(num) * 60
            num = ""
        elif ch == "m" and num:
            total += int(num)
            num = ""
        elif not ch.isspace():
            num = ""
    return total


def normalize_rating(rating) -> tuple[float | None, str | None]:
    """(rating /10, the scale it arrived on) — AI menus rate out of 5."""
    if rating is None:
        return None, None
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return None, None
    if r <= 5:
        return round(r * 2, 1), "5"
    return round(r, 1), "10"


def annotate_stays(stays: list[dict], prefs: dict | None, nights: int, supplier: str,
                   target: int | None = None, fill_lane: str | None = None) -> list[dict]:
    """Score, lane and explain a menu of stays that already exists in the
    board schema (AI or sample menus). Returns the re-ordered list; the
    model's own `why` survives only when the data gives nothing to say.
    With `fill_lane` every stay gets that lane instead of competing."""
    prefs = prefs or {}
    nights = max(int(nights or 1), 1)
    group = int(prefs.get("group_size") or 1)
    records = []
    for i, s in enumerate(stays):
        if not isinstance(s, dict):
            continue
        rating10, scale = normalize_rating(s.get("rating"))
        pp_total = s.get("total_per_person")
        try:
            pp_total = float(pp_total) if pp_total is not None else None
        except (TypeError, ValueError):
            pp_total = None
        record = {
            "key": s.get("id") or f"{supplier}-{i}",
            "name": s.get("name") or "",
            "type_label": s.get("type") or "",
            "rating10": rating10,
            "review_count": s.get("review_count"),
            "distance_km": s.get("distance_km"),
            "per_person_total": pp_total,
            "nights": nights,
            "facilities": list(s.get("amenities") or []),
            "description": "",
            "group_size": group,
        }
        record.update(score_stay(record, prefs, supplier))
        record["_stay"] = s
        record["_scale"] = scale
        records.append(record)
    if fill_lane:
        chosen = [dict(r, lane=fill_lane) for r in sorted(records, key=lambda r: -r["rank_score"])]
        chosen = chosen[:target] if target else chosen
    else:
        chosen = pick_stay_lanes(records, target or len(records), prefs.get("accommodation_votes"),
                                 prefs.get("lodging_band"))
    out = []
    for rec in chosen:
        s = dict(rec["_stay"])
        s["lane"] = rec["lane"]
        s["match_score"] = rec["match_score"]
        s["reasons"] = rec["reasons"]
        s["supplier"] = s.get("supplier") or supplier
        s["rating10"] = rec["rating10"]
        s["rating_scale"] = rec["_scale"]
        if rec["reasons"]:
            s["why"] = why_line(rec["lane"], rec["reasons"])
        out.append(s)
    return out


def flight_facts_from_option(opt: dict, dest_city: str = "", live: bool = False) -> dict:
    """Facts for a board-schema flight (depart/arrive strings, est price,
    duration labels) — what the AI and sample menus carry."""
    airline = str(opt.get("airline") or "")
    return {
        "key": opt.get("id") or opt.get("journey_key") or airline,
        "price": float(opt.get("est_price_per_person") or 0),
        "minutes": parse_duration_minutes(opt.get("duration")) + parse_duration_minutes(opt.get("return_duration")),
        "stops": opt.get("stops"),
        "out_depart": parse_hhmm(opt.get("depart")),
        "out_arrive": parse_hhmm(opt.get("arrive")),
        "out_arrive_date": str(opt.get("arrive") or "")[:10],
        "back_depart": parse_hhmm(opt.get("return")),
        "checked_bag": bool((opt.get("bags") or {}).get("checked")),
        "carry_on": bool((opt.get("bags") or {}).get("carry_on")),
        "single_carrier": bool(airline) and "+" not in airline and "combo" not in airline.casefold(),
        "carrier": airline,
        "dest_city": dest_city,
        "live": live,
    }


def annotate_flights(flights: list[dict], pace: str | None, dest_city: str, supplier: str) -> list[dict]:
    """Lane/reasons/score for a fixed AI or sample flight menu, in place."""
    facts = [flight_facts_from_option(o, dest_city, live=bool(o.get("live_price"))) for o in flights]
    for opt, label in zip(flights, label_flights(facts, pace, supplier)):
        opt["lane"] = label["lane"]
        opt["reasons"] = label["reasons"]
        opt["match_score"] = None if supplier in ESTIMATED_SUPPLIERS else label["match_score"]
        opt.setdefault("supplier", supplier)
        if label["lane"] and label["reasons"]:
            opt["why"] = label["reasons"][0]["text"]
    return flights


def summarize_suppliers(options: list[dict]) -> list[str]:
    """Distinct `supplier` values, most common first — the board's provenance."""
    counts = Counter((o.get("supplier") or "") for o in options if isinstance(o, dict))
    return [s for s, _ in counts.most_common() if s]
