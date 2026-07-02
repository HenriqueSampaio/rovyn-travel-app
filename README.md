# Rovyn — plan the whole group trip in one place

Rovyn is a centralized trip-planning platform for groups. Instead of juggling flight sites, hotel tabs, Airbnb searches, tour marketplaces and endless group chats, one person creates the trip, everyone answers a short questionnaire, an AI proposes destinations tailored to the whole group, the group votes, and Rovyn assembles a full itinerary plus a booking hub with everything ready to purchase.

## How it works

1. **Sign up / sign in.** Every traveler has an account; trips belong to users, so you can leave and come back to any trip from **Your journeys**.
2. **Create a trip.** The trip leader names the trip and gets a 6-character invite code and a shareable link (`/join/CODE`).
3. **Friends join.** Anyone with the code/link joins the group (they'll be asked to sign in first).
4. **Everyone fills the questionnaire.** Each member submits:
   - travel date window and budget range,
   - the city they fly from (used to build flight searches),
   - whether they'd rather stay in one base city or explore multiple cities within a country,
   - their vibes — nature expeditions, nightlife & parties, culture, food, beach, adventure, wellness, shopping,
   - preferred accommodation style and any special requirements.
5. **Trip dashboard.** Everyone sees how many people have joined and how many questionnaires are complete, live.
6. **AI recommendations.** Once *all* questionnaires are in, the leader unlocks the "Generate destination recommendations" button. The AI (Claude) reconciles everyone's answers — the overlapping date window, the budget range everyone can afford, the most-voted vibes, multi-city vs. single-base preference — and proposes 5 destinations with match scores, highlights, suggested routes and per-person budget estimates.
7. **The group votes.** Every member votes for their favorite (votes can change until the last vote lands). When everyone has voted, the winner is selected automatically — ties break toward the higher AI match score.
8. **Itinerary.** The leader builds a day-by-day itinerary for the winning destination: morning/afternoon/evening activities, a recommended stay, meals budgets, cost breakdown and local tips.
9. **Booking hub.** One screen with everything purchasable:
   - ✈️ **Flights** — Expedia searches pre-filled with the trip dates and group size, one per departure city,
   - 🏨 **Stays** — Airbnb and Booking.com searches pre-filled with destination, dates and party size,
   - 🎟️ **Tours & activities** — GetYourGuide and Viator links, with the itinerary's bookable activities called out.

## Tech

- **Backend:** FastAPI (Python) with a **SQLite database** (`backend/data/rovyn.db`) — tables for `users`, `auth_sessions`, `trips`, `trip_members`, `questionnaires`, and `votes`. Passwords are PBKDF2-hashed; API access uses bearer tokens. Plain SQL throughout, so migrating to Postgres/Supabase later is mechanical.
- **Frontend:** React + Vite with `framer-motion` (page and step transitions) and `lucide-react` (icons). Editorial design system in `src/index.css` — Fraunces/Karla type, underline inputs, no boxed cards.
- **AI:** Anthropic Claude (`claude-opus-4-8`) generates recommendations and itineraries. Without an API key the app still works end-to-end using clearly-labeled sample data, so the whole flow is demoable.
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
- Trip social feed: share photo/video recaps of completed trips, and let viewers **copy a trip** — clone the itinerary with their own dates and rebook it in a few taps.
- Payments splitting between group members.
- Postgres/Supabase migration for cloud deployment.
