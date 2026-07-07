# Google Maps Platform — what it can do for Rovyn

*Written July 7, 2026, against the current docs (developers.google.com/maps) and the
pricing model in effect since March 1, 2025. One server-side `GOOGLE_PLACES_API_KEY`
is live with the Places API (New) and the Maps Static API enabled — both verified
with real calls on this date. Items marked ✅ are implemented on this branch.*

---

## The pricing model in one paragraph

Since **March 1, 2025** the old $200 monthly credit is gone. Instead every SKU has
its own free monthly call allowance — **10,000 free calls/month for Essentials SKUs,
5,000 for Pro, 1,000 for Enterprise** — then pay-as-you-go per 1,000 calls. The tier
of a Places request is set by the *most expensive field in its field mask* ("you are
billed at the highest SKU applicable to your request"). Reference:
`developers.google.com/maps/billing-and-pricing/pricing` and `…/march-2025`.
Numbers below are the list prices read from those pages on July 6–7, 2026 —
re-check in the Cloud console before making cost-sensitive decisions; Google moves
these more often than it used to.

| SKU (what Rovyn touches) | Tier | Free/month | After that (per 1,000) |
|---|---|---|---|
| Static Maps | Essentials | 10,000 | ~$2 |
| Geocoding | Essentials | 10,000 | ~$5 |
| Maps Embed API | — | **unlimited, free** | free |
| Autocomplete requests | Essentials | 10,000 | ~$2.83 |
| Dynamic Maps (Maps JS) | Essentials | 10,000 | ~$7 |
| Routes: compute routes / route matrix | Essentials | 10,000 | ~$5 |
| Time Zone | Essentials | 10,000 | ~$5 |
| Text Search (Pro fields: location, displayName…) | Pro | 5,000 | ~$32 |
| Text Search (Enterprise + Atmosphere: reviews, summary) | Enterprise | 1,000 | ~$40 |
| Place Details (Enterprise + Atmosphere) | Enterprise | 1,000 | ~$25 |
| Place Photos | Enterprise* | 1,000–5,000 | ~$7 |

**The pattern that matters:** map images, geocoding, embeds and routing are cheap
Essentials SKUs with 10k free calls. *Rich place data* (reviews, editorial
summaries) is the expensive part — which is exactly why Rovyn caches every details
answer on the trip and never asks twice.

---

## What Rovyn already uses ✅

### Places Text Search + rich fields — the option-detail engine
`backend/services/hotel_service.py: place_details()` pulls editorial summary,
address, opening hours, website, Google Maps link, rating, reviews and photos for
any stay/visit/event, in one `places:searchText` call. **Now also returns
`places.location` coordinates** (this branch), which feed the location maps.
Results are cached inside the trip's options JSON on first open — each option
costs at most one Enterprise+Atmosphere call, ever.

*Honest cost note:* because the field mask includes `reviews` +
`editorialSummary`, every uncached detail-open bills in the priciest Places tier
(1,000 free/month). At Rovyn's current scale (a board has ~20 options; only opened
options fetch) this is nothing. If detail-opens ever exceed ~30/day sustained,
split the mask: fetch Pro fields always, Atmosphere fields only for the review
panel.

### Places photos, resolved server-side ✅
`_places_photo_urls()` exchanges photo resource names for plain image URLs on the
backend, so the API key never reaches the browser. Used by detail pages and
review panels.

### Places Autocomplete — city inputs ✅ (pre-existing)
`places_service._google_suggest()` powers destination/departure-city fields, with
the offline GeoNames dataset as fallback. Verified working with the new key.
*Small optimization available:* Google bills autocomplete cheaper when keystrokes
are grouped into a session (session tokens) that ends in a Place Details call —
Rovyn's inputs don't end in a details call, so per-request billing is actually
correct for us. Leave as is.

### Maps Static API — the map visuals ✅ (this branch)
`backend/services/maps_service.py` + two endpoints in `backend/routers/trips.py`:

- `GET /trips/{id}/map/route` — numbered pine pins in route order joined by a clay
  path (multi-city), or one destination pin (single-city). Plan board Overview and
  FinalPlan render it.
- `GET /trips/{id}/map/option/{oid}` — one clay pin where a stay/visit/event sits.
  OptionDetail renders it.

Styled to the Rovyn palette with Static Maps `style=` params (cream landscape
`#F0EADC`, pine-tinted water `#BFD2C8`, POI/transit clutter off, soft ink labels).
A static map is a pure function of its URL, so images cache on disk *forever*
(`backend/data/map_cache/`, keyed by URL hash) — a given trip's map bills once in
its lifetime, then serves from disk. Geocodes cache the same way (`geocode.json`).
Fetches are proxied server-side; the frontend gets PNGs through Bearer-authed,
membership-checked endpoints, so outsiders can't farm our quota.

*Limits to know:* max 640×640 at scale 1–2 (we request scale 2 = 1280px wide,
crisp on phones); URL length cap 16,384 chars (a 9-city route with styles is
~1.3k — no risk); Google's logo/attribution must stay visible (we never crop).
Above the free tier, Google recommends signed URLs (HMAC signature) for Static
Maps — worth adding if Rovyn ever serves >10k *distinct* maps/month; the cache
makes that unlikely for a long time.

---

## What's on the table next

### 1. Routes API — real travel times and true route lines (top pick)
`routes.googleapis.com` `computeRoutes`: origin, destination, travel mode
(**transit included** — trains and buses, i.e. exactly Rovyn's inter-city legs),
returns duration, distance and an **encoded polyline**.

Concretely in Rovyn:
- The Transport tab's leg durations ("5h30 by train") are currently AI estimates.
  One transit `computeRoutes` call per leg gives real schedules-grade durations.
- The route map's straight pin-to-pin lines become the actual path: Static Maps
  accepts `path=enc:<polyline>` directly — no frontend change at all, just a
  smarter `path` parameter in `maps_service.route_map_png`.
- Feasibility guard at board generation: if the AI proposes Rome→Interlaken as a
  "2h hop", the real 6h answer can demote or annotate it.

Effort: ~a day (new service function + cache polylines next to geocodes + wire
into transport generation). Cost: Essentials, 10k free/month; legs-per-board is
tiny and cacheable per (from, to, mode). *Caveat:* transit coverage is uneven in
some countries; fall back to the AI estimate when the API returns nothing.

### 2. Maps Embed API — a free interactive map behind a tap
An iframe (`google.com/maps/embed/v1/place?q=…` / `directions?origin=…`) gives
pan/zoom/Street-View interactivity with **unlimited free usage**. Rovyn's taste is
"verbose things behind taps": tapping a static location map could open a
full-screen sheet with the interactive embed (and `directions` mode between route
cities).

*The catch:* the key rides in the iframe URL, visible in the browser. That
requires a **second, separate key** — HTTP-referrer-restricted to Rovyn's domains
and API-restricted to the Embed API only. Since embeds are free, a leaked embed
key can't run up a bill, but never reuse the server key here. Effort: small
(a sheet component + one env var + key setup in the console).

### 3. Route Matrix / Distance Matrix — day-plan sanity
`computeRouteMatrix` prices like compute-routes and answers "how far is each
starred sight from the chosen stay?" in one call. Uses:
- "12 min walk from your hotel" chips on visit cards (the data already exists to
  render them the moment coordinates are cached — coordinates now are).
- Final-plan day ordering: warn when a day's slots zigzag across town.

Effort: moderate — the API is easy; deciding where verdicts surface in the UI is
the real work. Pairs well with #1.

### 4. Geocoding API — cheaper coordinates (micro-optimization)
`maps_service.geocode_place()` currently geocodes through Places Text Search with
a `places.location` mask — that's a **Pro** call (~$32/1k after 5k free) for data
the **Geocoding API** sells at ~$5/1k after 10k free. The permanent geocode cache
means Rovyn's real volume is a handful of calls per new destination, so this is
pennies either way — but it's a one-function swap next time someone touches the
file. Not urgent; noted for honesty.

### 5. Time Zone API
One call per destination gives the IANA zone for correct "evening" slot times and
flight-arrival local times. Cheap, cacheable forever. Only becomes visible when
Rovyn starts rendering times across zones (flights already carry local times from
the AI, so today it would change almost nothing user-visible).

### 6. Maps JavaScript API (Dynamic Maps) — when maps become a workspace
Full interactive maps in-page: draggable markers, clustering, custom overlays,
vector styling. This is the right tool the day Rovyn wants map-first *editing* —
dragging a stay pin to explore neighborhoods, or an explore-on-map picker for
visits. Needs the same browser-key hygiene as #2 (referrer-restricted key), a
~150KB JS load, and design work to not break the editorial feel. ~$7/1k loads
after 10k free. Not before the product needs map *interaction*; the static
pipeline covers map *reading* well.

### 7. Street View Static API — niche filler imagery
A street-level photo by coordinates (~$7/1k). Could fill photo-less AI stays or
give "the street you'd sleep on" shots. Imagery quality varies wildly outside
cities; LiteAPI galleries + Places photos already cover the common case. Low
priority.

### 8. Environment APIs (Weather, Air Quality, Pollen)
Google's Weather API (preview) could add packing hints and rain-aware day
ordering near the trip date. Interesting after the itinerary becomes date-aware
enough to reshuffle days; skip for now.

Not relevant to Rovyn today: Address Validation (no shipping addresses),
Photorealistic 3D Tiles / Aerial View (spectacular, heavy, no planning value yet),
Roads API (no GPS traces).

---

## Key security — current state and required follow-ups

- The single key lives server-side only (`.env` → backend); the frontend receives
  finished PNGs and photo URLs, never the key. Map endpoints require trip
  membership, so quota can't be farmed anonymously.
- **Do in the Cloud console:** API-restrict the key to exactly what the backend
  uses — Places API (New) + Maps Static API (+ Geocoding if #4 lands). When Rovyn
  deploys to a stable host, add an IP restriction (App Engine/VM egress IPs).
- Any future browser-facing feature (Embed, Maps JS) gets its **own** key,
  referrer-restricted and API-restricted. Never ship the server key in HTML.

## Deferred on this branch (and why)

- **FinalPlan per-day maps with slot pins** — day slots are free-text strings
  ("Morning at the Uffizi"), so pinning them means fuzzy-matching text back to
  board options. The honest fix is upstream: have the final-plan generator carry
  option ids per slot, then maps fall out of the options cache trivially. The
  whole-trip route map ships now instead.
- **Static route paths along real roads/rails** — waiting on Routes API (#1);
  today's maps draw straight geodesic pin-to-pin lines, which read as "trip
  shape", not driving directions, and that's fine for the Overview.
