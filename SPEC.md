# Spec: Phase B (Hotels) — Rooming + In-App Hotel Booking, Nuitée as Merchant of Record

*Scope decided 2026-07-08; money model revised twice the same day after two spec reviews.
Corrected 2026-07-09 against the code and a LiteAPI sandbox spike — three premises were wrong
(the test suite exists; `liteapi_hotel_id` was never lost; mixes cannot be price-ranked from one
rates call). Hotels only. Viator activities and the Rovyn service fee are follow-up specs. Ships
sandbox-first behind a flag. Parent architecture: `docs/BOOKING-ARCHITECTURE.md` §2 (Phase B),
§3, §4, §5.*

**This spec contains two independently shippable parts.** Part 1 (rooming) is a prerequisite
for Part 2 (booking) — the solver's room mix defines the `occupancies` payload every rates
call depends on. Land and verify Part 1 first; it is valuable on its own even with
`LITEAPI_BOOK_MODE=off`.

## Objective

Let a group decide **who sleeps where**, then book their chosen hotel **inside Rovyn**
instead of linking out. LiteAPI's native multi-room flow books the entire stay as **one
reservation**: one `rates` call carrying one occupancy per room, one prebook, one payment,
one `hotelConfirmationCode`. Nuitée is the merchant of record — their payment SDK collects
one member's card in our UI, they hold the travel money, Rovyn earns a **10%** `margin`
commission. The trip's check then **auto-computes each traveler's equal share**, so the group
still splits the stay evenly even though one card was swiped. The Phase A checklist stays the
permanent opt-out — "book it myself" must keep working unchanged.

**User story:** on an approved trip, the group picks roommates; Rovyn proposes a rooming
board (room cards, occupant avatars, empty spots) which travelers adjust by tapping into a
free spot. Any member opens the Bookings tab, taps **Book in Rovyn** on the stay, reviews the
locked price + cancellation policy, pays through the embedded payment SDK, and the stay flips
to *booked* with the hotel's front-desk confirmation code — visible to the whole group. Every
member's check shows their equal share and who they owe. The payer can cancel in-app.

**Success looks like:** a complete sandbox booking of a multi-room stay (solver → rooming →
offer → prebook → pay → `hotelConfirmationCode` → cancel), with the flag off producing
byte-identical behavior to today.

### What this spec deliberately does *not* build

The full product vision — *every member's card is charged their share in one transaction, and
Rovyn allocates the money across flights, hotels, events and visits* — is **Phase C**. Pooling
members' money in a Rovyn-controlled account makes Rovyn the merchant of record, requiring a
legal entity, Stripe travel-category underwriting, seller-of-travel registrations (CA, FL, HI,
WA), and an EU/UK geo-guard for the Package Travel Directive. **None exist as of 2026-07-08,
so no code path in Phase B may pool funds.** Phase B's honest approximation: one member fronts
the card, and the check makes the split equal and visible.

*Newly relevant:* Rovyn's US-based operation with an **American co-founder** makes Phase C far
more tractable than the Brazilian route assessed in `BOOKING-ARCHITECTURE.md` §2 — a Delaware
entity (Stripe Atlas) sidesteps the CNPJ + CADASTUR problem entirely, and Stripe US requires
exactly the US person the team has. Starting entity formation in parallel with this build is
strongly recommended; nothing in Part 1 or Part 2 depends on it, and Phase C swaps only the
payment source.

Permanently out of reach, never to be promised in the UI: **Airbnb** (ToS ban automated booking;
automating it risks *the traveler's own* account) and **events/Ticketmaster** (link-out only).
**Flights** stay link-out in Phase B — they need Duffel KYC and a funded balance.

## Assumptions (decided in spec review — flag if wrong)

1. **One booking, one payer, equal check.** The stay books as a single LiteAPI reservation.
   Whoever taps Book pays Nuitée the full total; the check auto-computes what every member
   owes them. Rovyn never holds travel funds.
2. **Room mix comes from the solver**, not from Phase A's `⌈group/2⌉` heuristic. The heuristic
   survives only as the pre-solver estimate on the board.
3. **Split is always `equal`** (architecture §5: "matches Rovyn's promise"). `by_room` is **cut**:
   LiteAPI returns one `offerRetailRate` for the whole `occupancies` array and never a per-room
   price (verified against the sandbox 2026-07-09), so each traveler's room share could only be
   invented — and inventing a number attached to money is on the Never list.
4. **Currency stays USD** (hardcoded in Phase A rates calls today).
5. **Margin = 10%**, configured on the LiteAPI dashboard, not per-request. It is baked into the
   rate LiteAPI returns and displayed inside the first shown total — never added as a separate
   line at checkout (DOT full-fare rule + the Hopper FTC lesson).
6. **Non-refundable rates are warned, not blocked** (decided). The warning must be unmissable
   and must state that cancelling returns nothing.
7. **Only the payer may cancel** (decided). A non-refundable cancellation can then only ever
   harm the member who chose it. The leader has no override.
8. Multi-city trips book each city's stay as an independent checklist item with its own rooming.

## Part 1 — Rooming (architecture §5)

Ships first, works with `LITEAPI_BOOK_MODE=off`.

**Step 1 — Room mix (solver + leader choice).** A hotel's real room capacities come from
`GET /data/hotel` → `rooms[].maxOccupancy` (verified 2026-07-09: 9 rooms, capacities `[1,2,3,4]`
on a sandbox property). That call is **already made** by `_liteapi_stay_page`; only the parse is
new. A **pure, deterministic solver** enumerates viable mixes for group size N over those
capacities (10 → 5×double, or 2×triple + 2×double, …) and ranks them **structurally** — fewest
wasted beds, then fewest rooms, then a size-signature tiebreak.

**Prices are not in that response.** `offerRetailRate.amount` is the total for the *entire*
`occupancies` array (`hotel_service.py:209`, `:569`), so every candidate mix costs its **own
rates call**. The top 3 mixes are priced concurrently; the leader picks from those 3; a mix that
fails to price is shown unpriced rather than hidden. Choosing a mix re-prices the plan.

**Step 2 — Roommate preferences.** Shown only when the stay is room-based and rooms < group
size. Each traveler multi-selects preferred roommates. **Mutual picks are must-be-together pairs.**

**Step 3 — Auto-proposal.** Deterministic matching: satisfy mutual pairs first, then one-way
preferences greedily, then fill remaining spots. **Never split a mutual pair.** Unsatisfiable
preferences are flagged to the leader ("Ana and Bia both picked Carla").

**Step 4 — Rooming board.** Room cards with occupant avatars and empty spots. Travelers tap or
drag themselves into any free spot; the leader may move anyone. This is the one place in the
booking feature where **optimistic UI is correct** — rooming is not money. Filled active states,
minimal wording, detail behind taps.

**Step 5 — Locks and approval.** Rooming is part of the final plan: any change resets approvals
(existing rule); after payment it locks (existing rule).

**Data.** The mix and assignments live in the existing `final_plan` JSON, keyed per stay so
multi-city works. The key is `stay.city or stay.name` — the same stay identity `_detach_heavy`
and `_reattach_heavy` already use. Roommate **preferences** get their own table (see below).

```json
plan.rooming = {
  "<stay_key>": {
    "split_mode": "equal",
    "mix": {"hotel_id": "lp1c4b4",
            "rooms": [{"sleeps": 3, "count": 2}, {"sleeps": 2, "count": 2}],
            "room_count": 4, "capacity": 10,
            "total_price": 1171.18, "currency": "USD", "priced_at": "…"},
    "assignments": [["u1","u2","u3"], ["u4","u5","u6"], ["u7","u8"], ["u9","u10"]]
  }
}
```

`assignments` is indexed by room, expanded from `mix.rooms` largest-first. `total_price` is
null for AI/sample stays (no `liteapi_hotel_id`) — beds are still assigned, just unpriced.

**Preferences are a new table, not plan JSON:**
`roommate_prefs(trip_id, user_id, target_id, created_at)`, one row per directed pick. A mutual
pair is both edges existing, computed at read time. It cannot live in the plan: `revise_final_plan`
rewrites the whole blob through an LLM, every edit endpoint does `UPDATE trips SET final_plan = ?`
wholesale, and each traveler toggles their own picks concurrently. Every other per-user signal
here — `votes`, `stars`, `approvals`, `category_progress` — is already a thin join table.

## Part 2 — Booking, cancellation, settlement

### Flow (LiteAPI native multi-room — verified against the sandbox 2026-07-09)

**One `offerId` spans every room — confirmed empirically.** A rates call with
`occupancies=[{adults:3},{adults:2}]` returns each `roomTypes[]` entry carrying a single
**string** `offerId`, one `offerRetailRate` for the pair, and **two** `rates[]` entries —
`occupancyNumber: 1` (`maxOccupancy: 3`, "Comfort Triple") and `occupancyNumber: 2`
(`maxOccupancy: 2`, "Small Double French Bed"). All 200 room types in the response had one rate
per requested occupancy. The Node SDK's array example was misleading; the API reference's
"string" is right. **One prebook, one payment, all rooms** — the one-booking model stands.

```
GET  /trips/{id}/bookings/{item_key}/offers
       → rates search with occupancies derived from plan.rooming.mix,
         e.g. [{adults:3},{adults:3},{adults:2},{adults:2}]
         Each returned room carries occupancyRefId linking it to its occupancy.
         Offers show total, board, refundable flag + cancellation deadline, verified-at stamp.

POST /trips/{id}/bookings/{item_key}/prebook   {offer_id}
       → LiteAPI POST /rates/prebook  {offerId, usePaymentSdk: true}
         Locks price + policy. Returns prebookId, secretKey, transactionId,
         and any delta vs the plan's estimate (highlighted re-consent if changed).

   [frontend: payment SDK collects the card; Nuitée charges the payer]
     script: https://payment-wrapper.liteapi.travel/dist/liteAPIPayment.js
     publicKey MUST match the booking key's environment; secretKey from prebook;
     returnURL points at the book call. Sandbox test card: 4242 4242 4242 4242.

POST /trips/{id}/bookings/{item_key}/book  {prebook_id, transaction_id, guests[]}
       → LiteAPI POST /rates/book, payment {method: TRANSACTION_ID, transactionId}
         Guests carry occupancyRefId from plan.rooming.assignments.
         Returns bookingId + hotelConfirmationCode.

POST /trips/{id}/bookings/{item_key}/cancel        (payer only)
       → LiteAPI PUT /bookings/{bookingId}
         Refund, if any, is issued by Nuitée to the payer's card. Rovyn never moves money.
```

**Booking is atomic.** One reservation covers every room, so there is no partial-booking state,
no per-room claim arbitration, and no unwinding problem. A failed or abandoned SDK payment
leaves the item `to_book` with the link-out fully usable. This is the single biggest reason the
one-booking model was chosen over per-room payers.

**Cancellation** sets `status='cancelled'`, restores the checklist item to `to_book`, re-enables
the link-out, and emails the group. The confirm dialog states the refund amount, or — for a
non-refundable rate — states plainly that nothing will be returned.

### Settlement (auto-computed, read-only)

Derived at render time from `bookings.amount_cents` and `bookings.payer_id`: `amount / group size`
per member. The check renders "Bruno owes Ana $210." There is no `by_room` mode — see assumption 3.

The existing `payments` table is **not modified** — §5 notes the check already supports
per-traveler amounts because flights differ by departure city. Settlement is a derivation, not
new state.

### Data changes

Six columns on the existing `bookings` table — no new tables:

```
payer_id            TEXT REFERENCES users(id)   -- who paid Nuitée
amount_cents        INTEGER                     -- what they actually paid
provider_booking_id TEXT                        -- LiteAPI bookingId, for cancel/status
documents           TEXT                        -- JSON: voucher/confirmation artifacts
raw                 TEXT                        -- provider payload snapshot, audit
cancelled_at        TEXT
```

`status` gains the value `cancelled` (no new column). Applied twice, per the schema-ownership
rule: SQLite self-migration tuples in `backend/db.py` **and** a new
`supabase/migrations/…_add_booking_provider_columns.sql` (+ canary column entry so Postgres boot
fails loudly if unpushed).

**Plan plumbing — there is no gap.** Corrected 2026-07-09: `liteapi_hotel_id` *does* survive the
weave. `select_winners` copies the whole option through (`ai_service.py:1361`, `:1376`) into
`plan["stay"]`/`plan["stays"]`, and `bookings.py:93` snapshots the entire stay dict into
`bookings.details`, handed back parsed at `:197`. Nothing needs threading.

**The real hazard is the AI revision path, and it is a live bug.** `_detach_heavy`
(`ai_service.py:1841`) strips only `image`, `links`, `link`, `booking_links` and
`official_website` before serializing the plan into an LLM prompt. `liteapi_hotel_id` goes to the
model and whatever returns is trusted; once `plan.rooming` exists, roommate **user ids** would
round-trip through a language model too. Worse, `_reattach_heavy`'s guard `if … and key not in
target` (`:1889`) restores the original *only when the model didn't invent one* — so a
hallucinated value **wins**. This breaks this spec's own Never rule. Part 1 must: strip `rooming`
wholesale, protect `liteapi_hotel_id`, and make reattach overwrite unconditionally.

Stays without a `liteapi_hotel_id` (AI/sample stays) get an unpriced rooming board and no Book
button, and otherwise behave exactly as Phase A.

## Configuration & the flag

```
LITEAPI_API_KEY       (exists) — search/rates/details; unchanged
LITEAPI_BOOK_MODE     (new)    — off | sandbox | live; default off
LITEAPI_BOOK_API_KEY  (new)    — sandbox key (sand_…) or production key for booking
```

- `off` (default): no behavior change to booking. Part 1's rooming board still works. All Part 2
  endpoints 404/400 cleanly; the UI shows no Book button.
- `sandbox`: full flow against LiteAPI sandbox + SDK test cards. **Reachable today.**
- `live`: same code, production key. **Blocked until a legal entity exists.**
- Offer IDs are environment-bound: the booking flow does its **own** rates search with
  `LITEAPI_BOOK_API_KEY` — it must never mix offers from the display key's search. The payment
  SDK's `publicKey` must match that same environment.

### Enabling `live` mode later (answers spec-review Q4)

Production booking is **not a self-serve toggle**. As of 2026-07-08 the production key does not
have booking enabled. In order:

1. **Form a US legal entity.** Required first — LiteAPI's production booking contract and the
   margin payout are business-to-business. With an American co-founder, Stripe Atlas (Delaware
   C-corp or LLC) is the standard route and avoids Brazil's CNPJ + CADASTUR requirement entirely.
2. **Complete the LiteAPI/Nuitée company profile** (entity details, bank account for margin
   payouts, merchant agreement).
3. **Request booking + payment SDK ("live wallet") enablement** on the production key from your
   LiteAPI account manager or support. This is a request, not a switch.
4. **Verify with a full sandbox booking first**, including a cancellation.
5. Set `LITEAPI_BOOK_API_KEY` to the production key and `LITEAPI_BOOK_MODE=live`.

*Steps 2–3 reflect LiteAPI's process as understood on 2026-07-08; confirm exact dashboard paths
against their docs and your account manager. Nothing here is verifiable from inside this repo.
Note that Phase B needs no Stripe account — there is no Rovyn service fee this phase.*

## Tech Stack

- Backend: FastAPI (Python 3.12), stdlib-only HTTP (`urllib`, no `requests`/SDK), dual-mode DB
  (`backend/db.py`: SQLite locally, Postgres/Supabase when `DATABASE_URL` is set — same
  `?`-placeholder SQL on both).
- Frontend: React + Vite, `framer-motion`, `lucide-react`, editorial design system in
  `src/index.css`.
- Provider: LiteAPI v3.0 (`https://api.liteapi.travel/v3.0`) — already integrated for
  search/rates/details in `backend/services/hotel_service.py`.
- New external piece: **LiteAPI payment SDK** (their JS, loaded in our booking UI; card data
  never touches our server). Do **not** adopt their Node/Python SDK — stdlib is the house rule.
- **No Stripe.**

## Commands

```
Backend dev:    .venv/bin/uvicorn backend.main:app --port 8000      (repo root)
Frontend dev:   cd frontend && npm run dev                          (localhost:5173)
Frontend build: cd frontend && npm run build
Tests:          .venv/bin/python tests/test_maps_and_links.py       (exists — the harness)
                .venv/bin/python tests/test_rooming.py              (new, Part 1)
                .venv/bin/python tests/test_plan_revision.py        (new, Part 1)
                .venv/bin/python tests/test_rooming_pricing.py      (new, Part 1)
                .venv/bin/python tests/test_rooming_api.py          (new, Part 1)
                .venv/bin/python tests/test_booking_flow.py         (new, Part 2)
                plain python, no pytest.
E2E verify:     /verify skill (isolated side-port launch, seeded user, Playwright;
                observes the .env-trap rules — blank DATABASE_URL/SUPABASE_URL)
Lint:           none configured — match surrounding style
```

## Project Structure

```
backend/services/rooming_service.py   (new, Part 1) pure mix solver + deterministic
                                      roommate matching — no I/O, fully unit-testable
backend/services/booking_service.py   (new, Part 2) LiteAPI prebook/book/cancel, mode/flag
                                      logic, offer normalization, occupancies from the mix,
                                      settlement derivation — mirrors hotel_service.py
backend/routers/rooming.py            (new, Part 1) rooming read, prefs, occupant move, mix choice
backend/routers/bookings.py           offers/prebook/book/cancel endpoints join the existing
                                      checklist router
backend/services/ai_service.py        harden _detach_heavy/_reattach_heavy against the LLM
backend/db.py                         roommate_prefs table (Part 1); six new bookings columns
                                      + self-migration tuples (Part 2)
supabase/migrations/                  matching Postgres migration (+ canary for new columns)
frontend/src/components/RoomingBoard.jsx  (new, Part 1) the board
frontend/src/pages/FinalPlan.jsx      Rooms tab; Bookings tab: Book-in-Rovyn sheet
                                      (offers → review → pay → confirmed → cancel)
frontend/src/api/                     new client calls
tests/fixtures/                       (new) real sandbox /data/hotel + /hotels/rates responses
tests/test_rooming.py                 (new) solver + matching, pure functions
tests/test_rooming_pricing.py         (new) capacities + per-mix pricing
tests/test_rooming_api.py             (new) endpoints, permissions, approval/payment locks
tests/test_plan_revision.py           (new) LLM cannot corrupt rooming or hotel ids
tests/test_booking_flow.py            (new) in-process, fake urllib LiteAPI layer
tests/test_maps_and_links.py          existing — the harness these copy; must stay green
docs/BOOKING-ARCHITECTURE.md          parent doc — update Phase B row when shipped
```

## Code Style

Follow the established idiom — stdlib HTTP, best-effort degradation, docstrings that explain
product rationale, dual-mode SQL. Real example from `hotel_service.py`:

```python
def refresh_room_offers(option: dict, context: dict) -> dict | None:
    """Fresh room offers for an already-cached LiteAPI stay — prices are the
    one part of a detail page that must never be served stale. None when the
    stay isn't a LiteAPI property or the fetch fails."""
    hotel_id = option.get("liteapi_hotel_id")
    if not (hotel_id and liteapi_available()):
        return None
    try:
        return _liteapi_room_offers(...)
    except Exception:
        return None
```

Frontend: minimal wording, verbose detail behind taps, swipe/tap-first interactions, filled
active states. **The rooming board is optimistic; money states never are** — the stay shows
`booked` only after LiteAPI confirms, and `cancelled` only after the cancel call returns.

## Testing Strategy

**Part 1 — `tests/test_rooming.py`** (pure functions, no I/O):
- solver enumerates the known mixes for N = 4, 5, 7, 10; ranks by wasted beds, then fewest rooms
- solver is deterministic: same input ⇒ same output, always
- mutual pairs are never split, across every matching scenario
- one-way preferences applied greedily after mutual pairs
- unsatisfiable preferences surface a leader-facing conflict, not a silent drop

**Part 1 — `tests/test_rooming_pricing.py`, `tests/test_rooming_api.py`, `tests/test_plan_revision.py`:**
- capacities parse from `/data/hotel` `rooms[]`; each mix's rates payload carries *its own*
  `occupancies`, not `[{"adults": 2}] * n`
- no `LITEAPI_API_KEY` ⇒ structural candidates, zero LiteAPI calls, no exception
- changing the **mix** resets approvals; moving an **occupant** does not; both lock after payment
- roommate preferences survive a plan revision (they are not in the plan JSON)
- an LLM revision cannot rewrite `plan.rooming` or `liteapi_hotel_id`

**Part 2 — `tests/test_booking_flow.py`** (follows `tests/test_maps_and_links.py`: in-process,
fake `urllib` layer, temp SQLite):
- flag off ⇒ endpoints refuse, no Book button, checklist unchanged
- `occupancies` payload is built from `plan.rooming.mix`, not the `⌈group/2⌉` heuristic
- guests are sent with the correct `occupancyRefId` from `plan.rooming.assignments`
- one book call ⇒ one booking row, one `hotelConfirmationCode`, `provider='liteapi'`
- prebook price-delta detection vs the plan estimate
- non-refundable offer requires explicit consent before prebook
- SDK payment failure leaves the item `to_book`; link-out intact
- **cancel: payer succeeds; a non-payer (incl. the leader) is refused**
- cancel sets `status='cancelled'`, restores `to_book`, emails the group
- settlement math: the equal split produces correct per-member balances
- env-bound key separation (booking calls use the booking key only; SDK `publicKey` matches)

**Sandbox e2e** (manual + `/verify` Playwright where possible): one full multi-room sandbox
booking per release touching this flow, including a cancellation; verify a second group member
sees the confirmation code and their share on the check.

**Regression:** `tests/test_maps_and_links.py` is the existing net and must stay green — it is
also the harness every new test copies (env pinned before backend import, temp SQLite, a
`fake_urlopen` that routes by URL and raises `AssertionError` on any unstubbed call, a hand-rolled
`ok()`). Extend its LiteAPI routes from `tests/fixtures/`. Coverage is thin, so `npm run build`
plus a `/verify` pass over the Phase A checklist (mark booked, unbook, reconcile) still carries
real weight.

## Boundaries

- **Always:** default `LITEAPI_BOOK_MODE=off`; degrade every failure to the link-out self-book
  flow; keep card data SDK-only (never on our server or logs); snapshot the provider payload into
  `raw`; surface the cancellation deadline **before** payment; show live provider data verbatim
  with a verified-at timestamp (model proposes, providers dispose); run tests + frontend build
  before commit; blank `DATABASE_URL`/`SUPABASE_URL` on any test server.
- **Ask first:** switching `live` mode on; any new pip/npm dependency (stdlib is the house rule —
  includes LiteAPI's official Node/Python SDK); schema changes beyond the six `bookings` columns;
  changing Phase A checklist semantics; changing the 10% margin; giving the leader a cancel
  override.
- **Never:** pool travelers' money in a Rovyn-controlled account or otherwise act as merchant of
  record (that is Phase C, gated on a legal entity); commit API keys; charge a real card from a
  test; point tests at the real Supabase DB; AI-generated numbers anywhere in the booking flow;
  pre-checked fees or totals that exclude mandatory charges (DOT full-fare rule + Hopper/FTC
  lesson); split a mutual roommate pair; any Airbnb booking automation.

## Success Criteria

1. `LITEAPI_BOOK_MODE=off` (or unset): booking behavior is identical to today — `tests/test_maps_and_links.py`
   stays green, plus a `/verify` pass over the Phase A checklist and a no-Book-button UI check.
   The rooming board still works.
2. For a 7-person trip, the solver proposes a valid mix; mutual pairs stay together; the leader
   reassigns via the board; changing the **mix** resets approvals, moving an occupant does not.
3. In sandbox mode, a seeded trip completes offer → prebook → SDK test payment → book. The
   `bookings` row has `status='booked'`, `provider='liteapi'`, non-empty `confirmation`
   (hotelConfirmationCode), `provider_booking_id`, `documents`, `raw`, `payer_id`, `amount_cents`.
   The payer gets a confirmation email; every member sees the code.
4. Every member's check shows an equal share and who they owe the payer.
5. A price change between offer and prebook is shown as an explicit delta requiring re-consent —
   never silently absorbed. A non-refundable rate requires unmissable explicit consent.
6. The payer cancels in-app; a non-payer cannot. The item returns to `to_book`, the link-out
   works, and the group is emailed.
7. Abandoning payment leaves the item `to_book` and the link-out fully usable.
8. Stays without `liteapi_hotel_id` show no rooming step and no Book button.
9. New tests pass via plain python and `tests/test_maps_and_links.py` stays green;
   `npm run build` stays clean.
10. An AI plan revision cannot alter `plan.rooming` or a stay's `liteapi_hotel_id`.

## Resolved (2026-07-09 sandbox spike)

1. ~~Does one `offerId` span all occupancies?~~ **Yes.** One string `offerId` per room type, one
   `offerRetailRate` for the whole array, one `rates[]` entry per occupancy carrying
   `occupancyNumber` + `maxOccupancy`. One prebook, one payment, all rooms.
4. ~~Solver tie-breaks — show runner-ups?~~ **Yes, top 3, priced.** Price ranking is impossible
   from one call, so the solver ranks structurally and the board prices the top 3 for the leader.
5. ~~Should Part 1 ship as its own PR?~~ **Yes.** Plan in `tasks/plan.md`, branch `feature/rooming`.

## Open Questions

2. **Does `PUT /bookings/{bookingId}` return the refund amount**, or must the cancellation policy
   be read back from `GET /bookings/{bookingId}`? The confirm dialog needs a number.
3. **Cancelling after approval:** does restoring the item to `to_book` reset plan approvals and
   unlock rooming, or leave the plan locked? §5 step 4 says rooming locks after payment; it does
   not say what an in-app cancel does.
6. **Margin.** The sandbox returns `suggestedSellingPrice` ≈ 1.15 × `offerRetailRate`. If that
   ratio is LiteAPI's dashboard margin setting, the 10% in assumption 5 is configured somewhere we
   haven't looked. Confirm before Part 2 displays any total.
