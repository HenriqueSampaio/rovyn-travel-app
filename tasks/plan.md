# Implementation Plan — Phase B, Part 1: Rooming

## Context

Rovyn plans group trips but stops short of booking them. `docs/BOOKING-ARCHITECTURE.md` §5 calls for a rooming layer: the group decides **who sleeps in which room** before anyone books. Today the code guesses — `hotel_service.py:195` hardcodes `rooms_needed = max(1, (group_size + 1) // 2)`, "two travelers per room, rounding up." Nobody chooses roommates, and a group of 7 silently books 4 doubles.

Part 1 replaces that guess with a real decision: a solver enumerates viable room mixes over the hotel's actual room capacities, the leader picks one (priced), travelers pick roommates, and a board shows who is in which bed. It is a complete, shippable feature with **no dependency on LiteAPI booking** — it works with `LITEAPI_BOOK_MODE=off`. It is also the prerequisite for Part 2, because the chosen mix *is* the `occupancies` array every booking rates call must send.

Scope was confirmed with the user: **Part 1 only**, mixes priced by real rates calls, `split_mode` fixed at `"equal"`.

### Three spec premises that turned out to be false

Exploration contradicted `SPEC.md` on facts the design rests on. All three are verified against the code:

1. **A test suite exists.** `tests/test_maps_and_links.py` is tracked on `HEAD` and `dev`. It already provides the harness this work needs: env pinning before backend import (`:25-36`), a temp SQLite (`:38-41`), `TestClient`, a `fake_urlopen` monkeypatch that routes by URL and raises on anything unexpected (`:104-145`), and an `ok(label, cond)` assertion helper (`:205-209`). A previous session wrongly claimed no tests existed anywhere in history. **SPEC.md must be reverted on this point.**

2. **`liteapi_hotel_id` already reaches the plan and the checklist.** `select_winners` copies the full option through (`ai_service.py:1361`, `:1376`) into `plan["stay"]` / `plan["stays"]` (`:1608-1611`), and `bookings.py:93` snapshots the whole stay dict into `bookings.details`, returned parsed at `:197`. The "plumbing gap" SPEC.md describes does not exist.

   The real hazard is elsewhere and worse: `_detach_heavy` (`ai_service.py:1841`) shields only `("image", "links", "link", "booking_links", "official_website")` before serializing the plan into an LLM revision prompt (`:1908`). `liteapi_hotel_id` is sent to the model and whatever comes back is trusted. Once `plan.rooming` exists, **roommate user-ids would be round-tripped through a language model too.** Worse, `_reattach_heavy`'s guard `if ... and key not in target` (`:1889`) means a *hallucinated* key beats the original. This is the spec's own "never" rule (*AI-generated numbers anywhere in the booking flow*) being broken by code that already ships.

3. **The solver cannot rank mixes by price.** SPEC.md Step 1 assumes "live rates expose per-room-type occupancy and price." They don't. The codebase says so twice, in its own comments:

   > `hotel_service.py:209` — *offerRetailRate is the WHOLE offer (all requested rooms summed, per LiteAPI docs) — per-occupancy rate totals must not be mixed in*
   > `hotel_service.py:569` — *offerRetailRate = ALL requested rooms for the WHOLE stay*

   Price is a function of the entire `occupancies` array. Each candidate mix is therefore a **separate rates call**. This also kills `by_room` split mode: with only a bundled total, a per-room price would have to be invented, and inventing a number attached to money is exactly what the spec forbids. **`split_mode` is `"equal"`, and Part 1 builds no `by_room` UI.**

### Start from `dev`, not this branch

`feature/board-ux-feedback` is four commits behind `dev`. PR #19 landed `PALETTE`, `PALETTE_CYCLE`, `Pill`, and `Disc` as exports of `ui.jsx` (`dev:frontend/src/ui.jsx:22-58`). `Disc`'s comment reads *"Round tinted serif monogram — trips without a photo, travelers, authors"* — it is the occupant avatar, already built. Branch this work from `dev` as `feature/rooming`.

---

## Architecture Decisions

**Where rooming lives.** Inside the existing `trips.final_plan` JSON — no new column. Keyed per stay so multi-city works:

```json
"rooming": {
  "<stay_key>": {
    "split_mode": "equal",
    "mix": { "hotel_id": "lp999", "rooms": [{"sleeps": 3, "count": 2}, {"sleeps": 2, "count": 2}],
             "room_count": 4, "capacity": 10, "total_price": 4200, "currency": "USD",
             "priced_at": "2026-07-09T12:00:00Z" },
    "assignments": [["u1","u2","u3"], ["u4","u5","u6"], ["u7","u8"], ["u9","u10"]]
  }
}
```

`stay_key = stay.get("city") or stay.get("name")` — the *exact* stay identity `_detach_heavy`/`_reattach_heavy` already use (`ai_service.py:1850`, `:1872`). Single-city trips have no `city`, so the key is the name; there is one entry. `assignments` is a list-of-lists indexed by room, expanded from `mix.rooms` largest-first — no stringified-int JSON keys. `total_price` is nullable: AI/sample stays have no `liteapi_hotel_id`, so they get a structural mix with no price, and the board still assigns beds.

**Roommate preferences go in a new table, not the plan.** Three reasons, all from the code: (a) `revise_final_plan` rewrites the whole plan blob through an LLM and every edit endpoint does `UPDATE trips SET final_plan = ?` wholesale — preferences stored inside would not survive a re-finalize; (b) each traveler toggles their own preferences concurrently, and a blob forces read-modify-write of the entire plan on every tap; (c) every other per-user signal in this schema is a thin join table — `votes`, `stars`, `approvals`, `category_progress` (`db.py:87-116`). A **mutual pair** is both directed edges existing; it is computed server-side, never stored.

**Structural changes reset approvals; cosmetic ones don't.** Choosing a different *mix* changes the stay total, therefore `cost_per_person.stay`, therefore everyone's check — it must go through the existing reset path (`DELETE FROM approvals` + `status='final_review'`, as at `trips.py:1424-1428`) and is not optimistic. Moving an *occupant* between beds changes no money at all; it is optimistic, resets nothing. This is how "rooming changes reset approvals" and "the board is optimistic — rooming is not money" (SPEC.md Part 1 Step 4) are both true. **Flagged as a product call — if you want occupant moves to force re-approval, say so and Task 9 changes.** Both endpoints respect the payment lock.

**Prices are never cached.** `refresh_room_offers`' docstring already sets the rule: *"prices are the one part of a detail page that must never be served stale."* `GET /trips/{id}/rooming` prices the top 3 mixes live via `ThreadPoolExecutor` and stamps `priced_at`. It must **not** be on `FinalPlan.jsx`'s 3-second poll (`:482`) — that poll calls `getPlan`, which carries `plan.rooming` (mix + assignments) for free. The rooming endpoint is called on tab mount and explicit refresh only.

**The solver is pure.** `rooming_service.py` imports nothing but stdlib, does no I/O, and is fully unit-testable. Capacity discovery and pricing are the caller's job.

---

## Task List

### Phase 0 — Correct the record, then de-risk

#### Task 0: Fix SPEC.md
**Description:** Correct the three false premises above. Revert the five "no test suite" edits; replace the `liteapi_hotel_id` plumbing paragraph with the `_detach_heavy` hazard; rewrite Part 1 Step 1 to say mixes are priced one rates call each; drop `by_room` from the spec and from `plan.rooming`; close open questions 4 and 5.
**Acceptance:** No claim in SPEC.md contradicts the code. Open questions 1–3 (Part 2 concerns) remain.
**Verify:** Re-read §"Testing Strategy", §"Data changes", §"Part 1 Step 1" against the cited line numbers.
**Dependencies:** None. **Files:** `SPEC.md`. **Size:** S

#### Task 1: LiteAPI sandbox spike
**Description:** Throwaway read-only script against the existing sandbox key. Answer: (a) does `GET /data/hotel` return `data.rooms[]`, and with what capacity fields (`maxOccupancy`, `maxAdults`, `bedTypes`)? It is already fetched at `hotel_service.py:519` but `rooms[]` is never parsed. (b) Does `POST /hotels/rates` accept a **heterogeneous** `occupancies` array like `[{"adults":3},{"adults":2}]`, and does `offerRetailRate.amount` change with it? (c) Does `rates[].maxOccupancy` reflect per-room capacity — the fallback capacity source if `rooms[]` is unusable?

Capture sanitized real responses as fixtures. The script is not committed.
**Acceptance:**
- [ ] `tests/fixtures/liteapi_data_hotel.json` exists; `rooms[]` present (or its absence documented in the task notes)
- [ ] `tests/fixtures/liteapi_rates_mix.json` exists, from a heterogeneous `occupancies` call
- [ ] A written go/no-go: is `/data/hotel` the capacity source, or do we probe with rates?
**Verify:** `.venv/bin/python -c "import json; d=json.load(open('tests/fixtures/liteapi_rates_mix.json')); print(d['data'][0]['roomTypes'][0]['offerRetailRate'])"`
**Dependencies:** None. **Files:** 2 new fixtures. **Size:** S

> **If (b) fails — heterogeneous `occupancies` rejected, or one total not obtainable per mix — stop and escalate.** The mix picker and all of Part 2's booking shape depend on it. Do not design around it silently.

#### ✅ Task 1 findings (run 2026-07-09, sandbox, Rome, 2026-08-23 → 2026-08-26)

**All three questions: GO.**

**(a) `/data/hotel` is the capacity source.** `data.rooms[]` present — 9 rooms on hotel `lp1c4b4`, each with `maxOccupancy`, `maxAdults`, `maxChildren`, `bedTypes`, `roomName`, `roomSizeSquare`. Distinct capacities `[1, 2, 3, 4]`. The call is already made by `_liteapi_stay_page` (`hotel_service.py:519`); only the parse is new. **No probe needed.**

**(b) Heterogeneous `occupancies` accepted, and totals differ per mix.** On the same hotel and dates:

| occupancies | cheapest `offerRetailRate.amount` |
|---|---|
| `[{adults:2},{adults:2}]` | 918.98 |
| `[{adults:3},{adults:2}]` | 1171.18 |

Confirms one rates call per candidate mix. `[{adults:4}]` and `[{adults:1}]` also returned availability.

**(c) — and this answers SPEC.md open question 1, a Part 2 blocker.** Each `roomTypes[]` entry carries **one string `offerId`**, **one `offerRetailRate`** covering the whole array, and **one `rates[]` entry per requested occupancy**:

```
offerId: "3gAWonJzko-kc3JpZNoBGkc0WlRPTkpYR0EzREtOWlNHWTRUTVpSWEdJWkRBTlpTR1pURE1aUldNUVp…"  (str)
offerRetailRate: {"amount": 1171.18, "currency": "USD"}
rates[0]: occupancyNumber=1  maxOccupancy=3  name="Superior Room"
rates[1]: occupancyNumber=2  maxOccupancy=2  name="Small Double Room"
```

**All 200 room types in the response had one rate per occupancy.** The Node SDK's array example was misleading; the API reference's "string" is correct. **One prebook, one payment, all rooms — the one-booking model stands.** `rates[].maxOccupancy` also works as a fallback capacity source.

**Unexpected:** `suggestedSellingPrice` (1346.86) ≈ 1.15 × `offerRetailRate` (1171.18). If that ratio is the dashboard margin, the spec's 10% is configured somewhere we have not looked. Logged as SPEC.md open question 6 — **resolve before Part 2 displays any total to a traveler.**

Fixtures written (trimmed from 10 MB to 18 KB, images and the roomType tail dropped; every field the code reads retained): `tests/fixtures/liteapi_data_hotel.json`, `tests/fixtures/liteapi_rates_mix.json`.

### ✅ Checkpoint 0
- [ ] Fixtures captured from the live sandbox; capacity source decided
- [ ] SPEC.md no longer contradicts the code
- [ ] **Human review before writing any service code**

---

### Phase 1 — Foundation: pure and safe

#### Task 2: The pure solver
**Description:** New `backend/services/rooming_service.py`. Stdlib only, no I/O.

```python
def stay_key(stay: dict) -> str                      # stay.get("city") or stay.get("name")
def expand_mix(mix: dict) -> list[dict]              # -> [{"sleeps": 3}, ...] largest-first
def mutual_pairs(edges: list[tuple[str, str]]) -> list[tuple[str, str]]
def enumerate_mixes(group_size: int, capacities: list[int], *, max_results: int = 3) -> list[dict]
def propose_assignments(mix, members, mutuals, preferences) -> dict   # {"rooms": [...], "conflicts": [...]}
```

`enumerate_mixes` does DFS over multisets of room sizes with total capacity pruned to `[N, N + max(capacities) - 1]` and no fully-removable room. Ranked ascending by `(wasted_beds, room_count, tuple(sorted(sleeps, reverse=True)))` — the size signature is a total order, so identical input always yields identical output. Naive enumeration is safe: you enumerate *mixes*, not assignments, and restricted partitions of N ≤ 20 over ≤ 6 distinct room sizes is low hundreds of nodes. Guard `group_size <= 40`.

`propose_assignments`: union-find over mutual pairs → place components largest-first into the smallest room that fits → one-way preferences greedily (each remaining member, sorted, joins a room already holding someone they picked, if a bed is free) → fill lowest-index free bed. **A mutual component is never split.** If one is larger than the biggest room, it lands in `conflicts` — surfaced, never silently violated.

**This task freezes the `plan.rooming` JSON shape.**
**Acceptance:**
- [ ] N=10 over capacities `[2,3,4]` yields both `5×double` and `2×triple + 2×double`, correctly ranked
- [ ] 100 runs of identical input produce byte-identical output
- [ ] A mutual pair `(a,b)` is co-located in every scenario, including when preferences conflict
- [ ] One-way preferences honored when a bed is free, dropped silently when not
- [ ] A mutual component exceeding max room capacity appears in `conflicts` and is not split
**Verify:** `.venv/bin/python tests/test_rooming.py`
**Dependencies:** None. **Files:** `backend/services/rooming_service.py`, `tests/test_rooming.py`. **Size:** M

#### Task 3: Harden the LLM revision path
**Description:** In `ai_service.py`:
1. `_detach_heavy` pops `plan["rooming"]` wholesale as a `("plan", None, "rooming", …)` entry — the model never sees roommate user-ids.
2. Add `"liteapi_hotel_id"` to the protected key tuple at `:1841`. `take()`'s `key in obj` guard makes it a no-op for objects that lack it.
3. `_reattach_heavy` (`:1889`) **overwrites unconditionally**: `if isinstance(target, dict): target[key] = val`. Every entry in `heavy` was popped by us; the model never legitimately owns these keys, so ours must win. The current `key not in target` guard does the opposite.
4. After restoring `rooming`, prune keys whose stay no longer exists in the revised plan.

Note the failure mode this leaves: if the model renames a stay, `_reattach_heavy`'s `city or name` lookup (`:1872`) finds no target and `liteapi_hotel_id` is lost. That fails *closed* — no id, no Book button, link-out still works. Acceptable; do not paper over it.
**Acceptance:**
- [ ] A mocked revision returning an altered `rooming` and a rewritten `liteapi_hotel_id` restores both originals
- [ ] Image/link keys still restored (no regression)
- [ ] A rooming key for a stay absent from the revised plan is dropped
**Verify:** `.venv/bin/python tests/test_plan_revision.py && .venv/bin/python tests/test_maps_and_links.py`
**Dependencies:** Task 2 (frozen shape). **Files:** `backend/services/ai_service.py`, `tests/test_plan_revision.py`. **Size:** S

### ✅ Checkpoint 1
- [ ] Solver deterministic; mutual pairs provably never split
- [ ] An LLM revision can no longer corrupt `rooming` or `liteapi_hotel_id`
- [ ] `tests/test_maps_and_links.py` still green
- [ ] **Human review** — this closes a live bug in shipped code

---

### Phase 2 — Slice A: the group can see a priced proposal

#### Task 4: Capacity + pricing layer
**Description:** In `hotel_service.py`, two new functions beside `refresh_room_offers`:
- `hotel_room_capacities(hotel_id) -> list[int]` — sorted distinct capacities, parsed from `/data/hotel`'s `rooms[]` (already fetched at `:519`), or the probe decided in Task 1.
- `price_room_mixes(hotel_id, check_in, check_out, mixes) -> list[float | None]` — one `_lite_request("/hotels/rates", payload={...})` per mix, run concurrently (precedent: `ThreadPoolExecutor` at `:739`). Use `attempts=1` and a short timeout here; three mixes × three retries × 20s is a 3-minute worst case on a user-facing GET.

Note `_lite_request(path, payload=None, params=None, ...)` — `payload` is the **first** keyword after `path`. Call with keywords.

Every failure degrades to `None`/`[]`, never raises. House rule, and `real_stay_options:256` is the precedent.
**Acceptance:**
- [ ] With fixtures served through `fake_urlopen`, `price_room_mixes` returns one total per mix
- [ ] Each mix's rates payload carries *that mix's* `occupancies`, not `[{"adults":2}] * n`
- [ ] Unset `LITEAPI_API_KEY` ⇒ all `None`, no exception
- [ ] Capacities parse to sorted distinct ints
**Verify:** `.venv/bin/python tests/test_rooming_pricing.py`
**Dependencies:** Tasks 1, 2. **Files:** `backend/services/hotel_service.py`, `tests/test_rooming_pricing.py`. **Size:** M

#### Task 5: `GET /trips/{id}/rooming`
**Description:** New `backend/routers/rooming.py`, mounted at `prefix="/trips"` in `main.py` exactly as `bookings.py` is. Imports `_member_trip` from `trips.py:66` for the 403 gate.

Returns, per stay: `roster` (member ids + names), current `rooming` from the plan, my `preferences` (empty until Task 8), computed `mutual_pairs`, and up to 3 **priced** `candidates`. Candidates come from `enumerate_mixes` + `price_room_mixes`. Stays without `liteapi_hotel_id` get structural candidates with `total_price: null`. No writes, no caching, `priced_at` stamped.
**Acceptance:**
- [ ] Returns ≤3 candidates per stay, priced when the stay is a LiteAPI property
- [ ] Multi-city returns one entry per stay, keyed by `stay_key`
- [ ] Non-member ⇒ 403; unknown trip ⇒ 404
- [ ] Endpoint issues zero LiteAPI calls when `LITEAPI_API_KEY` is unset, and still returns structural candidates
**Verify:** `.venv/bin/python tests/test_rooming_api.py`
**Dependencies:** Task 4. **Files:** `backend/routers/rooming.py`, `backend/main.py`, `tests/test_rooming_api.py`. **Size:** M

#### Task 6: The board, read-only
**Description:** New `frontend/src/components/RoomingBoard.jsx`. A `ROOMING_TAB` joins `visibleTabs` (`FinalPlan.jsx:506-508`) — gated on a stay existing, **not** on `approved`, since rooming must happen before approval. Room cards, occupant avatars via `<Disc label={name} color={PALETTE_CYCLE[i % 3]} size={36} />` from `ui.jsx`, empty spots rendered as dashed placeholders. Fetched once on tab mount, not on the 3s poll. New `api.getRooming(tripId)` in `frontend/src/api/index.js` following the `getBookings` shape (`:199`).
**Acceptance:**
- [ ] Board renders rooms, occupants, and empty spots from `plan.rooming`
- [ ] Candidate mixes show a total and a per-person figure; unpriced stays show "—"
- [ ] Tab hidden when the trip has no stay; present at `final_review`
- [ ] `cd frontend && npm run build` clean
**Verify:** `npm run build`, then `/verify` — open a seeded trip, confirm the board renders and the poll does not re-price.
**Dependencies:** Task 5. **Files:** `frontend/src/components/RoomingBoard.jsx`, `frontend/src/pages/FinalPlan.jsx`, `frontend/src/api/index.js`. **Size:** M

### ✅ Checkpoint 2
- [ ] A group opens the plan and sees a priced rooming proposal, end to end
- [ ] Network tab shows the rooming call fires once, not every 3 seconds
- [ ] Build clean, backend tests green
- [ ] **Human review**

---

### Phase 3 — Slice B: travelers pick roommates

#### Task 7: `roommate_prefs` schema
**Description:** `CREATE TABLE IF NOT EXISTS roommate_prefs (trip_id, user_id, target_id, created_at, PRIMARY KEY (trip_id, user_id, target_id))` in `db.py`'s `_SCHEMA`. Because `_EXPECTED_TABLES` is regex-derived from `_SCHEMA` (`db.py:216`) and Postgres boot verifies every expected table (`:342-346`), a matching `supabase/migrations/<ts>_add_roommate_prefs.sql` is **mandatory** or Supabase boots will fail loudly. No `_COLUMN_MIGRATIONS` entry and no `_SCHEMA_CANARIES` entry — canaries track columns, and table existence is already checked.
**Acceptance:**
- [ ] Fresh SQLite boot creates the table; an existing SQLite DB gains it on next boot
- [ ] Migration file present, uses `CREATE TABLE IF NOT EXISTS`
**Verify:** `.venv/bin/python -c "import tempfile,pathlib,sqlite3,backend.db as db; db._DB_PATH=pathlib.Path(tempfile.mkdtemp())/'t.db'; db.init_db(); print('roommate_prefs' in {r[0] for r in sqlite3.connect(db._DB_PATH).execute('select name from sqlite_master')})"`
**Dependencies:** None. **Files:** `backend/db.py`, `supabase/migrations/<ts>_add_roommate_prefs.sql`. **Size:** S

#### Task 8: Preferences, mutual pairs, conflicts
**Description:** `POST /trips/{id}/rooming/prefs {targets: [user_id,...]}` — replace-all of my outgoing edges. `GET /rooming` now returns real `preferences` and `mutual_pairs`, and seeds `candidates[].assignments` through `propose_assignments`. UI: multi-select of trip members; mutual picks render as a linked pair; `conflicts` surface as a leader-facing banner ("Ana and Bia both picked Carla").
**Acceptance:**
- [ ] Posting prefs replaces my edges; a reciprocal pick becomes a mutual pair
- [ ] **Preferences survive an `edit_plan` call** — the separate-table decision, proven
- [ ] Proposed assignments keep mutual pairs together
- [ ] An unsatisfiable preference set produces a visible conflict, not a silent drop
**Verify:** `.venv/bin/python tests/test_rooming_api.py && cd frontend && npm run build`
**Dependencies:** Tasks 5, 6, 7. **Files:** `backend/routers/rooming.py`, `backend/models/__init__.py`, `frontend/src/components/RoomingBoard.jsx`, `frontend/src/api/index.js`, `tests/test_rooming_api.py`. **Size:** M

### ✅ Checkpoint 3
- [ ] Preferences persist across a plan revision
- [ ] Mutual pairs visibly never split; conflicts surface
- [ ] **Human review**

---

### Phase 4 — Slice C: move people, commit a mix

#### Task 9: Occupant moves (optimistic)
**Description:** `POST /trips/{id}/rooming/move {stay_key, user_id, to_room}`. Any member may move themselves; the leader may move anyone. Rewrites `assignments` only — **no approval reset, no status change**. Rejects a full room. Respects the payment lock (`COUNT(payments) > 0` ⇒ 400, reusing the message at `trips.py:1405`). Frontend mirrors `Plan.jsx`'s `star()` (`:465-486`): snapshot → mutate → call → roll back in `catch`. Tap-to-move only; cross-container drag has no precedent in this codebase and is not worth inventing here.
**Acceptance:**
- [ ] Self-move into a free bed lands instantly and leaves `status` and approvals untouched
- [ ] A non-leader moving someone else ⇒ 403; a full room ⇒ 400
- [ ] Server error rolls the avatar back to its old bed
- [ ] After any payment ⇒ 400, board renders read-only
**Verify:** `.venv/bin/python tests/test_rooming_api.py`, then `/verify` — two seeded members, move, kill the backend, confirm rollback.
**Dependencies:** Task 6. **Files:** `backend/routers/rooming.py`, `backend/models/__init__.py`, `frontend/src/components/RoomingBoard.jsx`, `tests/test_rooming_api.py`. **Size:** M

#### Task 10: The leader commits a mix
**Description:** `POST /trips/{id}/rooming/mix {stay_key, mix_index}` — **leader only** (inline `if user["id"] != trip["leader_id"]` per `trips.py:1224`). Writes the chosen `mix` and seeds `assignments` via `propose_assignments`. Sets `stay["total_per_person"] = round(total_price / group_size)`, then calls `recompute_plan_costs(plan)` (`ai_service.py:1527`, which re-derives `cost_per_person` from the stays). Then `DELETE FROM approvals` + `status='final_review'`, matching `trips.py:1424-1428`. Payment lock applies. UI: leader-only mix picker showing all 3 candidates with totals and per-person figures, and an explicit "this asks the group to re-approve" warning — copy already exists at `FinalPlan.jsx:791`.
**Acceptance:**
- [ ] Non-leader ⇒ 403
- [ ] Choosing a mix updates `cost_per_person.stay` and every member's check
- [ ] Approvals wiped, `status` back to `final_review`, and the UI reflects it within one poll
- [ ] After any payment ⇒ 400
- [ ] Assignments are seeded from preferences, mutual pairs intact
**Verify:** `.venv/bin/python tests/test_rooming_api.py && .venv/bin/python tests/test_maps_and_links.py && cd frontend && npm run build`
**Dependencies:** Tasks 4, 8, 9. **Files:** `backend/routers/rooming.py`, `backend/models/__init__.py`, `frontend/src/components/RoomingBoard.jsx`, `tests/test_rooming_api.py`. **Size:** M

### ✅ Checkpoint 4 — Ship
- [ ] All four backend test files green, `tests/test_maps_and_links.py` included
- [ ] `npm run build` clean
- [ ] `/verify` pass: 7-person seeded trip — solver proposes, leader picks a mix, approvals reset, a traveler moves themselves, mutual pair holds, payment locks the board
- [ ] Phase A checklist unchanged (mark booked / unbook / reconcile)
- [ ] `docs/BOOKING-ARCHITECTURE.md` §5 updated; SPEC.md open questions 4 and 5 closed

---

## Verification

**Unit / integration**, following the harness in `tests/test_maps_and_links.py` — plain python, no pytest:

```
.venv/bin/python tests/test_rooming.py            # pure solver
.venv/bin/python tests/test_plan_revision.py      # LLM hardening
.venv/bin/python tests/test_rooming_pricing.py    # capacity + per-mix pricing
.venv/bin/python tests/test_rooming_api.py        # endpoints, permissions, locks
.venv/bin/python tests/test_maps_and_links.py     # regression
cd frontend && npm run build
```

Extend `fake_urlopen` (`tests/test_maps_and_links.py:104-145`) with `api.liteapi.travel` routes fed from the Task 1 fixtures. It raises `AssertionError` on unexpected URLs (`:142`), so an unstubbed call fails loudly rather than hitting the network.

**End-to-end** via `/verify`: side-port launch with `DATABASE_URL` and `SUPABASE_URL` blanked (the `.env`-trap rule — `load_dotenv` walks up into the real `.env`). Seed a 7-person trip through `final_review`, then exercise the Checkpoint 4 list by hand.

**No new dependencies.** stdlib `urllib` outbound, no pip, no npm.

---

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `/hotels/rates` rejects heterogeneous `occupancies` | **Fatal** — the mix picker and Part 2's whole shape collapse | Task 1 is the gate. Escalate, don't design around it. |
| `/data/hotel` has no usable `rooms[]` | Medium — capacities need a probe | Task 1 decides; solver stays pure either way, only the caller changes |
| 3 rates calls on a user-facing GET | Medium — `_lite_request` defaults to 3 attempts × 20s | `attempts=1`, short timeout, `ThreadPoolExecutor`, degrade to unpriced |
| `_reattach_heavy` overwrite changes existing behavior | Low — image/link keys now always restored | Strictly more correct; Task 3's test covers the old paths |
| Sandbox prices are not real prices | Low | Cosmetic in Part 1; matters only at Part 2's `live` gate |
| Only 3 palette colors for N travelers | Cosmetic | Identity is carried by the initial and name; color is decoration |

## Open Questions

1. **Do occupant moves reset approvals?** I say no — moving a person between beds changes no money, and the spec explicitly calls the board optimistic. Changing the *mix* does reset. If you disagree, Task 9 folds into the Task 10 pattern and the board stops being optimistic.
2. **Should the board show all 3 candidate mixes, or just the winner?** Plan assumes all 3 with prices — that was the point of ranking. (This closes SPEC.md open question 4.)

## Out of scope

Checklist item text reflecting the room count; `by_room` split mode; anything in Part 2 (offers, prebook, book, cancel, settlement); Viator; the Rovyn service fee.

---

*On approval this plan is copied to `tasks/plan.md`, with the checklist extracted to `tasks/todo.md`, per the `/plan` convention.*
