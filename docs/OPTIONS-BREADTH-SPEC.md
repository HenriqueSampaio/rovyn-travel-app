# Spec: Wider, explained stay & flight options — "Picked for you"

*Drafted 2026-10-18; corrected the same day after a three-lens critique and the provider
research (LiteAPI, Duffel — docs fetched 2026-10-18). Scope: the planning board's Stay and
Flights menus. Parent docs: `docs/BOOKING-ARCHITECTURE.md` (suppliers), `README.md` (board
flow). Ships on `feature/wider-options`, PR into `dev`. Backend plus one line of frontend
(the board's detail prefetch); the planning-board UI that renders the new fields lands in the
`feature/glanceable-ui` PR — this PR is already visible through the richer `why` line every
card prints today.*

## Objective

Travelers open the board and see **too few** stays and flights, and the ones they see are not
obviously connected to what the group answered in the questionnaire. Before this change:

- Stays: `real_stay_options` asked LiteAPI for **40** hotels, priced them in one call and kept
  the top 4 by accommodation-type affinity plus the cheapest and priciest — **6 options**,
  ranked only by the `accommodation_type` votes. Budget, vibes, guest scores, location and
  group size played no part, and the `why` was a canned sentence. Multi-city trips kept 3 per
  city. (The research confirmed this was entirely client-side: LiteAPI's list endpoint
  defaults to 200 hotels and the rates endpoint accepts every filter the questionnaire maps to.)
- Flights: `_pick_diverse` kept at most **4** journeys per departure city out of the dozens
  LiteAPI returns, with a fixed one-liner per role.
- Only **one** supplier (LiteAPI) was wired; Duffel existed in the architecture doc, not in code.

**User story:** on the Stay tab I see 12 real properties (at least "voted styles + 3", five by
default, per city on a route), each with a one-line, data-backed reason it was picked *for us*
— "Best match · Bed & breakfast — the style 3 of 4 of you voted for" — and up to four reason
chips behind it (In budget · 8.9/10 · 9 min walk · Spa & Pool). The set spans every style the
group voted for, the budget band, the top-rated places and a value pick. Flights show up to 8
genuinely different itineraries per city, each labeled by what it is best at.

**Success looks like:** every accommodation style that got a vote is on the board when the city
has it; the cheapest and best-rated hotels *among those that were priced* are always present;
every explanation is computed from the data (never written by a model); a city's stay search
never takes longer than its wall-time budget; `tests/test_options_breadth.py` (118 checks) and
`tests/test_maps_and_links.py` pass.

## Principles (inherited from the architecture doc §4)

1. **The model proposes, suppliers dispose.** Every number on a card comes from a supplier;
   every reason is a deterministic function of those numbers and the questionnaire.
2. **Best-effort everywhere.** A wider search that fails degrades to id batches, then to the
   AI menu, then to samples. Board generation never breaks.
3. **No new dependencies.** stdlib `urllib`, `ThreadPoolExecutor`, existing helpers.
4. **Never mark a hotel down for data we don't have.** Dimensions a supplier cannot inform stay
   out of the score instead of being guessed at.

## Design

### Pipeline (stays) — `hotel_service.real_stay_options`

Under one absolute deadline per city (`STAY_SEARCH_DEADLINE_S`, 25 s) and the shared LiteAPI
throttle, concurrently:

```
GET  /data/hotels?cityName&countryCode&limit=STAY_CANDIDATES   content: types, facilityIds, scores, coordinates
POST /hotels/rates {countryCode, cityName, limit=STAY_CANDIDATES, maxRatesPerHotel=1, timeout=10}
                                                               the cheapest offer per hotel, one call
POST /hotels/rates {… hotelTypeIds=[core ids], limit=40}        ≤2 top-ups, one per voted style the first
                                                               page carries fewer than 8 core matches of
GET  /data/facilities                                          id→name map, cached per process (24 h)
geocode_place(destination)                                     the city center (Google, cached on disk)
        │
        ├─ by-city rates answered nothing (204 / timeout)? → price the content list in id batches of 50
        ├─ priced < max(4, target // 3)?                   → ONE re-price of the best unpriced hotels
        ├─ priced hotels missing from the content page?    → GET /data/hotels?hotelIds=… (one call)
        ▼
records (type, label, stars, rating /10, reviews, distance, per-person total, facility names, description)
        → option_ranking.score_stay → pick_stay_lanes → options
```

Anything not back by the deadline is dropped and reported (`report.truncated`); a failed rates
call counts in `report.batches_failed`. The by-city rates search is the documented main-query
form of the endpoint; the id-batched path is the one every board used before and remains the
fallback. Occupancy stays the `⌈group/2⌉` doubles heuristic (the rooming work replaces it).
`official_website()` is no longer called at board time — the option page heals links lazily.

### Throttle — `hotel_service._lite_request`

One process-wide pacer for **every** LiteAPI call (board searches, flight searches, galleries,
reviews, details): a token bucket at `LITEAPI_RATE_PER_S` (default 8/s; 4/s on a sandbox key,
whose limit is 5/s shared by hotels and flights) holding at most one token, so no one-second
window sees more than rate + 1 calls. It is a rate, not a concurrency cap — a 45 s flight search
never queues a stay search behind it. Calls made under a search deadline are not sent once it
has passed. A 429 on a rates call is retried once after `Retry-After` when the deadline allows;
dropped keep-alives, resets and truncated bodies retry like a 503. Multi-city boards search
their cities concurrently under this pacer instead of serially. `/data/hotel` is memoized per
hotel for an hour, so the gallery and the option page share one fetch.

### Configuration (all optional, defaults in code)

```
STAY_OPTIONS_SINGLE=12      # stays shown on a single-base trip
STAY_OPTIONS_PER_CITY=5     # stays per city on a route — never fewer than voted styles + 3
FLIGHT_OPTIONS_PER_CITY=8   # itineraries per departure city
STAY_CANDIDATES=150         # properties priced before choosing (capped at 60 per route city)
STAY_SEARCH_DEADLINE_S=25   # wall-time budget for one city's whole stay search
LITEAPI_RATE_PER_S=         # pacing; default 8, 4 on a sandbox key
DUFFEL_ACCESS_TOKEN=        # second flight supplier (test tokens are free and self-serve; airports resolve through LiteAPI)
DUFFEL_STAYS=               # 1 = Duffel accommodations on the Stay tab (live Stays access only)
```

### Data contract — fields added to every stay option (`options.schema_version = 2`)

```json
{
  "lane": "Best value",
  "match_score": 84,
  "reasons": [
    {"kind": "budget",   "text": "$410 per person for 5 nights — inside your $300–$900 stay budget", "short": "In budget"},
    {"kind": "style",    "text": "Bed & breakfast — the style 3 of 4 of you voted for",            "short": "Bed & breakfast"},
    {"kind": "rating",   "text": "8.9/10 from 1,204 guest reviews",                                "short": "8.9/10"},
    {"kind": "vibe",     "text": "Spa & Pool — matches the group's relaxation vibe",                "short": "Spa & Pool"}
  ],
  "why": "Best value · $410 per person for 5 nights — inside your $300–$900 stay budget",
  "supplier": "liteapi", "sandbox": true, "currency": "USD",
  "rating10": 8.9, "rating_scale": "10",
  "distance_km": 0.7, "lat": 45.43, "lng": 12.33
}
```

- `lane` ∈ `Best match · Your voted style · Best value · Top rated · Closest to the center ·
  Splurge · Budget pick · Also fits`.
- `supplier` ∈ `liteapi · duffel · ai · sample` (per option; `source` stays the board-level
  and details-level field it already is).
- `reasons[].short` is a ≤18-character chip label, never cut inside a word: an accommodation
  vote has a fixed short form ("airbnb/vacation rental" → "Vacation rental"); anything longer
  is cut back to its last whole word and ends in "…". `why` is the lane plus the strongest
  reason (the reason alone when that would pass ~88 characters). `reasons` is capped at 4; a
  budget verdict is never squeezed out by softer signals.
- `rating10` is the guest score out of 10; AI/sample ratings arrive out of 5 and are
  normalized (`rating_scale` records the origin). `rating` keeps its old meaning for old UI.
- `match_score` is emitted only when at least three dimensions carry real data and never for
  AI/sample menus. On an estimate, every reason whose number comes from the menu (`budget`,
  `price`, `rating`, `location`, a flight's `lane` line) is prefixed `est.`; a `style` reason's
  "2 of 3 of you voted for" is the group's real tally and stays bare.
- Duffel stays additionally carry `bookable: false`, `duffel_accommodation_id`,
  `duffel_search_result_id` and `duffel_details` (their option page is the search payload).
  They never carry `liteapi_hotel_id`, so rooming and booking paths ignore them.
- Every pre-existing field keeps its name and meaning.

Board-level additions: `schema_version`, `generated_at`, `group_prefs` (`accommodation_votes`,
`budget_range`, `top_vibes`, `group_size`, `group_pace` — so the UI can print what the group
asked for), `lodging_band` (null when unknown), `stay_search` — `{town: {listed, priced,
batches_failed, truncated, top_ups, mode, duffel}}` in EVERY mode (a single-base trip has one
town, the anchor), `stay_suppliers`, `flight_suppliers`, `flights_sandbox` (= any sandbox
itinerary). `flights_source` / `stay_source` name the supplier actually present (`liteapi`
when any LiteAPI live option is on the board, else the other supplier).

### Data contract — fields added to every flight option

```json
{
  "lane": "Early arrival", "role": "early_arrival", "match_score": 74,
  "reasons": [
    {"kind": "lane",  "text": "Lands 08:20 on Nov 21 — most of day one in Rome", "short": "Lands 08:20"},
    {"kind": "price", "text": "$38 more than the cheapest fare",                 "short": "$38 more"},
    {"kind": "bags",  "text": "Checked bag included",                             "short": "Bag included"}
  ],
  "why": "Lands 08:20 on Nov 21 — most of day one in Rome",
  "supplier": "duffel", "sandbox": true, "currency": "USD",
  "duffel_offer_id": "off_…", "duffel_offer_ids": ["off_…"], "offer_ids": []
}
```

Roles stay snake_case internally (`cheapest nonstop fastest balanced bags early_arrival
no_early_start late_return one_airline`); `lane` is the display name. `why` is the lane reason
only (plus the existing open-jaw note); bags live in `reasons`, so nothing prints twice.
`offer_ids` are LiteAPI's (the `/flights/verify` handle); Duffel ids are separate.

### Scoring (stays) — `option_ranking.score_stay`, pure

| Dimension | Weight | Known when | Rule | Reason |
|---|---|---|---|---|
| style | 0.35 | the group voted | vote-share-weighted `style_affinity` (core 1.0 / partial 0.5; the old `_affinity` rules, now matching LiteAPI type ids **or** label words for AI/sample/Duffel stays) | core: `"{Type label} — the style k of n of you voted for"`; partial: `"Close to the {vote} style you asked for"` |
| budget | 0.25 | a band exists | per-person total for THIS stay's nights vs the band for the same nights: 1.0 inside, 0.85 under, linear to 0 at 1.6× the ceiling | `"$410 per person for 5 nights — inside your $300–$900 stay budget"` / `under your …` / `$14 over your $436 stay ceiling` (≤1.3×) / `$214 per person over the group's ceiling` (>1.3×) |
| rating | 0.20 | a guest score exists | `rating/10 × confidence`, confidence `min(1, log10(reviews+1)/3)`; a missing or zero count gets 0.4 (≈15 reviews' worth — never more than a real sample) | printed only with ≥20 reviews: `"8.9/10 from 1,204 guest reviews"` (≥8.0); `"Top-rated: 9.4/10 from …"` (≥9) |
| vibe | 0.10 | facilities or a description exist | **whole-word/phrase** regex hits (never substrings — "spa" in "spacious" is not a spa) on the **facility names** first (LiteAPI `facilityIds` joined to `/data/facilities`), the description second, for the top two vibes — never the hotel's name; central vibes also accept ≤1.5 km | `"Spa & Pool — matches the group's relaxation vibe"`, `"Central — close to the nightlife you voted for"` |
| location | 0.10 | coordinates and a city center exist | haversine to the center: ≤1.5 km 1.0, ≤4 km 0.6, else 0.3 (0.6 when beach/nature/relaxation are top vibes) | `"Right in the center"` (<250 m), `"9 min walk from the center"` (≤2 km at 80 m/min), `"3.2 km from the center"` (≤4 km) |

`rank_score` = the weighted mean over the **known** dimensions (always computed, for ordering);
`match_score` = that number when ≥3 dimensions are known and the supplier is live. A group of
four or more also earns `"Sleeps the whole group under one roof"` on home-type properties
(by type id, or by whole-word label — a guest house never qualifies).
Ties between a vibe hit and a location hit go to the vibe (the more personal line).

**Lodging band** (`option_ranking.lodging_band(members, nights)`): members who left the ceiling
blank are ignored; `low = max(0, maxmin − 50·nights − 80) × 0.45`, `high = max(0, minmax − 50·nights
− 80) × 0.65` (the same meals/transport constants the final check uses — `option_ranking`
owns `MEALS_PER_DAY` / `LOCAL_TRANSPORT_FLAT` and `compute_costs` reads them from there);
non-overlapping ranges fall back to
the full span; `None` when nobody gave a ceiling, there are no nights, or the ceiling is under
$25 per person per night (too small to mean anything — then no budget reasons and no Splurge
lane at all). Route cities get the band scaled by their share of nights. A floor of $0 is
never printed: the verdict is phrased against the ceiling alone ("inside your $436 stay budget").

### Lanes — `option_ranking.pick_stay_lanes`

Claim order (a hotel is never claimed twice; display order = claim order; `target` caps it):

1. **Best match** — the single best `rank_score`.
2. **Your voted style** — one core match per voted style not yet covered, most votes first.
3. **Best value** — cheapest with a guest score ≥7.5 **and ≥20 reviews** (else it's the Budget pick).
4. **Top rated** — highest score with ≥20 reviews.
5. **Best match** — the next `k−1` by rank (k = 4 when target ≥10, else 2).
6. **Closest to the center** — only when ≥50 % of candidates have a distance.
7. **Splurge** — only a place rated ≥8.5 that is pricier than everything already on the board
   AND than the set's median price; with a band it must also sit between the ceiling and 2× the
   ceiling and leads with `"$x per person over the group's ceiling"` (for AI/sample menus too,
   prefixed `est.`); skipped entirely when the budget was too small to form a band.
8. **Budget pick** — only when it really is the cheapest thing on the board; otherwise skipped
   (the lane that already took the cheapest hotel satisfies criterion 3).
9. **Also fits** — next-best ranks until `target`.

Lanes are truthful: a lane whose claim would not hold against the board so far is skipped,
never stretched onto the next-best record.

Per-city target = `max(STAY_OPTIONS_PER_CITY, voted styles + 3)`. Thin live pricing (fewer than
`max(4, target // 3)` real stays after the re-price) is padded by the board with the AI/sample
menu — `supplier` ai/sample, `lane` "Also fits", `live_price` false, `stay_all_live` false.

### Flights — `flight_service` + `option_ranking.pick_flight_roles`

LiteAPI and Duffel are searched in parallel (Duffel under its own 15 s timeout, so it can only
add options). Candidates merge by itinerary signature (sorted carrier + flight number +
departure tuples — Duffel candidates set `journey_key` to it): the cheaper copy wins; a copy in
another currency than the board's is dropped (no silent FX); sandbox inventory never mixes with
production inventory (production wins). `sandbox` is per candidate (LiteAPI `sand_` key, Duffel
`live_mode` false).

Roles are claimed in the pace's order — `very` busy promotes early_arrival and late_return to
right after cheapest, `slightly` promotes no_early_start — and each role ranks its qualifying
itineraries and takes the **first that is not a near-duplicate** of one already on the board
(within 2 % price and 20 minutes door to door), so a role is never lost to a look-alike:

| Role | Rule | Reason |
|---|---|---|
| cheapest | lowest price | `"Cheapest live fare — $270 under the median option"` |
| nonstop | 0 stops both ways, cheapest | `"Nonstop both ways — 18h 05m door to door"` |
| fastest | fewest door-to-door minutes — only an itinerary within 20 min of the pool's fastest AND no slower than anything already on the board; otherwise the role is skipped | `"Fastest routing on these dates"` |
| balanced | min `price/floor + minutes/floor` among itineraries not dominated (pricier AND slower) by an existing pick | `"Within $142 of the cheapest and 17h faster"` / `"Same price as the cheapest, 2h faster"` |
| bags | checked bag included, cheapest | `"Checked bag included — no surprise fees"` |
| early_arrival | earliest **morning** landing (05:00–14:00 local), the date stated | `"Lands 06:30 on Nov 21 — most of day one in Rome"` |
| no_early_start | departs ≥09:00, lands 06:00–22:00, cheapest | `"Departs 16:20 — no dawn alarm"` |
| late_return | home flight departs ≥15:00, cheapest | `"Home flight at 17:15 — a full last day"` |
| one_airline | one marketing carrier on every segment, cheapest | `"Lufthansa both ways — one check-in, one app"` |

Then the price gap to the cheapest (`"$38 more than the cheapest fare"`) and bags.
`match_score = 100 × (0.6 × floor/price + 0.4 × floor_minutes/minutes)`.

**Open jaw.** When the suppliers ignore the open-jaw return (Duffel's true open-jaw offers
already took part in the primary search), two LiteAPI one-way searches are paired per role:
outbound-driven roles (early arrival, no early start) take their outbound pick plus the
cheapest return; late return takes the cheapest outbound plus its return pick; one airline needs
the same carrier on both legs; bags needs both; nonstop needs both; price/time roles pair
role-for-role. Reasons are judged on the combined trip against the other pairs.

**Detail page.** `live_flight_details` re-verifies through the supplier that quoted the fare:
LiteAPI `/flights/verify` for LiteAPI options, `GET /air/offers/{id}` for Duffel options
(`source: "duffel"`); only while the offer can still be alive.

AI-estimated flights are asked for 6 per city spanning the roles; samples grow to 5. Both are
labeled by the same role picker (`label_flights`): roles claimed as usual, the rest carry the
price gap and bags with no lane; `match_score` is omitted for estimates.

### Duffel Stays — adapter shipped, wiring opt-in

`duffel_service.search_stays` maps `POST /stays/search` results (priced from
`cheapest_rate_total_amount`, USD only) into stay records with photos, amenities and the
description as a ready detail sheet. It joins the board only with `DUFFEL_STAYS=1`, because
Duffel's test mode serves a synthetic test hotel at a fixed location and live Stays access is
requested separately from Flights — it cannot widen a real board today. When wired, a Duffel
copy of a LiteAPI hotel (matching distinctive name words, within 150 m) is dropped; the rest
enter scoring and lanes like any other record.

### Board payload and media — `trips.py`

- `GET /trips/{id}/options` ships each stay/visit/event's cached detail sheet as a stub
  `{"kind": "cached", "source": …, "cached": true}` (response only; storage unchanged; flights and
  transport untouched). The option page always fetches `/option-details`; the board's prefetch
  treats `kind` in (`live`, `cached`) as already warmed.
- `stay_media_for_options` fetches three stays at a time and returns `(media, complete)`;
  `read_stay_media` serves a throttled (429) answer but does **not** freeze it on the trip.
- The option page's room-rate re-quote is one quick attempt behind a bounded wait (about 5 s);
  a slow or failed re-quote leaves the cached sheet unchanged — the page never blocks on a
  fresh price.

## Project structure

```
backend/services/option_ranking.py   (new) pure: lodging band, scoring, reasons, stay lanes, flight roles,
                                      annotate_* for AI/sample menus — no I/O
backend/services/duffel_service.py   (new) urllib client; flight search → candidates; get_offer; stays search
backend/services/hotel_service.py    throttle; wide search (deadline pool, top-ups, fallbacks, re-price,
                                      facilities map, records, lanes); media (media, complete); Duffel detail sheet
backend/services/flight_service.py   merge suppliers; roles + reasons; open-jaw pairing; supplier-aware verify
backend/services/ai_service.py       lodging band on prefs; concurrent cities; per-city targets; padding;
                                      board provenance fields; wider AI/sample menus; shared cost constants
backend/routers/trips.py             _lighten_details; media not persisted when incomplete
frontend/src/pages/Plan.jsx          prefetch skips kind in (live, cached) — one line
.env.example                         the new optional variables
tests/test_options_breadth.py        (new) 118 checks; tests/fixtures/*.json
```

## Tests — `tests/test_options_breadth.py` (plain python)

The fake `urllib` layer records every call in `CALLS` (URL + parsed body) and appends unknown
URLs to `CALLS["unexpected"]` before raising; every scenario asserts it is empty. Scenarios:
the lodging band formula and its edge cases; the single-base wide search (12 options, lanes,
exact reason strings derived from fixture fields, top-ups, content fetch for top-up hotels, the
documented rates knobs, no official-website lookups, the report); votes driving top-ups and the
per-city target with three voted styles; the 204 → id-batch fallback, a failing batch, a hung
batch against a 1.5 s deadline; thin pricing (one re-price, then board padding with estimates,
`stay_all_live` false); a tiny budget (no budget reasons, no splurge in the cheapest third);
sample menus (8 stays, /5 ratings normalized, `est.` on estimated numbers but never on vote
tallies, chips never cut mid-word); flights (8 distinct lanes, both suppliers called once, the
duplicate itinerary collapsed to the cheaper USD copy, the median formula, pace orders, GBP copy
rejected, supplier-aware re-verify); open jaw (≥6 lanes, flies home from Florence); no keys → no
calls; Duffel stays mapped, deduped and gated; the multi-city board end to end (6 per city, all
three styles per city, city-night budget reasons, provenance, each departure city's flight link
sized to its own travelers); the API (detail stubs, throttled media not frozen).
`tests/test_maps_and_links.py` stays green.

## Boundaries

- **Always:** deterministic reasons; degrade on any supplier failure; keep every existing option
  field; one deadline per city search; stdlib only; blank `DATABASE_URL`/`SUPABASE_URL` on any
  test or spike run.
- **Ask first:** new dependencies; changing the `⌈group/2⌉` occupancy heuristic (the rooming
  work); turning `DUFFEL_STAYS` on by default; changing the lane order.
- **Never:** let a model write a number or a reason; call `/data/hotel` per candidate during
  board generation; mix currencies or sandbox/production inventory on one board.

## Success criteria

1. A single-base trip with a LiteAPI key shows 12 stays (or every priced property when fewer
   exist), each with a `lane`, `reasons` with chip labels, a `supplier`, and — for live
   suppliers with ≥3 known dimensions — a `match_score`; the existing cards print the lane-led
   `why` with no frontend change.
2. Every accommodation style that received a vote appears at least once when the city has it;
   a route city shows at least voted styles + 3.
3. The cheapest and the best-rated **priced** hotels are always on the board.
4. Flights show up to 8 distinct itineraries per city, lanes never lost to look-alikes; with
   `DUFFEL_ACCESS_TOKEN` set Duffel offers merge and dedupe; without it no Duffel call is made,
   and a Duffel token without the LiteAPI key makes no call to either supplier.
5. A city's stay search returns within `STAY_SEARCH_DEADLINE_S` even when a supplier hangs.
6. `tests/test_options_breadth.py` and `tests/test_maps_and_links.py` pass; `npm run build`
   is clean.

## Sandbox findings (2026-10-18, see the PR description)

Recorded from the spike script against the sandbox key: list size, priced count, rates wall
time by city vs by id batch, `maxRatesPerHotel` behaviour, `/hotels/min-rates` availability,
and the lanes/reasons `real_stay_options` and `real_flight_options` produced for Rome.
