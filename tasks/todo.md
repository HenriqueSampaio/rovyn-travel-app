# TODO — Phase B, Part 1: Rooming

Full plan and rationale: [`tasks/plan.md`](./plan.md).
Branch from **`dev`** (not `feature/board-ux-feedback`, which is 4 commits behind and
lacks the `Disc`/`PALETTE_CYCLE` exports this board needs).

---

## Phase 0 — Correct the record, then de-risk

- [x] **Task 0 — Fix SPEC.md** (S) · `SPEC.md`
  - [x] Revert the five "no test suite" edits — `tests/test_maps_and_links.py` exists and is tracked
  - [x] Replace the `liteapi_hotel_id` plumbing paragraph with the `_detach_heavy` hazard
  - [x] Rewrite Part 1 Step 1: mixes are priced one rates call each, not read from one response
  - [x] Drop `by_room` from the spec and from `plan.rooming`
  - [x] Close open questions 4 and 5

- [x] **Task 1 — LiteAPI sandbox spike** (S) · `tests/fixtures/*.json`
  - [x] Does `GET /data/hotel` return `data.rooms[]`, and with which capacity fields?
  - [x] Does `POST /hotels/rates` accept heterogeneous `occupancies`, one total per mix?
  - [x] Does `rates[].maxOccupancy` give per-room capacity (fallback source)?
  - [x] Capture `tests/fixtures/liteapi_data_hotel.json`
  - [x] Capture `tests/fixtures/liteapi_rates_mix.json`
  - [x] Write the go/no-go: `/data/hotel` as capacity source, or probe with rates?
  - ⚠️ **If heterogeneous `occupancies` is rejected — STOP and escalate.** The mix picker and
    all of Part 2's booking shape depend on it. Do not design around it silently.

### ✅ Checkpoint 0
- [x] Fixtures captured from the live sandbox; capacity source decided
- [x] SPEC.md no longer contradicts the code
- [x] **Human review before writing any service code**

---

## Phase 1 — Foundation: pure and safe

- [ ] **Task 2 — The pure solver** (M) · `backend/services/rooming_service.py`, `tests/test_rooming.py`
  - [ ] `stay_key`, `expand_mix`, `mutual_pairs`, `enumerate_mixes`, `propose_assignments`
  - [ ] N=10 over `[2,3,4]` yields `5×double` and `2×triple + 2×double`, correctly ranked
  - [ ] 100 runs of identical input ⇒ byte-identical output
  - [ ] A mutual pair is co-located in every scenario
  - [ ] One-way prefs honored when a bed is free, dropped when not
  - [ ] An oversized mutual component lands in `conflicts`, never split
  - [ ] ⛳ **Freezes the `plan.rooming` JSON shape**
  - Verify: `.venv/bin/python tests/test_rooming.py`

- [ ] **Task 3 — Harden the LLM revision path** (S) · `backend/services/ai_service.py`, `tests/test_plan_revision.py`
  - [ ] `_detach_heavy` pops `plan["rooming"]` wholesale — the model never sees roommate user-ids
  - [ ] Add `liteapi_hotel_id` to the protected key tuple
  - [ ] `_reattach_heavy` overwrites unconditionally (drop the `key not in target` guard —
        it currently lets a hallucinated key beat the original)
  - [ ] Prune rooming keys whose stay no longer exists in the revised plan
  - [ ] Mocked revision altering `rooming` + `liteapi_hotel_id` ⇒ both originals restored
  - [ ] Image/link keys still restored (no regression)
  - Verify: `.venv/bin/python tests/test_plan_revision.py && .venv/bin/python tests/test_maps_and_links.py`

### ☐ Checkpoint 1
- [ ] Solver deterministic; mutual pairs provably never split
- [ ] An LLM revision can no longer corrupt `rooming` or `liteapi_hotel_id`
- [ ] `tests/test_maps_and_links.py` still green
- [ ] **Human review** — this closes a live bug in shipped code

---

## Phase 2 — Slice A: the group can see a priced proposal

- [ ] **Task 4 — Capacity + pricing layer** (M) · `backend/services/hotel_service.py`, `tests/test_rooming_pricing.py`
  - [ ] `hotel_room_capacities(hotel_id) -> list[int]`
  - [ ] `price_room_mixes(hotel_id, check_in, check_out, mixes)` — one rates call per mix, concurrent
  - [ ] `attempts=1` + short timeout (default is 3 × 20s — a 3-minute worst case on a user GET)
  - [ ] Each mix's payload carries *that mix's* `occupancies`, not `[{"adults":2}] * n`
  - [ ] Unset `LITEAPI_API_KEY` ⇒ all `None`, no exception
  - [ ] `_lite_request(path, payload=..., params=...)` — `payload` comes first; call with keywords
  - Verify: `.venv/bin/python tests/test_rooming_pricing.py`

- [ ] **Task 5 — `GET /trips/{id}/rooming`** (M) · `backend/routers/rooming.py`, `backend/main.py`, `tests/test_rooming_api.py`
  - [ ] New router mounted at `prefix="/trips"`, mirroring `bookings.py`
  - [ ] Returns roster, current rooming, prefs, mutual pairs, ≤3 priced candidates per stay
  - [ ] Multi-city ⇒ one entry per stay, keyed by `stay_key`
  - [ ] Non-member ⇒ 403; unknown trip ⇒ 404
  - [ ] No `LITEAPI_API_KEY` ⇒ zero LiteAPI calls, structural candidates still returned
  - [ ] No writes, no caching, `priced_at` stamped
  - Verify: `.venv/bin/python tests/test_rooming_api.py`

- [ ] **Task 6 — The board, read-only** (M) · `frontend/src/components/RoomingBoard.jsx`, `frontend/src/pages/FinalPlan.jsx`, `frontend/src/api/index.js`
  - [ ] `ROOMING_TAB` in `visibleTabs`, gated on a stay existing — **not** on `approved`
  - [ ] Room cards, occupant avatars via `<Disc>`, dashed empty spots
  - [ ] Candidates show total + per-person; unpriced stays show "—"
  - [ ] Fetched on tab mount only — **never on the 3s poll**
  - [ ] `npm run build` clean
  - Verify: `npm run build`, then `/verify`

### ☐ Checkpoint 2
- [ ] A group opens the plan and sees a priced rooming proposal, end to end
- [ ] Network tab shows the rooming call fires once, not every 3 seconds
- [ ] Build clean, backend tests green
- [ ] **Human review**

---

## Phase 3 — Slice B: travelers pick roommates

- [ ] **Task 7 — `roommate_prefs` schema** (S) · `backend/db.py`, `supabase/migrations/<ts>_add_roommate_prefs.sql`
  - [ ] `CREATE TABLE IF NOT EXISTS roommate_prefs (...)` in `_SCHEMA`
  - [ ] Matching Supabase migration — **mandatory**: `_EXPECTED_TABLES` is regex-derived from
        `_SCHEMA` and Postgres boot verifies every table
  - [ ] No `_COLUMN_MIGRATIONS` entry, no `_SCHEMA_CANARIES` entry (canaries track columns)

- [ ] **Task 8 — Preferences, mutual pairs, conflicts** (M) · `backend/routers/rooming.py`, `backend/models/__init__.py`, `frontend/src/components/RoomingBoard.jsx`, `frontend/src/api/index.js`, `tests/test_rooming_api.py`
  - [ ] `POST /rooming/prefs {targets: [...]}` — replace-all of my outgoing edges
  - [ ] `GET /rooming` returns real prefs + mutual pairs; seeds assignments via `propose_assignments`
  - [ ] UI: member multi-select; mutual picks render as a linked pair
  - [ ] Conflicts surface as a leader-facing banner ("Ana and Bia both picked Carla")
  - [ ] ⛳ **Preferences survive an `edit_plan` call** — proves the separate-table decision
  - Verify: `.venv/bin/python tests/test_rooming_api.py && cd frontend && npm run build`

### ☐ Checkpoint 3
- [ ] Preferences persist across a plan revision
- [ ] Mutual pairs visibly never split; conflicts surface
- [ ] **Human review**

---

## Phase 4 — Slice C: move people, commit a mix

- [ ] **Task 9 — Occupant moves (optimistic)** (M) · `backend/routers/rooming.py`, `backend/models/__init__.py`, `frontend/src/components/RoomingBoard.jsx`, `tests/test_rooming_api.py`
  - [ ] `POST /rooming/move {stay_key, user_id, to_room}` — self, or anyone if leader
  - [ ] Rewrites `assignments` only — **no approval reset, no status change**
  - [ ] Full room ⇒ 400; non-leader moving someone else ⇒ 403; after any payment ⇒ 400
  - [ ] Frontend snapshot → mutate → call → roll back in `catch` (mirror `Plan.jsx` `star()`)
  - [ ] Tap-to-move only; no cross-container drag
  - Verify: `.venv/bin/python tests/test_rooming_api.py`, then `/verify`

- [ ] **Task 10 — The leader commits a mix** (M) · `backend/routers/rooming.py`, `backend/models/__init__.py`, `frontend/src/components/RoomingBoard.jsx`, `tests/test_rooming_api.py`
  - [ ] `POST /rooming/mix {stay_key, mix_index}` — **leader only** ⇒ 403 otherwise
  - [ ] Writes `mix`, seeds `assignments`, sets `stay["total_per_person"]`, calls `recompute_plan_costs`
  - [ ] `DELETE FROM approvals` + `status='final_review'`
  - [ ] After any payment ⇒ 400
  - [ ] UI: leader-only picker, 3 candidates with totals, "asks the group to re-approve" warning
  - Verify: `.venv/bin/python tests/test_rooming_api.py && .venv/bin/python tests/test_maps_and_links.py && cd frontend && npm run build`

### ☐ Checkpoint 4 — Ship
- [ ] All four backend test files green, `tests/test_maps_and_links.py` included
- [ ] `npm run build` clean
- [ ] `/verify` pass: 7-person seeded trip — solver proposes, leader picks a mix, approvals reset,
      a traveler moves themselves, mutual pair holds, payment locks the board
- [ ] Phase A checklist unchanged (mark booked / unbook / reconcile)
- [ ] `docs/BOOKING-ARCHITECTURE.md` §5 updated; SPEC.md open questions 4 and 5 closed

---

## Open questions carried forward

1. **Do occupant moves reset approvals?** Plan says no — moving a person between beds changes no
   money. Changing the *mix* does reset. Disagreeing folds Task 9 into Task 10's pattern.
2. **Show all 3 candidate mixes, or just the winner?** Plan shows all 3 with prices.

## Out of scope

Checklist item text reflecting the room count · `by_room` split mode · all of Part 2 (offers,
prebook, book, cancel, settlement) · Viator · the Rovyn service fee.
