"""Unit tests for the rooming solver — pure functions, no I/O.

Runs with plain python (no pytest needed):

    /Users/henriquesampaio/Rovyn/Rovyn/.venv/bin/python tests/test_rooming.py

Nothing here touches the network, the DB or the environment: rooming_service
imports only the stdlib. That is the whole point of keeping the solver pure —
capacity discovery and pricing live in hotel_service, where they can fail.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.rooming_service import (  # noqa: E402
    enumerate_mixes,
    expand_mix,
    mutual_pairs,
    propose_assignments,
    stay_key,
)

passed = 0


def ok(label, cond, extra=""):
    global passed
    assert cond, f"FAIL: {label} {extra}"
    passed += 1
    print(f"  ok - {label}")


def sig(mix):
    """A mix as a flat, comparable tuple of room sizes — test-readable."""
    return tuple(expand_mix(mix))


# ---------------------------------------------------------------------------
# 1. stay_key — the identity _detach_heavy/_reattach_heavy already use
# ---------------------------------------------------------------------------
print("stay_key:")
ok("city wins over name",
   stay_key({"city": "Venice, Italy", "name": "Hotel Danieli"}) == "Venice, Italy")
ok("falls back to name on single-city stays",
   stay_key({"name": "Hotel Danieli"}) == "Hotel Danieli")
ok("empty stay yields empty key", stay_key({}) == "")
ok("whitespace stripped", stay_key({"city": "  Rome  "}) == "Rome")


# ---------------------------------------------------------------------------
# 2. enumerate_mixes — structural, deterministic, no prices
# ---------------------------------------------------------------------------
print("\nenumerate_mixes:")
all10 = enumerate_mixes(10, [2, 3, 4], max_results=None)
sigs10 = [sig(m) for m in all10]

ok("10 over [2,3,4] offers 5x double", (2, 2, 2, 2, 2) in sigs10)
ok("10 over [2,3,4] offers 2x triple + 2x double", (3, 3, 2, 2) in sigs10)
ok("every mix seats the whole group", all(m["capacity"] >= 10 for m in all10))
ok("no mix has a removable room",
   all(m["capacity"] - min(expand_mix(m)) < 10 for m in all10),
   "6x double (capacity 12, drop one -> 10) must never be generated")
ok("6x double is not generated", (2, 2, 2, 2, 2, 2) not in sigs10)

ok("ranked by wasted beds first",
   [m["wasted_beds"] for m in all10] == sorted(m["wasted_beds"] for m in all10))
zero_waste = [m for m in all10 if m["wasted_beds"] == 0]
ok("then by fewest rooms",
   [m["room_count"] for m in zero_waste] == sorted(m["room_count"] for m in zero_waste))
ok("top mix for 10 is quad + 2x triple", sigs10[0] == (4, 3, 3),
   f"got {sigs10[0]}")
ok("5x double ranks below the 4-room mixes",
   sigs10.index((2, 2, 2, 2, 2)) > sigs10.index((3, 3, 2, 2)))

ok("default returns at most 3", len(enumerate_mixes(10, [2, 3, 4])) == 3)
ok("7 over [2,3,4] is a quad + a triple", sig(enumerate_mixes(7, [2, 3, 4])[0]) == (4, 3))
ok("5 over [2,3] is a triple + a double", sig(enumerate_mixes(5, [2, 3])[0]) == (3, 2))
ok("4 over [2,3,4] is a single quad", sig(enumerate_mixes(4, [2, 3, 4])[0]) == (4,))
ok("a solo traveler gets one room", sig(enumerate_mixes(1, [2, 3])[0]) == (2,))
ok("the solo traveler's spare bed counts as waste",
   enumerate_mixes(1, [2, 3])[0]["wasted_beds"] == 1)

ok("rooms are grouped largest-first",
   enumerate_mixes(10, [2, 3, 4])[0]["rooms"]
   == [{"sleeps": 4, "count": 1}, {"sleeps": 3, "count": 2}])
ok("room_count matches the expansion",
   all(m["room_count"] == len(expand_mix(m)) for m in all10))
ok("capacity matches the expansion",
   all(m["capacity"] == sum(expand_mix(m)) for m in all10))

ok("no capacities yields nothing", enumerate_mixes(6, []) == [])
ok("zero group size yields nothing", enumerate_mixes(0, [2, 3]) == [])
ok("junk capacities are ignored", sig(enumerate_mixes(4, [0, -2, 4])[0]) == (4,))

try:
    enumerate_mixes(41, [2, 3, 4])
    raised = False
except ValueError:
    raised = True
ok("a group over 40 is refused, not enumerated", raised)

first = json.dumps(enumerate_mixes(10, [2, 3, 4], max_results=None))
ok("deterministic across 100 runs",
   all(json.dumps(enumerate_mixes(10, [2, 3, 4], max_results=None)) == first
       for _ in range(100)))
ok("capacity order does not change the answer",
   json.dumps(enumerate_mixes(10, [4, 2, 3], max_results=None)) == first)
ok("duplicate capacities do not change the answer",
   json.dumps(enumerate_mixes(10, [2, 2, 3, 4, 4], max_results=None)) == first)


# ---------------------------------------------------------------------------
# 3. mutual_pairs — reciprocity is the whole rule
# ---------------------------------------------------------------------------
print("\nmutual_pairs:")
ok("reciprocal picks make a pair",
   mutual_pairs([("a", "b"), ("b", "a")]) == [("a", "b")])
ok("a one-way pick makes nothing", mutual_pairs([("a", "b")]) == [])
ok("pairs are ordered and sorted",
   mutual_pairs([("d", "c"), ("c", "d"), ("b", "a"), ("a", "b")])
   == [("a", "b"), ("c", "d")])
ok("self-picks are ignored", mutual_pairs([("a", "a")]) == [])
ok("duplicate edges collapse",
   mutual_pairs([("a", "b"), ("a", "b"), ("b", "a")]) == [("a", "b")])


# ---------------------------------------------------------------------------
# 4. propose_assignments — mutual pairs are inviolable
# ---------------------------------------------------------------------------
print("\npropose_assignments:")
MIX_2x2 = enumerate_mixes(4, [2])[0]          # (2, 2)
MEMBERS4 = ["ana", "bia", "carla", "dan"]


def room_of(result, user):
    return next(i for i, r in enumerate(result["rooms"]) if user in r)


def together(result, a, b):
    return room_of(result, a) == room_of(result, b)


res = propose_assignments(MIX_2x2, MEMBERS4, [("ana", "dan")], {})
ok("a mutual pair shares a room", together(res, "ana", "dan"))
ok("everyone gets exactly one bed",
   sorted(u for r in res["rooms"] for u in r) == sorted(MEMBERS4))
ok("no room exceeds its capacity",
   all(len(r) <= s for r, s in zip(res["rooms"], expand_mix(MIX_2x2))))
ok("a satisfied pair raises no conflict", res["conflicts"] == [])

# The pair must survive preferences that pull its members apart.
res = propose_assignments(
    MIX_2x2, MEMBERS4, [("ana", "dan")],
    {"ana": ["bia"], "dan": ["carla"], "bia": ["ana"], "carla": ["dan"]},
)
ok("a mutual pair is never split by competing preferences",
   together(res, "ana", "dan"))

# One-way preference, honoured when a bed is free.
res = propose_assignments(MIX_2x2, MEMBERS4, [], {"ana": ["carla"]})
ok("a one-way preference is honoured when a bed is free",
   together(res, "ana", "carla"))
ok("honoured preferences raise no conflict", res["conflicts"] == [])

# "Ana and Bia both picked Carla" — the spec's own example.
res = propose_assignments(MIX_2x2, MEMBERS4, [], {"ana": ["carla"], "bia": ["carla"]})
ok("the loser of a contested pick is still seated",
   sorted(u for r in res["rooms"] for u in r) == sorted(MEMBERS4))
unmet = [c for c in res["conflicts"] if c["kind"] == "preference_unmet"]
ok("an unmet preference surfaces a conflict", len(unmet) == 1, res["conflicts"])
ok("the conflict names who missed out and who they wanted",
   unmet[0]["user"] == "bia" and unmet[0]["wanted"] == ["carla"], unmet)

# A mutual component larger than the biggest room: physics wins, loudly.
MIX_3_2 = enumerate_mixes(5, [2, 3])[0]       # (3, 2)
MEMBERS5 = ["ana", "bia", "carla", "dan", "eve"]
res = propose_assignments(
    MIX_3_2, MEMBERS5,
    [("ana", "bia"), ("bia", "carla"), ("carla", "dan")], {},
)
oversized = [c for c in res["conflicts"] if c["kind"] == "oversized_group"]
ok("a mutual group too big for any room raises a conflict", len(oversized) == 1)
ok("the conflict names the whole component",
   oversized[0]["members"] == ["ana", "bia", "carla", "dan"], oversized)
ok("the conflict reports the biggest room", oversized[0]["max_room"] == 3)
ok("its members are still given beds",
   sorted(u for r in res["rooms"] for u in r) == sorted(MEMBERS5))

# Two trios into a quad and a double: each fits a room alone, neither fits once
# the other is seated. Best-fit cannot save this; only a different mix can.
MIX_4_2 = enumerate_mixes(6, [2, 4])[0]       # (4, 2)
MEMBERS6 = ["ana", "bia", "carla", "dan", "eve", "finn"]
res = propose_assignments(
    MIX_4_2, MEMBERS6,
    [("ana", "bia"), ("bia", "carla"), ("dan", "eve"), ("eve", "finn")], {},
)
unplaceable = [c for c in res["conflicts"] if c["kind"] == "group_unplaceable"]
ok("a mutual group with no room left raises a conflict, not a crash",
   len(unplaceable) == 1, res["conflicts"])
ok("the unplaceable group is the one that lost the race",
   unplaceable[0]["members"] == ["dan", "eve", "finn"], unplaceable)
ok("the group that did fit is still whole",
   together(res, "ana", "bia") and together(res, "bia", "carla"))
ok("everyone still gets a bed",
   sorted(u for r in res["rooms"] for u in r) == sorted(MEMBERS6))
ok("an unplaceable group is not called oversized",
   not any(c["kind"] == "oversized_group" for c in res["conflicts"]))

# Best-fit earns its keep: two trios DO fit a quad and a triple.
MIX_4_3 = {"rooms": [{"sleeps": 4, "count": 1}, {"sleeps": 3, "count": 1}],
           "room_count": 2, "capacity": 7, "wasted_beds": 1}
res = propose_assignments(
    MIX_4_3, MEMBERS6,
    [("ana", "bia"), ("bia", "carla"), ("dan", "eve"), ("eve", "finn")], {},
)
ok("best-fit seats two trios in a quad and a triple", res["conflicts"] == [])
ok("both trios stay whole",
   together(res, "ana", "carla") and together(res, "dan", "finn"))
ok("the two trios are not in the same room", not together(res, "ana", "dan"))

# A chain that does fit stays whole.
res = propose_assignments(MIX_3_2, MEMBERS5,
                          [("ana", "bia"), ("bia", "carla")], {})
ok("a 3-person mutual chain lands in the triple",
   together(res, "ana", "bia") and together(res, "bia", "carla"))
ok("a fitting chain raises no oversized conflict",
   not any(c["kind"] == "oversized_group" for c in res["conflicts"]))

# Non-members and self-references must not corrupt the seating.
res = propose_assignments(MIX_2x2, MEMBERS4, [("ana", "ghost")],
                          {"ana": ["ghost", "ana"], "zed": ["bia"]})
ok("preferences naming non-members are ignored",
   sorted(u for r in res["rooms"] for u in r) == sorted(MEMBERS4))

# Rooms with spare beds are legal; the group just doesn't fill them.
MIX_3_3 = {"rooms": [{"sleeps": 3, "count": 2}], "room_count": 2,
           "capacity": 6, "wasted_beds": 1}
res = propose_assignments(MIX_3_3, MEMBERS5, [], {})
ok("a spare bed is left empty, not double-booked",
   sum(len(r) for r in res["rooms"]) == 5 and len(res["rooms"]) == 2)

try:
    propose_assignments(MIX_2x2, MEMBERS5, [], {})
    raised = False
except ValueError:
    raised = True
ok("a mix too small for the group is refused", raised)

first = json.dumps(propose_assignments(
    MIX_3_2, MEMBERS5, [("ana", "bia")], {"eve": ["dan"], "carla": ["eve"]}))
ok("deterministic across 100 runs",
   all(json.dumps(propose_assignments(
       MIX_3_2, MEMBERS5, [("ana", "bia")], {"eve": ["dan"], "carla": ["eve"]})) == first
       for _ in range(100)))
ok("member order does not change the answer",
   json.dumps(propose_assignments(
       MIX_3_2, list(reversed(MEMBERS5)), [("bia", "ana")],
       {"carla": ["eve"], "eve": ["dan"]})) == first)


print(f"\nall {passed} checks passed")
