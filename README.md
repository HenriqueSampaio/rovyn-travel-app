# Rovyn — plan the whole group trip in one place

Rovyn is a centralized trip-planning platform for groups. Instead of juggling flight sites, hotel tabs, Airbnb searches, tour marketplaces and endless group chats, one person creates the trip, everyone answers a short questionnaire, an AI lays out tailored options in five categories, everyone stars what they love, and Rovyn merges the votes into one final plan — majority wins, every decision explained — through approval, an itemized check and equal-split payment between the group.

## How it works

A **journey tracker** at the top of every trip page shows where the group is:
`Preferences → Destination → Star favorites → Final plan → Approve & pay`.

1. **Sign up / sign in.** Every traveler has an account; trips belong to users, so you can leave and come back to any trip from **Your journeys**.
2. **Create a trip.** The trip leader names the trip and — optionally — pins a **destination** ("Hvar, Croatia"). With a pinned destination the group skips destination voting entirely; left blank, the AI proposes destinations (and treats the trip *name* as a strong hint, so "Summer in Hvar" leans Adriatic). Either way the leader gets a 6-character invite code and a shareable link (`/join/CODE`).
3. **Friends join.** Anyone with the code/link joins the group (they'll be asked to sign in first).
4. **Everyone fills the questionnaire.** Dates, budget, departure city, single-base vs multi-city, vibes, accommodation style, special requirements.
5. **Destination.** Pinned by the leader, or: the AI proposes 5 destinations reconciling everyone's answers, the group votes, ties break toward the higher AI match score.
6. **The planning board.** The leader opens the board and the AI generates concrete options in five tabs — **Flights** (3 per departure city), **Stay** (4 styles/price points), **Itinerary** (3 day-by-day variants with different personalities), **Visits** (6 sights) and **Events** (4 time-specific experiences). Every traveler:
   - **stars** everything they'd enjoy (stars are votes, changeable until the final plan is drafted),
   - **locks in each section** — or taps **"I don't mind"** when they have no strong opinion,
   - follows their 5-section checklist; the Overview tab shows live group progress.
7. **The final plan.** When all travelers finish all sections, the leader drafts the final plan. Winner selection is **deterministic majority rule** computed in code — the AI only weaves the winning choices into a coherent day-by-day schedule. A **"How the group decided"** section explains every call with real vote counts ("2 of 3 travelers with an opinion (67%) starred the boutique hotel…"). Every starred visit and event gets a slot.
8. **Approve, edit, or both.** Every traveler approves the plan. Before anyone pays, *any* member can:
   - **request a change** in plain words — the AI revises the plan minimally and logs the revision,
   - **reorder days** with arrows directly on the plan.
   Any edit resets approvals so the whole group signs off again.
9. **The check & payment.** Once all approve, everyone gets an itemized check: shared costs (stay, activities, visits, events, meals, transport) **split equally**, plus their own flight priced from their departure city. Each traveler pays their share (demo payment — no real money moves), gets an in-app receipt and an **email receipt** (via Resend, when `RESEND_API_KEY` is set). When the last person pays, the trip is **booked**. Booking deep links (Expedia / Airbnb / Booking.com / GetYourGuide) stay attached to options for real-world purchase.

## Tech

- **Backend:** FastAPI (Python) with a **SQLite database** (`backend/data/rovyn.db`) — tables for `users`, `auth_sessions`, `trips`, `trip_members`, `questionnaires`, `votes`, plus the planning workflow: `stars`, `category_progress`, `approvals`, `payments`. Passwords are PBKDF2-hashed; API access uses bearer tokens. Plain SQL throughout (with lightweight column migrations in `db.py`), so migrating to Postgres/Supabase later is mechanical.
- **Frontend:** React + Vite with `framer-motion` (page and step transitions) and `lucide-react` (icons). Editorial design system in `src/index.css` — Fraunces/Karla type, underline inputs, no boxed cards. Key pages: `Plan.jsx` (the tabbed planning board) and `FinalPlan.jsx` (decisions, approvals, edits, check, payment).
- **AI:** Anthropic Claude (`claude-opus-4-8`) generates destination recommendations, the five-category option menu, the final-plan weave and free-text revisions. Group decisions themselves (which option wins, at what percentages, and all cost math) are computed deterministically in `ai_service.py` — the model narrates, code decides. Without an API key the app still works end-to-end with clearly-labeled sample data.
- **Email:** payment receipts go out through Resend's HTTP API (`backend/services/email_service.py`, stdlib-only). Without `RESEND_API_KEY` the send is simulated and the UI says so.
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
