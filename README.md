# Rovyn — plan the whole group trip in one place

Rovyn is a centralized trip-planning platform for groups. Instead of juggling flight sites, hotel tabs, Airbnb searches, tour marketplaces and endless group chats, one person creates the trip, everyone answers a short questionnaire, an AI lays out tailored options in five categories, everyone stars what they love, and Rovyn merges the votes into one final plan — majority wins, every decision explained — through approval, an itemized check and equal-split payment between the group.

## How it works

A **journey tracker** at the top of every trip page shows where the group is:
`Preferences → Destination → Star favorites → Final plan → Approve & pay`.

1. **Sign up / sign in.** Every traveler has an account; trips belong to users, so you can leave and come back to any trip from **Your journeys**.
2. **Create a trip.** The trip leader names the trip and — optionally — pins a **destination** ("Hvar, Croatia"). With a pinned destination the group skips destination voting entirely; left blank, the AI proposes destinations (and treats the trip *name* as a strong hint, so "Summer in Hvar" leans Adriatic). Either way the leader gets a 6-character invite code and a shareable link (`/join/CODE`).
3. **Friends join.** Anyone with the code/link joins the group (they'll be asked to sign in first).
4. **Everyone fills the questionnaire.** Dates, budget, departure city (and, if different, the city you fly home to — Rovyn plans the full round trip), single-base vs multi-city, vibes, accommodation style, special requirements. Choosing **multi-city** opens a wishlist picker: AI-suggested cities near the destination plus a free search, with live hints showing which cities travel mates already picked.
5. **Destination.** Pinned by the leader, or: the AI proposes 5 destinations reconciling everyone's answers, the group votes, ties break toward the higher AI match score.
6. **The planning board.** The leader opens the board and Rovyn lays out concrete options in five tabs — **Flights** (up to 8 explained round-trip itineraries per departure city — cheapest, nonstop, early arrival, late return…), **Stay** (12 properties on a single-base trip, at least every voted style + 3 per city on a route, each with a lane like *Best value* or *Top rated* and data-backed reasons it was picked for the group), **Itinerary** (3 day-by-day variants with different personalities), **Visits** (6 sights) and **Events** (4 time-specific experiences). See `docs/OPTIONS-BREADTH-SPEC.md` for how stays and flights are chosen and explained. When the group chose multi-city, Rovyn first plans the **route** (which cities, in what order, how many nights each — drawn from the group's wishlist votes) and the board gains a **Transport** tab (train/flight/bus options per leg, starred like everything else); flights become open-jaw (land in the first city, fly home from the last), stays are offered per city for that city's exact dates, and visits/events are tagged to their city. Every traveler:
   - **stars** everything they'd enjoy (stars are votes, changeable until the final plan is drafted),
   - **locks in each section** — or taps **"I don't mind"** when they have no strong opinion,
   - follows their 5-section checklist; the Overview tab shows live group progress.
7. **The final plan.** When all travelers finish all sections, the leader drafts the final plan. Winner selection is **deterministic majority rule** computed in code — the AI only weaves the winning choices into a coherent day-by-day schedule. A **"How the group decided"** section explains every call with real vote counts ("2 of 3 travelers with an opinion (67%) starred the boutique hotel…"). Every starred visit and event gets a slot. Multi-city plans add a **route timeline** and city-banded itinerary (each day shows which city the group is in), one chosen stay and transport leg per city, and a **time-frame check**: if the group starred more events in a city than its nights can hold, the most-starred ones win the evenings and the decision log says why.
8. **Approve, edit, or both.** Every traveler approves the plan. Before anyone pays, *any* member can:
   - **request a change** in plain words — the AI revises the plan minimally and logs the revision,
   - **reorder days** with arrows directly on the plan.
   Any edit resets approvals so the whole group signs off again.
9. **The check & payment.** Once all approve, everyone gets an itemized check: shared costs (stay, activities, visits, events, meals, transport) **split equally**, plus their own flight priced from their departure city. Each traveler pays their share (demo payment — no real money moves), gets an in-app receipt and an **email receipt** (via Resend, when `RESEND_API_KEY` is set). When the last person pays, the trip is **booked**. Booking deep links (Expedia / Airbnb / Booking.com / GetYourGuide) stay attached to options for real-world purchase.

## Tech

- **Backend:** FastAPI (Python) with a dual-mode database layer (`db.py`): **SQLite** (`backend/data/rovyn.db`) out of the box, **Postgres/Supabase** when `DATABASE_URL` is set — same plain SQL runs on both. Tables for `users`, `auth_sessions`, `trips`, `trip_members`, `questionnaires`, `votes`, plus the planning workflow: `stars`, `category_progress`, `approvals`, `payments`. Auth is dual-mode too: built-in PBKDF2 passwords + bearer tokens by default, or **Supabase Auth** (email, Google, Apple — JWTs verified via JWKS) when `SUPABASE_URL` is configured; legacy accounts are adopted by email on first Supabase sign-in. See `docs/SUPABASE_SETUP.md` for the hosted setup and `backend/scripts/migrate_to_postgres.py` for the one-shot data copy.
- **Frontend:** React + Vite with `framer-motion` (page and step transitions) and `lucide-react` (icons). Editorial design system in `src/index.css` — Fraunces/Karla type, underline inputs, no boxed cards. Key pages: `Plan.jsx` (the tabbed planning board) and `FinalPlan.jsx` (decisions, approvals, edits, check, payment).
- **AI:** a two-tier, provider-agnostic layer (`backend/services/llm.py`). The **standard** tier (Claude Sonnet 5 by default) generates destination recommendations, routes, the option menus, the final-plan weave and free-text revisions; the **fast** tier (Claude Haiku 4.5 by default) handles the schema-bound work — flight/transport menus and city ideas. Option detail screens are NOT AI-generated: real hotels pull their LiteAPI page plus live room rates on first open (rates re-quoted on every open), visits/events/other stays pull Google Places facts (hours, address, reviews, photos), and the static parts cache on the trip (`hotel_service.real_option_details`). Tiers are configured via `LLM_FAST` / `LLM_STANDARD` as `provider/model`; a stdlib Google Gemini provider activates with `GEMINI_API_KEY` (any Gemini failure falls back to Anthropic). The board's stay menu and visits/events menu generate as parallel calls, so the board is ready when the slowest fetch lands, not after one giant one. Group decisions themselves (which option wins, at what percentages, and all cost math) are computed deterministically in `ai_service.py` — the model narrates, code decides. Without an API key the app still works end-to-end with clearly-labeled sample data.
- **Email:** payment receipts go out through Resend's HTTP API (`backend/services/email_service.py`, stdlib-only). Without `RESEND_API_KEY` the send is simulated and the UI says so.
- **Payments & booking (prototype):** the group checkout makes Rovyn the merchant of record — each traveler's card is *authorized* on a Stripe-hosted page, nobody is charged until everyone has paid, then Rovyn books the stay (LiteAPI, net rates) and the flights (Duffel holds paid from a balance), captures every card and writes the confirmations into the Bookings tab, refunding any item that fails. Without keys the whole flow runs simulated (`SIM-…` confirmations); only provably-test credentials are accepted unless `ROVYN_LIVE_MONEY=1`. Design: `docs/PAYMENTS-MOR-SPEC.md`; what going live takes: `docs/MOR-RUNBOOK.md`; provider facts: `docs/PROVIDER-API-FACTS.md`. Services in `backend/services/{payment,booking,duffel_booking}_service.py`, endpoints in `backend/routers/payments.py`, tests in `tests/test_payments_flow.py`.
- **Live suppliers:** LiteAPI (`LITEAPI_API_KEY`, free sandbox) prices real hotels and flights for the board; Duffel (`DUFFEL_ACCESS_TOKEN`, free self-serve test tokens) is an optional second flight supplier whose offers merge with LiteAPI's (`DUFFEL_STAYS=1` adds its accommodations once live Stays access exists). How wide the board casts its net is tunable: `STAY_OPTIONS_SINGLE`, `STAY_OPTIONS_PER_CITY`, `FLIGHT_OPTIONS_PER_CITY`, `STAY_CANDIDATES`, `STAY_SEARCH_DEADLINE_S`, `LITEAPI_RATE_PER_S` — all documented in `.env.example`.
- **Booking links:** Airbnb and Booking.com have no open consumer API and Expedia's requires partner credentials, so the MVP generates deep search links with all parameters pre-filled. The link builders live in `backend/services/links.py` and are the seam where real APIs (Expedia Rapid, Booking.com Connect, Viator Partner) plug in later.

## Running it

```bash
# Backend (from the repo root)
cp .env.example .env          # add your ANTHROPIC_API_KEY for AI-personalized results
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --port 8000

# Frontend
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

Open http://localhost:5173 and create an account.

**Testing with two users:** sign-in state is per browser profile, so use a normal window for the leader and an **incognito window** (or a different browser) for the second traveler — the same way you'd test two Gmail accounts. Two tabs in the same window share the same signed-in user.

## Roadmap

- Real booking APIs for in-app checkout (flights, stays, tours) — no leaving the app.
- Real payment rails (Stripe) behind the existing check/receipt flow.
- Trip social feed: share photo/video recaps of completed trips, and let viewers **copy a trip** — clone the itinerary with their own dates and rebook it in a few taps.
- Postgres/Supabase migration for cloud deployment.
