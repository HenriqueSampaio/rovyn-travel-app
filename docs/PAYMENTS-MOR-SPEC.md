# Spec: In-app payment & auto-booking prototype — Rovyn as merchant of record

*Drafted 2026-10-18 against provider documentation fetched and fact-checked the same day
(`docs/PROVIDER-API-FACTS.md`); revised the same day after a four-lens design critique and
again to match the shipped implementation. Ships on `feature/stripe-mor-prototype`. Parent docs:
`docs/BOOKING-ARCHITECTURE.md` §2 "Phase C" (the money flow this implements), §3
(confirmations), §4 (accuracy). The legal and operational steps that turn this prototype into
real money are in `docs/MOR-RUNBOOK.md`.*

## Objective

The end goal of Rovyn is a **centralized trip-booking app**: every payment happens through the
app and every reservation is accessible in the app. This prototype builds the Phase C money
flow end to end — **lock live prices, authorize everyone, book what can be undone, capture
everyone, book the rest, pay the airlines, refund what fails** — so that with Stripe test keys,
the LiteAPI sandbox key and a Duffel test token the whole thing runs today with no real money,
and so that going live is configuration plus the runbook, not a rewrite.

**User story:** the plan is approved. A traveler opens *Costs & payment* and taps **See my
share at live prices**: Rovyn re-prices the stay with the hotel's booking system and asks the
airlines for the group's fares, holding them. Each traveler then fills in the details airlines
require, sees exactly what will be charged (only provider-verified items; estimates stay
estimates), and pays by card on a Stripe-hosted page — the card is **authorized**, not charged.
When the last traveler authorizes, Rovyn re-verifies every price, books the freely cancellable
hotel rate first, captures every authorization, books anything non-refundable, pays the held
flights from its Duffel balance, writes every confirmation (hotel booking id now, the hotel's
confirmation code when the hotel issues it, airline booking reference and e-ticket numbers)
into the Bookings tab, and emails each traveler a receipt. If a booking fails after capture,
that item's share is refunded to everyone automatically and the item falls back to the
self-book checklist. "I'll book it myself" keeps working and stays fee-free.

**Success looks like:** a 3-member trip in simulated mode (no keys) runs lock → authorize ×3 →
book → capture → booked with `SIM-…` confirmations; the same trip with `sk_test_` Stripe keys
runs the real hosted checkout with test cards and manual capture; with
`LITEAPI_BOOK_MODE=sandbox` the stay is a real sandbox reservation with a LiteAPI `bookingId`;
with a `duffel_test_` token the flight is a real test-mode held order paid from the test
balance; a forced booking failure refunds every traveler's share of that item.

## Non-goals (this PR)

- Real money: only provably-test credentials (`sk_test_` / `sand_` / `duffel_test_` / empty)
  are accepted unless `ROVYN_LIVE_MONEY=1` (`payment_service.refuse_live_keys`, called at import
  of `backend.main`).
- Viator/activity booking (certification needed) — visits and events remain checklist items
  with deep links and are **not charged** by Rovyn.
- Flights that the board sourced from LiteAPI Flights: LiteAPI's flight booking was not
  researched; only board options that carry a Duffel hint (`duffel_offer_id`, from the
  options-breadth work) are auto-booked — and even then from a fresh offer request at lock
  time, never from the stored id. Other flights are self-book items and are not charged.
- The rooming board (separate branch) — occupancies come from `plan.rooming[stay].mix` when it
  exists, else `⌈group/2⌉` doubles; hotel guests are filled per room in join order.
- Per-room split, instalments, a refunds UI beyond "release my authorization" and cancelling a
  booking, LiteAPI's Payment-SDK mode (Nuitée as merchant; see `SPEC.md` on `feature/rooming`).

## Principles

1. **Nobody is charged until everybody commits.** Authorize-all-then-capture, a visible
   deadline, automatic release on timeout or abandonment.
2. **Never charge for an estimate.** The charged total contains only items a provider priced
   (stay, Duffel flights) plus any configured service fee **inside** the first displayed total.
   Meals, local transport, visits, events and flights without a bookable fare stay
   informational and are never charged; a traveler whose city has no priced flight sees
   "not booked by Rovyn" — never a substituted average.
3. **Never book on a stale price.** Lock, then re-verify at settlement by *identity* (the same
   rate, the same itinerary), never "else the cheapest". A per-person rise above $1 pauses
   everything and asks the whole group to pay again (Stripe cannot raise an existing hold);
   a decrease is captured at the lower amount; a sub-$1 post-capture airline increase is
   absorbed by Rovyn.
4. **Suppliers first only when it is free to undo.** Refundable hotel rates whose free
   cancellation runs at least 48 h past the group deadline are booked before capture; airline
   holds cost nothing until paid. Everything else is bought after the cards are captured and
   refunded if it fails. Nothing non-refundable is bought before every capture succeeded, and
   a non-refundable item needs an explicit acknowledgment on the first screen.
5. **Card data never touches our server.** Stripe-hosted Checkout only (PCI SAQ A).
6. **No provider call inside an open database connection.** Each call is bracketed: persist
   the cursor and commit, call, persist the result and commit. A dead process leaves a cursor
   the reconciler resolves by READING the provider — money-moving calls are never repeated on
   a guess; the unknowable becomes `needs_attention` with an email to the leader.
7. **One money mode per settlement** (simulated | test | live), decided at lock time and
   re-checked against every credential and every provider object (`livemode`, `sandbox`,
   `live_mode`).
8. **Everything degrades honestly.** No Stripe key → simulated authorizations; no booking key →
   simulated hotels; no Duffel token → simulated flights — all clearly labeled, never next to
   live money. A real provider that refuses makes its item a self-book checklist entry that is
   not charged.
9. **Personal data is redacted** (`services/redact.py`) before anything is logged or persisted
   as audit: dates of birth, phones, emails, names on bookings, card hints never land in
   `settlement_events`, `webhook_events` or `bookings.raw`.
10. **Nothing is booked twice.** The group checkout and the self-book checklist share their
    items: what a traveler already recorded as booked is never priced, held or charged by
    Rovyn, and what the open round charges cannot be recorded as booked by hand.

## Flow

```
status = approved
   │
   ▼ POST /checkout/lock (any member)        — or the first POST /checkout/session
[VERIFY & LOCK]  one settlement per trip (round N), money_mode fixed, plan_hash stored
   stay:    POST /hotels/rates (booking key, margin:0, occupancies from the rooming mix or ⌈n/2⌉ doubles)
            → cheapest freely-cancellable rate (its *identity* = name + board + refundable tag + policy)
            → POST /rates/prebook → price locked; pre_capture_safe if refundable past deadline + 48 h
   flights: POST /air/offer_requests with THIS city's travelers → the board's itinerary by signature
            → instant-only / non-USD / holds shorter than 12 h → self-book with the reason
            → POST /air/orders type=hold once every passenger's details exist (no money moves)
   already booked by a traveler on the checklist (provider self, booked): no provider call,
            charge=false, "Already booked by <name> — not charged by Rovyn"
   others:  estimates, charge=false
   deadline terms: airline payment_required_by − 2 h
   │
   ▼ each traveler: POST /checkout/profile, then POST /checkout/session
[COLLECT]  pending payment_attempt inserted BEFORE the Stripe Checkout Session (manual capture,
           extended authorization requested, metadata {trip, user, settlement, attempt, round});
           return page → POST /checkout/confirm, and/or webhook → attempt authorized, capture_before kept
           deadline = min(first commitment + CHECKOUT_WINDOW_DAYS, card holds − 24 h, airline holds − 2 h),
           where a commitment is an authorized, nothing_due or captured attempt; recomputed whenever
           one appears or goes away (authorize, $0 commit, release, a hold Stripe drops) and NULL
           while nobody is committed
   │  everyone authorized → settle() (thread; inline in tests)
   ▼
[PREFLIGHT]  trip still approved · plan_hash unchanged · same members · money mode unchanged ·
             Σ shares = Σ items + fees · every PI requires_capture with the locked amount and > 3 h left
[RE-VERIFY]  hotels re-rated by identity and re-prebooked; held orders re-read (signature, conditions,
             price guarantee, total) → any rise > $1/person or lost guarantee → reconfirm (all released);
             an item a traveler recorded as booked after the lock leaves the charge (its hold released),
             so the shares fall and the capture takes the lower amount
[BOOK]       pre-capture-safe hotels (all-or-nothing) — LiteAPI /rates/book with ACC_CREDIT_CARD and
             the attempt_ref as clientReference; a timeout is resolved by re-POSTing the SAME reference
[CAPTURE]    every PaymentIntent, idempotency key capture:{pi}. A transport failure re-reads the PI:
             succeeded → continue; requires_capture → the same capture again (same key, up to 3×);
             canceled → treated as a refusal; anything else → needs_attention. Only an EXPLICIT refusal
             unwinds, and the round moves to `unwinding` BEFORE the first refund (hotels cancelled,
             captured shares refunded, holds released → failed) — a crash resumes the unwind, never a capture.
             Travelers who owe nothing (`nothing_due`) are settled at $0 without any provider.
[PAY]        the remaining hotels, then POST /air/payments per held order (price_changed ≤ $1/person
             absorbed, more → the item is persisted as failed FIRST, then its share refunded); a timeout
             re-reads the order
[CONFIRM]    only when no refund row is pending/failed; booked | partially_booked; trips.status = booked;
             receipts emailed; hotel confirmation code arrives by webhook (/webhooks/liteapi) or poll
```

Refunds are rows first: every refund for an item is planned in ONE write transaction (each
traveler capped at captured − refunded − already-pending), then POSTed with the row id as the
Idempotency-Key. A refund Stripe did not confirm stays `pending` with the cursor on the refund
step and parks the round in `needs_attention`; the reconciler re-POSTs it with the same key. An
explicit refusal marks the row `failed` for a person. A partial refund updates the stored
receipt's lines (`refunded`, `refunded_total`, `partially_refunded`) instead of wiping it.

## Data model (SQLite `_SCHEMA` + `_COLUMN_MIGRATIONS` **and** `supabase/migrations/20261018170000_payments_prototype.sql`; canary `bookings.cancelled_at`)

| Table | Purpose |
|---|---|
| `settlements` | one row per round: `id`, `trip_id`, `round`, `status` (collecting · reconfirm · verifying · booking · capturing · unwinding · paying · reconciling · booked · partially_booked · needs_attention · failed · cancelled · resolved), `money_mode`, `lock` (JSON), `plan_hash`, `cursor` (the provider call in flight; a parked round also carries `resume_to`), `deadline`. Partial unique index: one active (not cancelled/failed/resolved) per trip. |
| `settlement_events` | append-only audit: `step`, `item_key`, `level`, `message`, `data_redacted`. |
| `payment_attempts` | append-only per traveler and round: `checkout_session_id` / `payment_intent_id` (unique), `status` pending → authorized → captured → refunded (or `nothing_due` for a $0 share, releasing/released/failed/expired), amounts. Partial unique index: one pending/authorized/nothing_due/releasing/captured row per **settlement** and user, so a traveler refunded in one round can pay in the next. `releasing` is only left once Stripe confirms the cancel; until then no new attempt can start for that traveler (409). |
| `refunds` | persisted BEFORE Stripe is called; the row id is the Idempotency-Key; `pending` rows are re-POSTed by the reconciler, `failed` ones wait for a person. |
| `webhook_events` | provider event id as PK, `INSERT … ON CONFLICT DO NOTHING`; `processed_at NULL` = dead letter. |
| `traveler_profiles` | given/family name, date of birth, gender, title, E.164 phone — only what airlines and hotels need. |
| `payments` (existing) | the one-row-per-member summary everything else reads: `status` (`paid` for self-book rows, else mirrors the attempt), `provider`, `current_attempt_id`, captured/refunded cents. `paid` in the API means `status IN ('paid','captured')`; an authorized hold is reported as `payment_status: authorized` and never as paid. |
| `bookings` (existing) | gains `payer_id` (the one traveler a single-passenger flight was bought for; NULL for group items), `amount_cents`, `provider_booking_id`, `documents`, `raw` (redacted), `cancelled_at`; statuses `pending_confirmation`, `cancelling` (claimed by a cancellation in flight), `failed` next to `to_book`/`booked`. |

The checklist (`services/checklist.py`, moved out of the bookings router) is the bookings
ledger: `settle()` syncs it first and only ever UPDATEs rows by `(trip_id, item_key)`. Rows
holding a real reservation (provider ≠ self, or booked/pending_confirmation) are never deleted
or overwritten by a plan change — they are flagged stale. `trip_is_locked()` (active settlement
or a summary row not released/failed/expired) replaces the old payment-count guards on plan
edits; a traveler with a pending link may leave, an authorized one must release first, and a
departure while collecting restarts the round with a fresh lock.

The checklist and the group checkout never book the same item twice. A self row a traveler
recorded as booked (`provider = 'self'`, booked) is read into the trip context
(`self_booked`, item key → `users.name` of `booked_by`): the lock never prices, holds or charges
it, and re-verification drops one recorded after the lock. In the other direction,
`charged_item_keys()` (the items the trip's non-terminal round charges) makes
`POST /trips/{id}/bookings/{key}/confirm` answer 409 for them, and `GET /trips/{id}/bookings`
returns them as `charged_keys` (`[]` with no round) so the page offers no "Mark as booked".
A hotel booked ahead of capture and then cancelled by an unwind (`_unbook_hotel`, or the
reconciler's `cancel_hotel` step) goes back to the checklist with a note saying why
(`UNWOUND_HOTEL_NOTE`), never as a bare "Not booked yet".

### The lock (`settlements.lock`)

```json
{
  "verified_at": "…", "money_mode": "test", "fee_percent": 0, "group_size": 3,
  "member_ids": ["u1", "u2", "u3"], "needs_ack": false, "deadline_terms": ["2026-10-21T18:53:14+00:00"],
  "items": [
    {"item_key": "stay:Smart Hotel Roma", "category": "stay", "provider": "liteapi", "mode": "sandbox",
     "charge": true, "estimate": false, "total_cents": 48624, "per_person_cents": 16208, "currency": "USD",
     "refundable": true, "cancel_deadline": "2026-11-22T10:00:00+00:00", "pre_capture_safe": true,
     "taxes_included_cents": 4862, "due_at_property_cents": 10128, "attempt_ref": "<uuid4 = LiteAPI clientReference>",
     "ref": {"hotel_id": "lp36d84", "check_in": "…", "check_out": "…", "occupancies": [{"adults": 2}, {"adults": 2}],
             "identity": [{"name": "Smart Double Room", "board_type": "BI", "refundable_tag": "RFN", "policies": [...]}],
             "offer_id": "…", "prebook_id": "…", "rooms": 2}},
    {"item_key": "flight:New York", "category": "flight", "provider": "duffel", "mode": "test", "charge": true,
     "per_person_cents": 30600, "travelers": ["u1", "u2"], "attempt_ref": "<uuid4 = Duffel metadata.rovyn_attempt>",
     "ref": {"offer_id": "off_…", "offer_request_id": "orq_…", "passenger_ids": {"u1": "pas_…", "u2": "pas_…"},
             "signature": "ZZ123@2026-12-01T09:00|ZZ124@2026-12-06T12:00", "conditions": {…},
             "orders": [{"order_id": "ord_…", "booking_reference": "PNR…", "payment_required_by": "…",
                         "price_guarantee_expires_at": "…", "total_amount": "612.00", "paid": false}]}},
    {"item_key": "flight:Boston", "category": "flight", "provider": "self", "charge": false,
     "note": "No bookable fare for this flight yet — book it yourself", "travelers": ["u3"]},
    {"item_key": "estimate:meals_estimate", "category": "estimate", "charge": false, "per_person_cents": 25000}
  ],
  "members": {"u1": {"lines": [{"item_key": "…", "label": "…", "cents": 16208, "charged": true, "estimate": false}],
                     "charged_cents": 46808, "estimate_cents": 85308, "total_cents": 46808}}
}
```

## Endpoints — `backend/routers/payments.py` (`router` at `/trips`, `webhooks_router` at `/webhooks`)

| Endpoint | Who | What |
|---|---|---|
| `GET /trips/{id}/checkout` | member | the member-scoped read model: modes, my profile needs (missing fields), my check (charged vs estimate lines), items, every member's state, deadline, a derived timeline (never raw events), confirmations. No provider calls, no expiry side effects. `GET /trips/{id}/plan` carries a light `checkout` summary for the poll and `plan_locked` — `trip_is_locked()`, the rule every plan-edit endpoint enforces. |
| `POST /trips/{id}/checkout/lock` | member | verify-and-lock without paying, so the share is visible first. |
| `POST /trips/{id}/checkout/profile` | member | upsert traveler details (validated). |
| `POST /trips/{id}/checkout/session` | member | needs `approved`, no self-book row, a complete profile (names for everyone; airline fields for travelers on a Duffel item), the non-refundable acknowledgment when the lock needs it. Places pending airline holds, inserts the pending attempt (409 on a concurrent start), then creates the Stripe session; reuses an open session with > 10 min left; a completed-but-unconfirmed one is recorded as authorized on the spot. Simulated mode authorizes immediately. |
| `POST /trips/{id}/checkout/confirm` | member | looks the attempt up by session id (must be this trip and this caller), re-reads Stripe, records `authorized`. The hold the attempt already owns is never touched (the webhook may have recorded it first; the page may be reloaded); only a hold on an expired/failed/released attempt, outside a settling round, is cancelled as a stray. |
| `POST /trips/{id}/checkout/release` | member (own) | CAS authorized→releasing, cancel at Stripe, then released — only once Stripe confirmed; otherwise the row stays `releasing` for the reconciler. Only while collecting/reconfirm (409 otherwise). |
| `POST /trips/{id}/checkout/abandon` | leader | CAS to cancelled, every authorization released, holds cancelled, summaries dropped. |
| `POST /trips/{id}/checkout/retry` | leader | a new round (new settlement, fresh prices and holds) after `failed`. |
| `POST /trips/{id}/bookings/{key}/cancel` | leader, the row's payer, or any traveler on that flight (`can_cancel` in the read model) | claims the row (booked → `cancelling`, rowcount-checked) and persists a `cancel_booking` cursor BEFORE the supplier call, so a double click or a second canceller gets 409 and a crash is resumed by the reconciler (it reads the booking back); then cancels at the provider (a lost answer is read back, a refusal restores the row), persists the cancellation, plans the refunds from the ledger (floor of each traveler's share, remainder to the lowest user ids, fee pro rata, capped at captured − refunded − pending) and issues them; `CANCELLED_WITH_CHARGES` is recorded as a Rovyn loss event. |
| `POST /webhooks/stripe` | Stripe | signature-verified; routed by session/PI/attempt id against `payment_attempts`; a hold already recorded is a no-op; a hold on an expired/replaced link is cancelled as a stray; other events for non-current attempts are logged only; unroutable events are dead-lettered (200); handler failures return 500 so Stripe retries (the redelivery is processed, not deduped). |
| `POST /webhooks/liteapi` | LiteAPI | 401 without a configured token or on a mismatch (constant-time compare); the envelope's `sandbox` must match the settlement's money mode; its `request`/`response` JSON strings are parsed before the audit copy is redacted (holder, guests, phone, email never stored); the handler re-GETs `/bookings/{id}` with our key and acts only on that, for bookings we made. |
| `POST /trips/{id}/pay` | member | unchanged for `self_book` (409 while a settlement is active); `mode=full` → 409 pointing at the checkout. Once one traveler self-books, the group checkout is off trip-wide (`availability()`), so every other traveler locks in the same way; the trip is `booked` when all have. |

Deadlines are enforced on the write paths (`expire_if_due` in lock/session/confirm) and by the
reconciler's sweep of open rounds past their deadline, never from the polled read; the read
model carries `deadline_passed` so the UI stops showing a dead deadline as pending. The deadline
belongs to the open round only while somebody is committed: the last release (or a hold Stripe
drops) sets it back to NULL, and `expire_if_due` never fails a round nobody is committed to — a
deadline left behind is cleared and the round keeps its lock and any open checkout links. A
closed round keeps the deadline it closed with.

`POST /trips/{id}/checkout/profile` refuses (409) to blank a field the booking needs once the
traveler's share is in; `DELETE /trips/{id}` refuses (400) while any attempt is pending,
authorized, captured or releasing, otherwise closes an idle round, releases its holds and
cascades refunds → settlement_events → payment_attempts → settlements before the trip row
(`webhook_events` and `traveler_profiles` are not trip-scoped and stay); leaving a trip closes
the member's pending Checkout Session at Stripe through the same release path and rebuilds the
lock BEFORE the round moves to reconfirm (if the new group cannot be priced the round fails
cleanly rather than keeping a lock that lists the old members).

### Reconciler (`services/reconciler.py`)

One pass at boot (no staleness — nothing can still be running from the previous process) and
every 5 minutes: (1) open rounds past their deadline are released; (2) `releasing` attempts
Stripe never confirmed are finished by reading the PaymentIntent (canceled → released; still
held → cancel again with the same key; captured → a person); (3) stuck settlements — in-flight
phases quiet for 10 minutes, finished rounds with a cancellation/refund cursor, and
`needs_attention` rounds whose cursor carries `resume_to` — are resolved by READING the provider
(same client reference / order id / refund key), then resumed at `resume_to`. A round is
resumed at most 3 times (`MAX_RECONCILE_ATTEMPTS`); after that it waits for an operator. The
settle thread is non-daemon with an exception boundary (an unexpected error records an event
and parks the round) and FastAPI's shutdown joins live settlements for up to 130 s.

## Services

```
backend/services/payment_service.py   Stripe over urllib: form-encoding, Basic auth, Stripe-Version pin, caller-supplied
                                      Idempotency-Keys, retry with the same key, webhook signature verification,
                                      refuse_live_keys() allow-list, money_mode_now(), livemode assertions, simulated twins
backend/services/booking_service.py   lock (hotel rates by identity + prebook; fresh Duffel offer requests; holds),
                                      member checks, sessions/attempts/summaries, the resumable settle() state machine
                                      with a persisted cursor, refunds from the ledger, cancellation, reconcile(),
                                      the read models
backend/services/duffel_booking.py    Duffel client (its own helper, swappable for the shared one): offers, offer
                                      requests, hold/instant orders, balance payments, two-step cancellation
backend/services/checklist.py         checklist_from_plan / sync_checklist / trip_is_locked (shared, no router cycle)
backend/services/reconciler.py        startup + every 5 min sweep of settlements stuck > 10 min (ROVYN_RECONCILER=0 off)
backend/services/redact.py            redact() / request_hash() for every audit write
backend/scripts/settle_resolve.py     operator tool for needs_attention (below)
```

## Configuration

```
STRIPE_SECRET_KEY=            # sk_test_… enables Stripe test mode; unset = simulated authorizations
STRIPE_WEBHOOK_SECRET=        # whsec_… (stripe listen --forward-to localhost:8000/webhooks/stripe)
STRIPE_API_VERSION=           # Stripe-Version pin (default 2026-09-30.endive in payment_service.py)
APP_PUBLIC_URL=               # where Stripe sends travelers back (default http://localhost:5173)
CHECKOUT_WINDOW_DAYS=5        # group deadline after the first commitment — a card authorization or a $0 commit (≤ the 7-day card hold)
ROVYN_SERVICE_FEE_PERCENT=0   # optional, inside the first displayed total, never pre-checked
ROVYN_LIVE_MONEY=             # 1 only after docs/MOR-RUNBOOK.md is complete (allow-list guard otherwise)
LITEAPI_BOOK_MODE=off         # off | sandbox | live
LITEAPI_BOOK_API_KEY=         # sand_… for sandbox bookings (hidden test card, free); never falls back to the search key
LITEAPI_WEBHOOK_TOKEN=        # shared token of the LiteAPI dashboard webhook
DUFFEL_ACCESS_TOKEN=          # duffel_test_… holds and pays flights from the unlimited test balance
ROVYN_RECONCILER=             # 0 disables the background reconciler (tests)
ROVYN_SETTLE_INLINE=          # 1 runs settlements inline instead of on a thread (tests)
```

### Running it with real test keys

1. Stripe: `STRIPE_SECRET_KEY=sk_test_…`; run `stripe listen --forward-to localhost:8000/webhooks/stripe`
   and copy the printed `whsec_…` into `STRIPE_WEBHOOK_SECRET`; set `APP_PUBLIC_URL` to the frontend
   origin. Test cards: 4242 4242 4242 4242 (ok), 4000 0025 0000 3155 (3-D Secure), 4000 0000 0000 9995
   (insufficient funds). Without the CLI the return page's confirm call records the authorization.
2. LiteAPI: `LITEAPI_BOOK_MODE=sandbox` and `LITEAPI_BOOK_API_KEY=sand_…` (the same sandbox key as the
   search; every sandbox key carries a hidden test card). A full rates → prebook → book → get → cancel
   loop was run against the sandbox on 2026-10-18 and its responses are the fixtures in
   `tests/fixtures/liteapi_book_*.json`.
3. Duffel: `DUFFEL_ACCESS_TOKEN=duffel_test_…` — board flights need a Duffel hint from the options
   work; the JFK→EWR test route produces holdable orders, LHR→STN simulates a price change.

### Operator tool

`needs_attention` means the reconciler could not finish by reading the providers (it keeps
trying while the cursor carries `resume_to`, up to 3 times); no capture, cancel or refund is
ever issued on a guess. `.venv/bin/python -m backend.scripts.settle_resolve <trip_id>` prints
the settlement, its items, attempts, refunds and events; after reconciling money by hand against
the Stripe, LiteAPI and Duffel dashboards, `… --resolve` marks the round resolved so the group
can start a fresh checkout, or `… --resolve --to booked|partially_booked` when the bookings
stand and only the money needed a hand (so the trip's bookings can still be cancelled in-app).

## Frontend

`frontend/src/components/Checkout.jsx` (module-level component, mounted from the Costs &
payment section of `FinalPlan.jsx`, which calls `Money()` instead of mounting a closure
component so the checkout survives the 3 s poll). States: *not started* ("See my share at live
prices"), *traveler details*, *the check* (charged lines flagged live price / estimate /
provider, "what you'll pay on the spot" behind a toggle, cancellation deadlines and airline hold
times, the non-refundable acknowledgment), *awaiting others* (member pills, deadline, release),
*reconfirm* (previous vs new price), *transient* (timeline), *booked / partially booked*
(confirmations, cancel where `can_cancel`, receipt, Bookings tab), *failed* (retry for the
leader), *needs attention*. Simulated and test modes are labeled. Return from Stripe lands on
`/trips/{id}/final?tab=money&checkout=success&session_id=…`, which confirms and cleans the URL.
"I'll book everything myself" shows whenever no round exists — `self_book_chosen` is trip-wide
and never coexists with a round, so it must not hide the button. Once a traveler has self-booked
(`!available && self_book_chosen`) the checkout leads with that reason in place of "Pay in
Rovyn" and offers "The group is booking item by item — I'll book my part myself" as the primary
action; without it every later traveler had no way to lock in and the trip never reached booked.
The Bookings checklist shows Rovyn-made rows as "Booked by Rovyn" (pending hotel code when the
hotel has not issued one yet) and sends edits to the checkout; `confirm`/`unbook` answer 409
for them. Items in `charged_keys` show "Rovyn is booking this in the group checkout" instead of
"Mark as booked" and the seller link. The plan's edit controls (Request a change, day and
activity drag, ✕) follow `plan_locked` from the plan poll — falling back to "someone has paid"
only when an older backend does not send it — so they disappear exactly when the server would
refuse an edit (an authorized hold never reads as paid).

## Tests — `tests/test_payments_flow.py` (plain python; 222 checks)

A fake `urlopen` stands in for Stripe, both LiteAPI hosts and Duffel, records every call and
raises on anything unknown; every scenario asserts the unexpected list is empty. Covered: the
allow-list live-key guard; the webhook signature algorithm; the simulated end-to-end settlement
(paid semantics, plan lock, mutual exclusion with self-book, leave rules, estimates never
charged, cancellation refunds split from the ledger); the Stripe + LiteAPI + Duffel settlement
(session params, lock by identity, fresh offer request + hold, pending ≠ paid, member leaves →
reconfirm with the hold cancelled, return-page confirm, webhook confirm, duplicate webhook,
hotel before capture and flight after, idempotency keys, redacted raw payloads, late cancel
event for a captured PI, LiteAPI webhook token/sandbox/re-read rules, post-settlement hotel
cancellation with ledger refunds); price rise → reconfirm; capture refusal → unwind (hotel
cancelled, summaries gone) and leader retry; prebook drift re-quote; book timeout resolved by
the same client reference with `pending_confirmation`; EUR fare → self-book; payment timeout
resolved by reading the order (one payment call, one order); sub-$1 price change absorbed;
hard airline failure → refunds + `partially_booked`; short airline holds → self-book with the
hours; the reconciler finishing a settlement whose process died mid-capture; expired session on
return; abandon; self-book exclusivity; two same-name hotels in different towns; non-member 403;
`plan_locked` true from the first price lock (nobody paid) and false again after abandon; the
deadline cleared by the last release, kept for whoever is still committed, cleared by a hold
Stripe drops, started by a $0 commitment, and never used to fail an empty round (time moved past
the old deadline, and a stale one left in the row); the unwind note on a hotel cancelled by the
all-or-nothing undo and by the reconciler's `cancel_hotel` step; a stay a traveler booked by hand
never charged or overwritten, `charged_keys`, the 409 on recording a charged item, and a stay
recorded mid-round dropped at re-verify with every share captured without it; second and third
self-bookers accepted and the trip reaching booked.
`tests/test_maps_and_links.py` stays green; `npm run build` is clean; a Playwright pass on the
isolated side-port servers walks the simulated checkout for three travelers (desktop + phone)
through to the Bookings tab, plus release and leader abandon.

## Deviations from the first draft (all deliberate)

- A `POST /checkout/lock` step exists so travelers see live prices before authorizing (DOT
  full-fare / FTC: total first); the first session call still locks if nobody did.
- Airline holds are placed as soon as every passenger on that flight has their details, not
  strictly at lock time — the lock marks them `hold_pending` until then; a hold that fails after
  others paid restarts the round.
- A hold whose `payment_required_by − 2 h` would leave the group less than 12 hours from the
  lock is not taken: the flight becomes self-book with "this airline only holds fares for N
  hours".
- An explicit capture refusal after other travelers were captured unwinds (refunds + hotel
  cancellation) rather than parking in `needs_attention`, because the outcome is known; only
  unknown outcomes park.
- Simulated flights are simulated whether or not the option carried a Duffel hint, so the
  no-keys demo books every flight; with a Duffel token, flights without a hint are self-book.
- A traveler whose share is $0 commits as `nothing_due` (no card, no provider, in every money
  mode) and is marked captured at $0 when the group is charged, so the trip reads as fully paid.
- A hold whose creation call timed out is looked up by the attempt reference in the account's
  recent orders and adopted; when it is not found the flight is parked (`hold_pending`, "the
  airline did not answer") and retried on the next checkout — never self-booked on a guess.
- A pending hold is re-searched (same itinerary, fresh offer) when its offer expired before
  the hold could be placed; a per-person rise above $1 after others paid restarts the round.
- Flight shares are the higher equal split of the Duffel total (Rovyn may over-collect by up
  to n − 1 cents, never under); the item total stays share × n so the ledger check holds.
- `webhook_events` and `traveler_profiles` are not deleted with a trip: neither is trip-scoped,
  the former holds only redacted audit, the latter belongs to the user across trips.

## Boundaries

- **Always:** default simulated; allow-list credentials; verify webhook signatures/tokens;
  persist every provider payload redacted; show deltas, deadlines and non-refundable terms
  before consent; keep the self-book path untouched; USD everywhere; stdlib only; blank
  `DATABASE_URL`/`SUPABASE_URL` on test servers; never retry an order/book/payment call blindly.
- **Ask first:** new dependencies; charging for estimates; any fee default above 0; changing
  Phase A checklist semantics; storing anything beyond the traveler fields above.
- **Never:** card data or dates of birth in logs; capturing before everyone authorized; buying
  a non-refundable item before every capture succeeded; booking on a changed price without
  re-consent; a real card from a test; AI-generated numbers.
