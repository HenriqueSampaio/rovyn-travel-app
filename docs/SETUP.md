# Rovyn — Manual Setup Guide

Everything that requires an account, a key, or a dashboard click by a human.
Code seams already exist for each of these — the items below are the only parts
Rovyn can't do for itself.

## 1. Git workflow (already configured)

- **`main`** — releases only. Never commit here directly.
- **`dev`** — integration branch. All feature branches start from and merge back into `dev`.
- **Every change** = new branch off `dev` (`feature/...`, `fix/...`, `docs/...`) → commit →
  push → **pull request into `dev`** on GitHub, which you review and merge.
- Periodically, open a PR from `dev` → `main` to cut a release.

Suggested one-time GitHub settings (repo → Settings → Branches → Add branch ruleset):
- Protect `main` and `dev`: require a pull request before merging.
- Enable "Automatically delete head branches" (repo → Settings → General) to keep the
  branch list clean after merges.

## 2. Environment variables (`Rovyn/.env`)

```bash
cp .env.example .env
```

| Variable | Where to get it | What breaks without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API keys | App falls back to clearly-labeled **sample data**: generic destinations (this is why "Summer in Hvar" once suggested Lisbon), generic planning-board options, and no AI plan revisions. |
| `RESEND_API_KEY` | [resend.com](https://resend.com) → API keys | Payment receipt emails are **simulated** (receipt still stored in-app; UI says no email was sent). |
| `EMAIL_FROM` | See §3 | Falls back to Resend's shared test sender `onboarding@resend.dev`. |

Restart the backend after editing `.env` — it is read once at startup.

## 3. Email (Resend) — production checklist

1. Create a Resend account and API key; put it in `RESEND_API_KEY`.
2. The default sender `onboarding@resend.dev` **only delivers to your own Resend account
   email** — fine for testing, useless in production.
3. For real users: buy a domain → Resend dashboard → **Domains → Add domain** → add the
   DKIM/SPF DNS records they show you → wait for "Verified" → set
   `EMAIL_FROM="Rovyn <receipts@yourdomain.com>"`.
4. Code seam: `backend/services/email_service.py` (stdlib HTTP call, no SDK). Swapping to
   another provider (Postmark, SES) means changing one function.

## 4. Payment architecture (future — how it will work)

Today's flow is a **demo**: "Pay my share" records a payment row and emails a receipt;
no money moves. The flow was deliberately shaped so real payments slot in without
changing the user experience:

```
final plan approved by all
        │
        ▼
GET /trips/{id}/check          ← per-member itemized check (already exists)
        │                        shared costs split equally + own-city flight
        ▼
POST /trips/{id}/pay           ← replace the body of this endpoint with Stripe
        │
        ▼
all paid → status 'booked'     ← webhook flips this instead of the endpoint
```

Recommended architecture — **Stripe Checkout** (hosted page, no card data ever touches
your server, minimal PCI scope):

1. **Manual setup**: create a Stripe account → dashboard → Developers → API keys →
   add `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` to `.env`. No frontend key needed
   for hosted Checkout.
2. **`POST /trips/{id}/pay`** becomes: compute the member's check (unchanged) → create a
   Stripe **Checkout Session** with `amount = total * 100` cents, the itemized check as
   `line_items`, `metadata = {trip_id, user_id}` → return the session URL → frontend
   redirects to it.
3. **New webhook endpoint** `POST /webhooks/stripe`: on `checkout.session.completed`,
   verify the signature with `STRIPE_WEBHOOK_SECRET`, then do exactly what `pay` does
   today — insert the `payments` row, send the receipt email, flip the trip to `booked`
   when the last member pays. (Manual setup: dashboard → Developers → Webhooks → add your
   deployed URL; for local testing use `stripe listen --forward-to localhost:8000/webhooks/stripe`.)
4. **Refund path for group edits**: payments lock the plan today; keep that rule. If you
   later allow post-payment edits, refund via the Stripes dashboard first.
5. **Money flow**: all charges land in *your* Stripe balance — Rovyn is the merchant of
   record, which is correct while Rovyn itself books the trips. Only if you later pay
   out to third parties (hotels, guides) would you need Stripe **Connect**; don't start
   there.
6. Keep `payments.receipt` as the immutable snapshot of what was charged — Stripe's
   receipt complements it, doesn't replace it.

## 5. Booking APIs (future)

Deep links (Expedia, Airbnb, Booking.com, GetYourGuide, Viator) are generated in
`backend/services/links.py` — that file is the seam. Real in-app checkout requires
partner programs with manual applications:
- **Expedia Rapid API** (flights/hotels) — partner application at developers.expediagroup.com
- **Booking.com Connectivity/Demand API** — partner application
- **Viator Partner API** — easiest to get into; affiliate tier is open signup

## 6. Going to production (when you're ready)

1. **Database**: SQLite → Postgres (e.g. Supabase). Schema is plain SQL in
   `backend/db.py`; JSON columns become `jsonb`. See `docs/DATABASE.md`.
2. **Secrets**: move `.env` values into your host's secret manager (Render/Railway/Fly
   all have one). Never commit `.env` (already gitignored).
3. **CORS**: `backend/main.py` allows only `localhost:5173/3000` — add your deployed
   frontend origin.
4. **API base URL**: `frontend/src/api/index.js` hardcodes `http://localhost:8000` —
   switch to an env-driven value (`import.meta.env.VITE_API_URL`).
5. **Auth hardening**: session tokens currently never expire and passwords have no reset
   flow — add both before real users.
6. **Polling → websockets** (optional): pages poll every ~3s; fine for small groups,
   upgrade if trips get large.
