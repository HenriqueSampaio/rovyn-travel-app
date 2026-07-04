# Rovyn — Payment & Auto-Booking Architecture

*Researched July 2026 (6 research tracks, 16 load-bearing claims adversarially verified
against primary sources — all confirmed). This document answers: can Rovyn really
auto-book a whole approved trip, how does the money flow, how are bookings confirmed,
how do we guarantee accuracy, and how do we allocate hotel rooms in groups.*

---

## 0. The answer in one paragraph

**Yes, auto-booking is possible — without Rovyn becoming an accredited travel agency —
because a new generation of aggregators lets startups sell under *their* accreditation.**
Flights: **Duffel** (self-serve, sells under Duffel's IATA/ARC accreditation, $3 + 1% per
order, returns real airline PNRs + e-ticket numbers). Hotels: **LiteAPI/Nuitée**
(self-serve, free booking API, returns the hotel's own front-desk confirmation code).
Activities: **Viator** (in-app booking with Viator as merchant of record, ~8% commission,
returns vouchers). **Airbnb is a hard no** — verified: no demand API exists and their ToS
ban automated booking even on the user's own account — so Airbnb stays a first-class
"book it myself" flow. **Events (Ticketmaster etc.) are also link-out only.** The money
flow that matches Rovyn's vision (each traveler pays their share → app books everything)
makes Rovyn the **merchant of record**, which triggers real obligations (legal entity,
Stripe travel-category approval, seller-of-travel registrations, EU/UK rules) — so we
get there in **three phases**, each shippable and each honest.

---

## 1. Who will actually let us book (verified July 2026)

### Flights
| Option | Verdict | Facts |
|---|---|---|
| **Duffel** | ✅ **The path** | Self-serve signup, pay-as-you-go ($3.00/order + 1% managed content, 2% FX, no monthly fees). Sells under Duffel's own IATA/ARC accreditation (US, UK, IE, FR, AU) — "issue tickets from day 1", no financial guarantees. Live mode gated by KYC review. Bookings funded from a **prepaid Duffel Balance**; you collect traveler money with your own Stripe. Returns `booking_reference` (airline PNR) + e-ticket numbers per passenger. **Hold orders** lock a fare (`price_guarantee_expires_at`) and refuse stale-price payment — purpose-built for collect-then-book. |
| Amadeus Self-Service | ❌ Dead | Portal **decommissioned July 17, 2026** (new registrations paused since March 2026). Booking never worked for unaccredited startups anyway (consolidator agreement required). |
| Kiwi Tequila | ❌ Closed | Invite-only since May 30, 2024. |
| Travelfusion / GDSs | ❌ | Sales-negotiated enterprise contracts. |
| Travelpayouts (affiliate) | ✅ fallback | Deep-link monetization of what Rovyn does today (~1.1–1.3% of ticket). No booking, no PNR. |

### Stays
| Option | Verdict | Facts |
|---|---|---|
| **LiteAPI (Nuitée)** | ✅ **The path** | Self-serve, free signup, **booking endpoints are free** (look-to-book ratio applies). 3M+ properties incl. apartments/villas. Two modes: (a) **Nuitée as merchant** — their payment SDK collects the traveler's card in our UI, we set a `margin` % as our commission, weekly payouts; (b) **we as merchant** — net rates, we collect via Stripe and pay from a funded wallet. Booking returns `hotelConfirmationCode` — the hotel's own reference, honored at the front desk. Full hotel identity pre-booking (name, address, photos, rating). |
| **Duffel Stays** | ✅ strong 2nd | Same account as flights; commission profit-share (split unpublished); prepaid Balance; returns front-desk `reference`. |
| Expedia Rapid | 🔶 later | Case-by-case application ("Startup" is an accepted business type but turnover is asked). Prize: 900k+ vacation rentals incl. 650k **Vrbo** homes (extra approval; virtual-card payment required — Stripe Issuing). This is the eventual Airbnb-alternative endgame. |
| Booking.com Demand API | ❌ | Managed-partner contract only; Booking.com culled small affiliates in June 2025. |
| RateHawk, Hotelbeds | ❌ for now | Require a registered company + signed agreement / certification. Revisit at volume. |

### Activities & events
| Option | Verdict | Facts |
|---|---|---|
| **Viator** | ✅ **The path** | Tiered: Basic (instant, links out) → Full + **Booking Access** (application + certification, no traffic minimum): booking happens **inside Rovyn** but **Viator stays merchant of record** — traveler pays via Viator's payment iframe, Viator handles refunds/support, we earn ~8%. Returns `voucherURL` (some products require email-only voucher delivery). Separate **Merchant tier** (we become MoR, set our own prices; refundable deposit + screening) for later. |
| Tiqets | ✅ growth path | Self-serve content/availability APIs, **Booking API at ~200 orders/month** — merchant checkout (we charge, Tiqets confirms + issues mobile tickets/barcodes). Best route to *attraction tickets* as MoR. |
| GetYourGuide | 🔶 links only | Booking API needs ~1M visits + 300 bookings/mo. Affiliate (~8%) for now. |
| **Ticketmaster / events** | ❌ **link-out only** | Discovery API is data-only (free, 5k calls/day) — use it for *accurate event data*. Purchase APIs are restricted to existing distribution partners; Eventbrite likewise. Affiliate link-out to the official seller is the only honest play. |

### Airbnb — the definitive no
- No public API; the partner API is **supply-side only** (for property managers) and closed to unsolicited applicants.
- API ToS (Oct 2025) explicitly prohibit products that "facilitate, accept, or process payments in relation to the Airbnb Platform".
- Consumer ToS §11.1 (Feb 2026) ban "bots… or other automated means to… interact with the Airbnb Platform" — **automating a booking on the user's own account risks the *user's* account**, not just ours. Never build this.
- What we do instead: (1) first-class **self-book flow** (below); (2) offer *bookable* villas/aparthotels via LiteAPI as in-app alternatives; (3) later, Vrbo via Expedia Rapid.

---

## 2. Money flow (Point 1)

### The three-phase plan

**Phase A — "Verified plan + booking checklist" (ship now; no company needed)**
Rovyn never touches travel money. The final plan becomes a **booking checklist**: every
item deep-links to its seller (monetized: Travelpayouts flights, Viator/GYG affiliate
~8%, Expedia affiliate). Travelers mark items booked and paste/forward confirmations
(see §3). This *is* the "Don't autobook, I'll do it myself" mode — we build it first
because it must exist forever as the opt-out, and it works for Airbnb and events where
autobooking will never exist.

**Phase B — In-app booking, suppliers hold the money (needs entity + Stripe, minimal
travel regulation)**
Hotels become bookable inside Rovyn with **Nuitée as merchant of record** (their payment
SDK inside our UI; our commission = `margin` param). Activities become bookable with
**Viator as MoR** (their payment session inside our flow). Each traveler still pays
*their own share* — but pays the supplier directly, per item; Rovyn's Stripe charges only
**Rovyn's service fee**. Because suppliers hold the travel funds, we avoid most
seller-of-travel/insolvency obligations while delivering "book in the app". Flights stay
link-out in this phase.

**Phase C — Full auto-book, Rovyn as merchant of record (the vision)**
Each traveler pays their full share to Rovyn (Stripe), Rovyn books everything:

```
group approves final plan
        │
        ▼
[VERIFY & LOCK]  re-quote every item live:
  Duffel hold order (fare locked via price_guarantee_expires_at)
  LiteAPI prebook (rate + cancellation policy locked)
  Viator /bookings/cart/hold (price + availability locked)
        │  Review screen: live prices, flight numbers, policies, any deltas vs estimate
        ▼
[COLLECT]  Stripe PaymentIntent per traveler:
  capture_method=manual + request_extended_authorization
  (travel qualifies for 30-day extended auth on Visa/MC — fits group coordination)
        │  all authorized? ──no──► release all auths, notify group
        ▼ yes
[CAPTURE + BOOK]  capture all → pay Duffel order from Balance,
  LiteAPI book (net rates, wallet), Viator book
        │  any booking fails? → auto-refund that item's share to every traveler + notify
        ▼
[CONFIRM]  per-item confirmations (§3) + receipt emails
```

Requirements before Phase C goes live (this is the honest checklist):
1. **Legal entity.** Stripe US needs a US person/entity (Stripe Atlas is the standard
   route); Stripe Brazil supports Pessoa Física but **operating a travel agency in
   Brazil requires a CNPJ + free CADASTUR registration** (individuals can't register).
2. **Stripe travel approval.** "Travel reservation services" is on Stripe's *restricted*
   list — expect underwriting and possibly a rolling reserve (5–10% is industry norm).
   Stripe **Connect is not needed** — we sell our own product and pay suppliers ourselves.
3. **Seller-of-travel registrations** as US marketing expands: California ($100/yr +
   bond/trust + foreign-entity registration), Florida ($300/yr + bond), Hawaii, and
   Washington — where the trust/bond duty triggers only if we **hold consumer funds >5
   business days** (an architecture lever: capture late, book fast).
4. **Geo-guard EU/UK initially.** Selling flight+hotel at a total price to EU travelers
   makes us a Package Travel Directive "organiser" (mandatory insolvency protection —
   explicitly applies to non-EU websites; the PTD was just tightened in April 2026).
   UK flight sales need **ATOL** (~£1,408 + £2.50/pax small-business tier). Add these
   markets deliberately, later.

### The fee (how Rovyn makes money)
- **Phase A:** affiliate commissions (~8% activities, ~4% hotels, ~1% flights) + an
  optional flat **planning fee** per traveler.
- **Phase B:** LiteAPI `margin` (we choose, e.g. 8–12%) + Viator ~8% + service fee.
- **Phase C:** a transparent **"Rovyn service fee" line item on each traveler's check**
  (benchmarks travelers accept: human agents charge $25–75/ticket; SquadTrip charges a
  flat 6% and travelers pay it; Hopper's add-ons are 40–50% of its revenue).
- **Compliance guardrails** (fresh precedent: Hopper's **$35M FTC settlement, July 2,
  2026**, over pre-checked "tips" and hidden fees): every fee opt-in, never pre-checked,
  always inside the first-displayed total. For airfare, the **DOT full-fare rule**
  requires any mandatory fee to be included in the first displayed total price.
- **Self-book mode is fee-free** — the checklist + affiliate links cost the traveler
  nothing extra, which keeps the choice honest.

### Group-payment lessons (verified precedents)
- Airbnb ran exactly our model (organizer books, 72h window for others to pay their
  share, forfeit + refund on timeout, piloted with 80k groups) — and **killed it** as
  operationally painful. Lessons: use **authorize-all-then-capture** (nobody is charged
  until everyone commits), a visible payment deadline, and automatic full release on
  timeout — never partial-paid limbo.
- SquadTrip/TrovaTrip/WeTravel prove per-person pay-your-share checkout works today in
  organizer-led travel; Rovyn's twist is applying it to a *peer group's own* plan.

---

## 3. Confirmations (Point 2)

**What each vertical actually returns (verified):**

| Item | Artifact | Source |
|---|---|---|
| Flight | **Airline PNR** (`booking_reference`) + **e-ticket numbers** per passenger — traveler can check in directly with the airline | Duffel order object |
| Hotel | **Hotel confirmation code** honored at the front desk | LiteAPI `hotelConfirmationCode` / Duffel Stays `reference` |
| Activity | **Voucher URL** (sometimes email-only by contract: `isVoucherRestrictionRequired`) | Viator booking response |
| Attraction ticket (later) | Mobile ticket/barcode | Tiqets Booking API |
| Event | No API artifact exists legitimately → link-out; traveler self-confirms | Ticketmaster affiliate |
| Airbnb / self-booked | Traveler-provided: paste confirmation code **or forward the confirmation email** to a per-trip address (`trip+CODE@rovyn.app` — the Wanderlog-proven pattern; we already run Resend for outbound) | user |

**Data model:**

```sql
CREATE TABLE bookings (
    id            TEXT PRIMARY KEY,
    trip_id       TEXT NOT NULL REFERENCES trips(id),
    category      TEXT NOT NULL,   -- flight | stay | visit | event
    scope         TEXT NOT NULL,   -- 'group' or a user_id (flights are per city-group)
    provider      TEXT NOT NULL,   -- duffel | liteapi | viator | self
    status        TEXT NOT NULL,   -- quoted | held | booked | failed | cancelled | self_booked
    confirmation  TEXT,            -- PNR / hotel code / voucher ref
    documents     TEXT,            -- JSON: e-ticket numbers, voucher URLs, uploads
    raw           TEXT,            -- provider payload snapshot (audit)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

**UX:** a **"Bookings" tab** on booked trips — one card per item: provider, confirmation
code (copy button), voucher/e-ticket download, "manage/cancel" link, and per-traveler
visibility (your flight shows *your* e-ticket). Every confirmation is also **emailed via
Resend** as it lands, and the existing receipt email gains the confirmation list.
Self-booked items show "waiting on your confirmation" until pasted/forwarded.

**Cancellations/refunds:** every rate carries its cancellation policy at booking time —
we surface the deadline **in the approval UI** ("free cancellation until Aug 30"). In
Phase C, supplier refunds land in *our* balance, so the reverse flow (auto-refund the
traveler's Stripe payment) must ship with booking, not after.

---

## 4. Accuracy (Point 3)

**Principle: the model proposes, providers dispose.** The LLM plans, describes and
weaves; it never invents a number a traveler pays for. Concretely:

1. **Two data classes in the UI.** Everything AI-generated is an *estimate* and labeled
   as such (today's flight prices etc.). The moment booking approaches, the **Verify &
   Lock step** replaces every bookable item with live provider data — actual flight
   numbers/times from Duffel offers, actual room rates + cancellation policies from
   LiteAPI, actual availability from Viator holds — displayed verbatim with a
   "verified N minutes ago" timestamp and highlighted deltas vs the estimate.
2. **Never book on a stale price.** Duffel hold orders *refuse* payment after a price
   change; LiteAPI prebook and Viator cart-hold lock rates. Any change → re-consent.
3. **Hotel links go to the real hotel.** Providers give us full identity (name, address,
   coordinates, photos) but no official-website field → enrich via **Google Places
   Details** (`website` field); fallback to the Booking.com/Expedia property page. Show
   provider star-rating and guest score as-is, never AI-generated ratings.
4. **Events use Ticketmaster Discovery** (free, data-only) for names/dates/venues, and
   link to the official seller. No AI-invented events on booked plans.
5. **Fee display** follows the DOT full-fare rule and the Hopper-FTC lesson: totals
   first, all mandatory fees inside, nothing pre-checked.

---

## 5. Room allocation for groups (Point 4)

Hotels sell rooms of fixed occupancy; a group of 10 doesn't fit "1 room". Design:

**Step 1 — Room mix (solver + leader choice).** Live rates give per-room-type occupancy
and price (LiteAPI returns these per rate). A small solver enumerates viable mixes for
group size N (10 → 5×double, or 2×triple + 2×double, …), ranked by total price and
fewest rooms. The leader picks the mix (default: cheapest); the mix becomes part of the
plan and re-prices it.

**Step 2 — "Who do you want to room with?"** A rooming step (only shown when the stay is
room-based and rooms < group size): each traveler picks preferred roommates
(multi-select). **Mutual picks** (couple selects each other) are treated as
must-be-together pairs.

**Step 3 — Auto-proposal.** Deterministic matching: satisfy mutual pairs first, then
one-way preferences greedily, then fill remaining spots; never split a mutual pair. The
proposal renders as a **rooming board** — room cards with occupant avatars and empty
"spots". Same interaction language as the rest of Rovyn: travelers can **drag themselves
(or tap "join")** into any room with a free spot; the leader can drag anyone. Unsatisfiable
preferences get flagged to the leader ("Ana and Bia both picked Carla").

**Step 4 — Locks and approval.** Rooming is part of the final plan: any change resets
approvals (existing rule). After payment it locks (existing rule).

**Pricing fairness.** Room types can cost differently per person (a triple is cheaper per
head than a double). The leader chooses, with both numbers shown:
- **Split stay equally** (default — matches Rovyn's promise), or
- **Pay by room** — each traveler's check uses their own room's per-person rate (the
  check system already supports per-traveler amounts because flights differ by city).

**Data:** `plan.rooming = { mix: [{room_type, capacity, price_per_night, count}],
assignments: {room_index: [user_ids]}, split_mode: "equal" | "by_room" }` — JSON on the
plan, same as everything else, promoted to tables when bookings become real (room-level
guest names are required by hotel APIs at booking time, so assignments feed directly
into the LiteAPI/Duffel Stays booking calls).

---

## 6. Sequencing & effort

| Phase | Ships | Needs | Rovyn work |
|---|---|---|---|
| **A** (next) | Booking checklist + confirmation center + self-book flow + affiliate monetization + live hotel prices via LiteAPI free search + Places official-site links | Nothing legal (affiliate accounts only) | `bookings` table & tab, email-forward/paste capture, Travelpayouts/Viator/GYG affiliate params on existing links, LiteAPI search integration at Verify step |
| **B** | In-app hotel + activity booking (suppliers as MoR), Rovyn service fee | Legal entity, Stripe account (fee only), Viator Full+Booking certification, LiteAPI live wallet | Nuitée payment SDK embed, Viator cart/hold/book flow, fee line on check |
| **C** | Full auto-book incl. flights; group authorize-then-capture | Stripe travel approval, Duffel KYC + funded Balance, SOT registrations (market-dependent), EU/UK geo-guard | Verify-and-lock pipeline, extended-auth group collection, booking orchestrator + auto-refund, rooming board feeding real room bookings |

**Honest caveat from the precedent research:** consumer trip-planning apps that tried to
live on booking commissions alone mostly pivoted or died (Travefy → B2B, Lola → wound
down, Pana → acquired); the survivors monetize planning itself (TripIt Pro, Wanderlog
Pro, eDreams Prime subscriptions) or fintech add-ons (Hopper: 40–50% of revenue). Rovyn's
service fee should launch alongside — not instead of — affiliate revenue, and a Pro tier
remains a good future lever.
