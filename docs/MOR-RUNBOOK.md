# Runbook: taking Rovyn live as merchant of record

*Written 2026-10-18 from primary sources fetched and fact-checked the same day (Stripe,
LiteAPI/Nuitée, Duffel docs; US DOT and FTC rules; state statutes; EU/UK regulators). Every
number below has a source in `docs/PROVIDER-API-FACTS.md` or the research notes behind it.
Laws and vendor terms change — re-check each item when you act on it, and have a travel-law
attorney review steps 6–11 before real money moves. Nothing here is legal advice.*

## The goal and the two money models

**Goal:** every payment happens in Rovyn and every reservation is accessible in Rovyn. The
prototype on `feature/stripe-mor-prototype` implements that as *Rovyn is the merchant of record
(MoR)*: travelers pay Rovyn by card (Stripe), Rovyn buys from suppliers at net rates (LiteAPI
hotels charged to Rovyn's card at booking; Duffel flights paid from a prepaid balance), and Rovyn
keeps the spread and owns refunds, disputes and taxes. One supplier per category, the one that
books it: the planning board searches flights on Duffel only and stays on LiteAPI only
(decided 2026-10-19), so every live option a group stars is one Rovyn can book.

The alternative, *supplier is the merchant of record*, keeps the booking inside the app but the
traveler's card is charged by Nuitée (LiteAPI's payment SDK) — or, for activities, by Viator
(Viator's tiers were described in the July architecture notes and were **not re-verified** in
this pass; check viator.com before relying on them); Rovyn earns a commission and charges only
its own service fee through Stripe. It has a much smaller legal
surface (no client-funds trust rules, no lodging-tax collection, no travel chargebacks). The
code supports switching hotels to that mode later (`LITEAPI_BOOK_PAYMENT=sdk`, per `SPEC.md` on
`feature/rooming`). A reasonable sequencing is: build and operate the ledger as MoR, launch
hotels as supplier-MoR while the registrations below are in progress, and flip to full MoR when
steps 2, 7 and 8 are done.

## What already works today (no company, no legal work)

| Need | Where to get it | Cost |
|---|---|---|
| Stripe test keys (`sk_test_…`, `whsec_…`) | dashboard.stripe.com → Developers → API keys; `stripe listen --forward-to localhost:8000/webhooks/stripe` for webhooks | free |
| LiteAPI sandbox booking | the existing `sand_…` key: set `LITEAPI_BOOK_MODE=sandbox` and `LITEAPI_BOOK_API_KEY` to it. Every sandbox key carries a hidden test card, so `payment.method = ACC_CREDIT_CARD` bookings are free and reserve nothing | free |
| Duffel test token (`duffel_test_…`) | app.duffel.com (self-serve); the same token makes the board's flights live (test inventory: "Duffel Airways", ZZ); test mode has an unlimited balance; scenario routes: JFK→EWR holdable order, LHR→STN price change, LGW→STN insufficient balance | free |
| Resend (receipts) | already configured | free tier |

With those three set, the full flow runs: authorize → book → capture → confirmations → receipts
→ cancel → refunds, with Stripe test cards (4242 4242 4242 4242 ok; 4000 0025 0000 3155 3-D
Secure; 4000 0000 0000 9995 insufficient funds).

## Ordered steps to real money

Legend — **S**: needed for the sandbox prototype (done or free). **L**: needed before live money.
Lead times are practical estimates unless a source gives one.

### 1. Lock the money model (S, do now)
Decide MoR vs supplier-MoR for hotels (see above). Flights via Duffel make Rovyn the MoR on the
card statement either way (Duffel sells under its own accreditation but you charge the
traveler). Record the decision in `docs/BOOKING-ARCHITECTURE.md`. Cost $0.

### 2. Form the US entity (L, start immediately — longest lead time)
- Stripe Atlas: $500 one-time; Delaware C-corp or LLC; includes EIN application, 83(b) filing
  for a C-corp, first-year registered agent ($100/yr after). Delaware franchise tax is extra and
  due March 1. An LLC is simpler if you are not raising venture money; a C-corp if you are.
- Non-US founders are supported. Without an SSN the EIN takes roughly 10–35 business days; US
  card payments can be enabled before the EIN arrives, bank account needs it.
- Open a business bank account (and, per step 7, a separate client-trust account).
- Lead time: 1–6 weeks. Owner: you (with the US co-founder as Stripe's "representative").

### 3. Stripe live account and travel approval (L, blocking)
- Activate the account with business KYC. Describe Rovyn accurately as a **travel booking
  platform / travel agency** — never as an airline or cruise operator ("Commercial airlines and
  cruises" are on Stripe's **prohibited** list; "Travel reservation services and clubs" is on the
  **restricted** list, last updated 2026-09-22, at https://stripe.com/legal/restricted-businesses).
  Restricted means Stripe must do "additional due diligence" and "might not be able to grant
  approval"; an approval is "specific to each service offer" and can be "modified or revoked by
  Stripe at any time". Stripe's help-center FAQs for businesses that join through another
  company's platform go further (re-checked 2026-10-19,
  https://support.stripe.com/express/questions/prohibited-and-restricted-businesses-list-faqs
  and https://support.stripe.com/embedded-connect/questions/restricted-business-list-faqs):
  "Stripe and/or their financial partners deem the following travel industries high risk and,
  therefore, they are unsupported: travel reservation services and clubs; airlines; cruises;
  timeshares", while adding that listed businesses "may be eligible for processing with
  explicit prior approval from Stripe". Rovyn would open its own account, where the legal list
  governs, so approval is possible but discretionary and revocable. Treat it as uncertain, not
  a formality, and keep the backup processor below moving in parallel.
- Before launch, write to Stripe sales/compliance and get in writing: (i) approval for the
  restricted category, (ii) reserve terms (travel often carries a fixed or rolling reserve
  because delivery is weeks after payment), (iii) access to **extended authorizations** —
  this is an interchange-plus feature; on standard 2.9% + 30¢ pricing you must request it. Even
  when granted it is per card network and merchant category: on a travel-agency category (MCC
  4722) Visa allows it at +0.08%, American Express does not allow it at all (lodging and vehicle
  rental only), Discover only on airline/hotel/transport categories, and payments made through
  Link never get it. Design the group window around **7 days** (the prototype defaults to 5),
  store `capture_before` per charge, and re-issue a Checkout session when a hold lapses.
- Apply to a backup acquirer that onboards travel **in parallel**, not as a fallback (Adyen,
  Checkout.com, Braintree — verify their current travel policies); each has its own lead time.
  Have the website, refund/cancellation policy and terms of service (step 6) drafted before the
  Stripe application — Stripe asks for them during activation.
- Pricing to model: 2.9% + 30¢ per traveler payment (each share is its own transaction, so fees
  scale with group size), +1.5% international cards, +1% currency conversion, +0.08% on Visa
  extended authorizations outside lodging categories, $15 dispute fee
  (+$15 to contest, refunded if won); processing fees are not returned on refunds. Stripe
  Connect is **not** needed (Rovyn pays suppliers itself). Stripe's own "Managed Payments" MoR
  product is for digital goods only.
- Lead time: 2–6 weeks. Owner: you.

### 4. PCI DSS posture (S to build, L to attest)
- Using Stripe-hosted Checkout keeps you at **SAQ A** (card data never touches the FastAPI
  server). Keep it that way: never add a card form of your own; never call LiteAPI's
  `pci-book` card endpoints.
- Annual attestation in Stripe Dashboard → Compliance → Documents. The January 2025 SAQ A asks
  you to confirm the checkout page is protected against script attacks: serve the plan page with
  a Content-Security-Policy and Subresource Integrity on third-party scripts.
- Cost $0 (an optional external scan ≈ $500/yr). Lead time: days.

### 5. Checkout compliance in code (S — the prototype does most of it)
Implemented by the prototype: the first displayed price is the all-in total (14 CFR 399.84 for
air; FTC Unfair or Deceptive Fees Rule, 16 CFR 464, for lodging since May 12, 2025); nothing is
pre-selected (Hopper's $35M FTC settlement, July 2, 2026, was about pre-checked add-ons);
"Operated by …" shown for code-share flights on the first results screen (14 CFR 257.5); the
airline's 24-hour cancellation right shown when booking ≥ 7 days out; refund duties as MoR:
cancelled or significantly changed flights refunded on request (fare, taxes, ancillaries)
within **7 business days** for card payments, 20 calendar days for other methods (14 CFR
399.80(l), 260.2).
Still to do before live: a refund-policy page, an itemized tax breakdown behind an expander,
the written statement in step 6, and the state refund clocks for hotels and activities (step 14).

### 6. Terms of service, pre-payment disclosure and the emailed written statement (L; draft during S)
One disclosure screen before the group pays, and an email within 3 business days, containing:
Rovyn's legal name, street address and phone; itemized total, amount paid, balance and due
dates; each supplier's name; departure date, time and place; Rovyn's and each supplier's
cancellation and refund policy; state-mandated sentences (California's trust-account/bond
statement and the oral-and-written "not a participant in the Travel Consumer Restitution Fund"
notice until you are California-based; the California CST registration number in ads and
disclosures; Washington and Florida registration numbers; the bold refund statement Illinois
and Washington prescribe; for Illinois residents the trust account's bank name and account
number; for Hawaii residents the DCCA consumer-rights disclosure delivered by ticket issuance;
for Massachusetts residents a statement of when payments are forwarded to suppliers, a written
verification within 5 days of forwarding and a written reservation confirmation within 2 days);
non-refundable rates acknowledged by an explicit checkbox. Florida-targeted ads need "This is an
offer to sell travel" plus the registration number.
Cost: travel-law counsel ≈ $3,000–10,000. Lead time 2–4 weeks.

### 7. Residency capture and geo-fencing (S — cheap now)
Collect billing country and state for every payer. Block EU and UK residents until step 13
(the Package Travel Directive and UK ATOL apply to non-EU/UK sellers that direct sales at those
markets). For the US, either register in CA / FL / WA / HI before accepting their residents
(step 9) or block those states in v1. Illinois, Massachusetts and New York impose conduct and
disclosure rules without registration. Nevada: no regime was found in the current statutes but
the repealing act was not located — confirm with the Nevada Consumer Affairs Division before
accepting Nevada residents; Iowa's regime is gone. A travel-law firm lists Maryland, Michigan,
Pennsylvania, Virginia and Louisiana as regulating sellers of travel; none was verified here —
have counsel check before enabling those states (Louisiana has a telephonic-seller regime,
$150/yr plus a $50,000 bond, if you ever solicit by phone). Delaware needs only a business
licence (≈ $225, a 2013 figure — confirm) if you have a Delaware location.

### 8. Client-funds handling: trust account or bonds (L, blocking for CA / HI / WA / IL residents)
- **California** treats 100% of receipts, including card payments, as trust funds unless you
  post a bond for at least the trust amount; per booking, section 17550.16(a)(7) also exempts
  transactions whose provider is located outside the US when the passenger signs a written
  non-TCRF acknowledgment before paying (potentially most hotels abroad — counsel to confirm).
  **Hawaii** requires a client trust account at a federally insured institution located in
  Hawaii; card payments are deemed compliant when the charge data reaches the processor within
  5 days (section 468L-5), and the trust-account waiver is documented only as a renewal path for
  previously approved licensees — ask the DCCA whether a first application can obtain it.
  **Washington** requires a trust account only for funds held longer than
  **5 business days** — so the backend rule "forward supplier payments within 5 business days"
  avoids it (or a $10,000–50,000 bond). **Illinois** requires a 90% trust account unless you
  carry $1M errors-and-omissions insurance plus a $100,000 bond.
- Operational rule to adopt: Stripe payouts land in the trust account; withdrawals go only to
  suppliers (LiteAPI's charges, Duffel balance top-ups — the Duffel float is a non-trust
  operating balance and is not withdrawable on demand, see step 11), traveler refunds, and
  earned fees after tickets and hotel confirmations are delivered.
- Lead time: 1–2 weeks for the account; bonds 1–3 weeks (annual premium typically a few
  percent of face value — get quotes).

### 9. State seller-of-travel registrations (L), in this order
| State | Regulator | Fee | Security | Notes |
|---|---|---|---|---|
| Florida | FDACS | $300/yr | bond up to $25,000 | 2–4 weeks; registration number in ads |
| Washington | DOL | $222 | trust account, or $10k–50k bond | renews July 1; applies when advertising is directed at WA residents |
| California | Attorney General | $100 per location | 100% trust account or bond | file ≥ 10 days before selling to CA residents; ≤ 14 business days processing; TCRF enrolment only if your principal place of business is in California |
| Hawaii | DCCA | $95 / $140 | Hawaii client trust account or waiver | renews Dec 31 of odd years; separate Activity Desk registration if you sell activities |
| Delaware | — | $225 business licence | — | only if you have a Delaware location |
Budget year 1 ≈ $1,000 in fees plus bond premiums and counsel. No federal travel-agency licence
exists (the DOT regulates "ticket agents" through conduct rules, 49 USC 41712); IATA/ARC
accreditation is not needed because Duffel's Managed Content supplies ticketing authority
(Duffel's current pricing page lists Managed Content at 1% of order value; an older help
article said 0.5% — confirm the figure in writing).

### 10. Insurance (L)
$1M errors-and-omissions (≈ $450–1,200/yr; also unlocks the Illinois trust-account exemption
with a $100k bond), general liability, cyber. Lead time 1 week.

### 11. Supplier go-live (L)
**LiteAPI / Nuitée** — dashboard → Payment Methods → attach a company credit card (this unlocks
the `prod_…` key and is what `ACC_CREDIT_CARD` bookings charge); set up a payout method;
register production webhooks (Developer tools → Webhooks: `booking.book`,
`booking.book.hotelConfirmationNumber`, `booking.checkinInstruction`, `booking.cancel`,
`booking.refund`, `booking.book_error`) pointing at `https://<your host>/webhooks/liteapi` with
the shared token in `LITEAPI_WEBHOOK_TOKEN`; keep the dashboard sandbox/production toggle
matching the key. Get in writing from your account manager: that the wallet is retired and
`ACC_CREDIT_CARD` at `margin:0` is the net-rate path (and any card-spend limits or card fees);
whether `prebook.payment.useOwnSecretKey` lets LiteAPI run the card flow on Rovyn's Stripe;
who remits lodging taxes under each payment option. Do one real refundable booking and cancel
it to validate the money loop — and watch the cancel/refund webhooks arrive during that test,
because they fire only in production, never in sandbox. LiteAPI webhooks carry no signature
(a shared token plus HTTPS and event-id de-duplication is all there is), and the hotel's
confirmation code is never present at booking time, so keep the daily booking-status poll
(step 14). Production rate limit 250 req/s; search/prebook/book are free subject to a
reasonable look-to-book ratio.
**Duffel** — dashboard: verify email, then the verification section (business type, personal
details, business information, KYC) to enable live tokens; add a card under Settings → Billing
for the month-end platform invoice; top up the prepaid **Balance by bank transfer** (funds
credited within about a working day; an order cannot be created until the balance covers it);
withdrawals from the Balance are not self-service — you email Duffel support and the money
returns to the original top-up method — so size the float to a few days of bookings, not more;
set the low-balance alert. The 2% FX fee applies whenever a fare's currency differs from your
billing currency, so set Duffel's billing currency to Stripe's settlement currency (USD) and
quote in it. Fees: $3.00 per confirmed order + 1% of order value (Managed Content) + $2 per paid
ancillary + 2% FX when needed + $0.005 per search above a 1500:1 search-to-book ratio. Refunds
return to the Balance, so Rovyn refunds travelers itself. Ask Duffel in writing for the Managed
Content terms, the merchant-of-record statement, how quickly refunded fares return to your
Balance (the DOT 7-business-day refund clock runs against Rovyn regardless), and whether
card-payment approval is needed for the balance model. Caveat: Duffel's help-centre pages block
automated fetching, so the statements about the card statement, Balance funding and credit
timing were captured from search snippets — re-read them logged in and keep copies.
**Viator / activities** — not re-verified in this pass; the July architecture notes describe a
booking-access tier requiring certification. Until that is confirmed and built, activities stay
link-out and uncharged.

### 12. Lodging taxes when reselling hotel rooms at net rates (L, only in the MoR model)
Florida taxes only the hotel's rate (Alachua County v. Expedia); Georgia charges a $5/night fee
on marketplace innkeepers above $100k in facilitated sales; New York City requires "room
remarketers" to register and remit on the full rent including booking fees; Texas says online
travel companies "may also be responsible"; Washington excludes travel-agency marketplaces from
its facilitator definition. Either use a tax engine (Stripe Tax has no travel codes; Avalara
MyLodgeTax does) or restrict v1 to rates where the supplier collects tax at the property. The
prototype records `taxes_included` versus `due_at_property` per booking for this purpose.

### 13. EU and UK (post-launch)
Selling a flight plus a hotel at a total price to EU residents makes Rovyn an "organiser" under
Directive 2015/2302 — Article 17(1) (kept by the 2026 amending Directive, applying 29 March
2029) requires insolvency protection under the law of each targeted Member State. The UK
Package Travel Regulations 2018 reg 19(4) are identical, and the CAA requires an **ATOL** for
flight-inclusive packages sold to UK consumers by overseas businesses: Small Business ATOL
≈ £1,538 application, £50,000 bond, 500 passengers / £1m cap, £2.50 per passenger, renewal
≈ £962, with months of financial review. Until then: geo-block and no EU/UK marketing.

### 14. Operations before launch (L)
- Refund deadlines in a job, keyed by the payer's state and stored as `refund_due_at` per
  booking: flights 7 business days (card) / 20 calendar days (other) from the request (14 CFR
  399.80(l), 260.2); hotels and activities: Hawaii 14 calendar days from the request (468L-7),
  California the earlier of 30 days or 3 days after first being unable to provide the service
  (17550.14), Washington 30 days after the vendor returns the funds or 14 days if they were
  never forwarded (19.138.040), Massachusetts 30 days (940 CMR 15.06), Illinois "promptly".
  Rovyn's own fee refund policy stated in the terms.
- A daily reconciliation poll of every open booking (`GET /bookings/{id}` for status, the
  hotel confirmation code and payment status; `GET /air/orders/{id}` for documents), because
  webhooks can be missed and LiteAPI's cancel/refund webhooks do not exist in sandbox.
- Reconciliation ledger per traveler and per booking (collected, forwarded to supplier, date
  forwarded, refunded) — the `settlements` and `payments` tables are the start.
- Monitoring: Duffel balance and low-balance alert; Stripe webhook failures; LiteAPI webhook
  delivery; `capture_before` sweeps so no authorization expires uncaptured.
- Dispute evidence pack per booking (service date, supplier confirmations, cancellation policy
  acknowledgment, correspondence) to answer `charge.dispute.created` before its due date.
- A support inbox; Duffel notifies it when key-collection details must be forwarded.

## Costs at a glance (year 1, before volume)
| Item | Cost |
|---|---|
| Entity (Stripe Atlas) | $500 + Delaware franchise tax |
| State registrations | ≈ $1,000 fees + bond premiums |
| Counsel for terms/disclosures | $3,000–10,000 |
| E&O + liability + cyber | ≈ $1,000–3,000 |
| Stripe | 2.9% + 30¢ per share (+1.5% intl, +1% FX), $15 per dispute |
| Duffel | $3 + 1% per order, $2 per ancillary, 2% FX |
| LiteAPI | free API; card fees on net rates if any (confirm) |
| UK ATOL (if/when) | ≈ £1,538 + £50k bond |

## What runs in parallel
Weeks 1–6: entity (2) while the prototype is exercised in sandbox (0) and the money model is
locked (1). Weeks 3–8: Stripe activation and travel approval (3), terms and disclosures (6),
trust account and bonds (8), E&O (10). Weeks 4–10: state registrations (9) and supplier go-live
(11). Launch with US residents of registered states only, EU/UK blocked (7). Hotels can launch
earlier as supplier-MoR if the registrations lag.

## Questions only you can answer
1. Where is Rovyn's principal place of business and where do the founders reside? California
   residence triggers the TCRF fund; Hawaii, Washington and Florida have their own triggers.
2. Does the entity already exist, and is there an existing Stripe account (test or live)?
3. Which launch markets: US-only first? Which states do you want to register in before launch
   versus block?
4. MoR for hotels from day one, or supplier-MoR first (the lower-risk launch)?
