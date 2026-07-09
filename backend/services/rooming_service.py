"""Who sleeps where — the pure half of rooming (booking architecture §5).

Phase A guessed: `rooms_needed = ceil(group_size / 2)`, two travelers per room,
rounding up. A group of seven silently booked four doubles and nobody chose a
roommate. This module replaces the guess with a decision the group actually makes.

Two things live here, and both are pure:

  enumerate_mixes()      which room configurations can seat N people, given the
                         hotel's real capacities (a quad + two triples, or five
                         doubles, ...). Ranked structurally, never by price.
  propose_assignments()  who goes in which room, honouring roommate picks.

**Prices are deliberately absent.** LiteAPI returns one `offerRetailRate` for the
whole `occupancies` array, never a per-room figure, so a mix can only be priced by
asking for that exact mix (see hotel_service.price_room_mixes). Ranking here is
therefore structural — fewest wasted beds, then fewest rooms — and the caller
prices the handful of survivors. Keeping this module free of I/O is what lets the
solver be exhaustively tested and lets pricing fail without breaking the board.

The one inviolable rule: **a mutual roommate pair is never split.** If two people
both picked each other they share a room, and if that transitively binds a group
larger than the biggest room, the solver says so instead of quietly breaking it.
"""

_MAX_GROUP = 40


def stay_key(stay: dict) -> str:
    """The stable identity of a stay inside the plan JSON.

    Multi-city trips carry plan["stays"], one per city; single-city trips carry
    plan["stay"] with no city at all. This is the same `city or name` identity
    ai_service._reattach_heavy uses to find a stay after an AI revision, and the
    two must not drift — plan.rooming is keyed by it."""
    return ((stay.get("city") or stay.get("name") or "") or "").strip()


def expand_mix(mix: dict) -> list[int]:
    """A grouped mix -> one capacity per room, largest first.

    plan.rooming.assignments is indexed by this expansion, so its order is part
    of the stored data: room 0 is always the biggest."""
    return [r["sleeps"] for r in mix["rooms"] for _ in range(r["count"])]


def _group_rooms(sizes: list[int]) -> list[dict]:
    """[4, 3, 3] -> [{"sleeps": 4, "count": 1}, {"sleeps": 3, "count": 2}]"""
    out: list[dict] = []
    for s in sizes:
        if out and out[-1]["sleeps"] == s:
            out[-1]["count"] += 1
        else:
            out.append({"sleeps": s, "count": 1})
    return out


def enumerate_mixes(group_size: int, capacities: list[int],
                    *, max_results: int | None = 3) -> list[dict]:
    """Every room mix that seats the group with no room to spare, best first.

    `capacities` is the set of room sizes the property actually sells, from
    /data/hotel's rooms[].maxOccupancy. Mixes are built by adding rooms in
    non-increasing size until the group fits, which has a useful side effect:
    it becomes impossible to generate a mix with a removable room. Six doubles
    for ten people is never produced, because the fifth double already seated
    everyone.

    Ranked by wasted beds, then fewest rooms, then a size signature. The
    signature is a total order, so the same input always yields the same list —
    the leader's default choice must not wobble between page loads.

    Returns the top `max_results` mixes (all of them when None)."""
    if group_size <= 0:
        return []
    if group_size > _MAX_GROUP:
        raise ValueError(f"group of {group_size} is beyond rooming ({_MAX_GROUP} max)")
    caps = sorted({int(c) for c in capacities if c and int(c) > 0}, reverse=True)
    if not caps:
        return []

    found: list[list[int]] = []

    def walk(start: int, chosen: list[int], seated: int) -> None:
        if seated >= group_size:
            found.append(list(chosen))
            return
        for i in range(start, len(caps)):
            chosen.append(caps[i])
            walk(i, chosen, seated + caps[i])
            chosen.pop()

    walk(0, [], 0)

    found.sort(key=lambda sizes: (sum(sizes) - group_size, len(sizes), tuple(sizes)))
    if max_results is not None:
        found = found[:max_results]
    return [{
        "rooms": _group_rooms(sizes),
        "room_count": len(sizes),
        "capacity": sum(sizes),
        "wasted_beds": sum(sizes) - group_size,
    } for sizes in found]


def mutual_pairs(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Directed roommate picks -> the pairs where both people picked each other.

    Only reciprocity binds. A one-way pick is a wish, honoured when convenient;
    a mutual pick is a promise the solver must keep."""
    directed = {(a, b) for a, b in edges if a and b and a != b}
    return sorted({tuple(sorted((a, b))) for a, b in directed if (b, a) in directed})


def _components(pairs: list[tuple[str, str]], members: set[str]) -> list[list[str]]:
    """Union-find over mutual pairs. a↔b and b↔c means all three share a room —
    the promise is transitive whether the group meant it to be or not."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in members and b in members:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[min(ra, rb)] = max(ra, rb)

    groups: dict[str, list[str]] = {}
    for m in parent:
        groups.setdefault(find(m), []).append(m)
    return sorted((sorted(g) for g in groups.values() if len(g) > 1),
                  key=lambda g: (-len(g), g[0]))


def propose_assignments(mix: dict, members: list[str],
                        mutuals: list[tuple[str, str]],
                        preferences: dict[str, list[str]]) -> dict:
    """Seat the group. Returns {"rooms": [[user_id, ...], ...], "conflicts": [...]}.

    Order matters and is deliberate:
      1. Mutual groups land first, into the tightest room that fits them, so a
         pair is never crowded out by individuals who arrived earlier.
      2. Everyone else is seated in turn, each person pulling their one-way picks
         into the room with them while beds remain.
      3. Whatever is left fills the lowest-numbered free bed.

    Nothing is ever violated silently. A mutual group can fail to be honoured two
    ways, both physics rather than policy: it is bigger than any room
    (`oversized_group`), or it would fit a room but the earlier groups already took
    the space — two trios cannot both sit in a quad and a double
    (`group_unplaceable`). Either way its members are seated as individuals and the
    conflict names them. A one-way pick that didn't land raises `preference_unmet`.
    All three are for the leader to see and fix, by changing the mix or the picks."""
    sizes = expand_mix(mix)
    roster = sorted(set(members))
    if len(roster) > sum(sizes):
        raise ValueError(f"mix seats {sum(sizes)}, group is {len(roster)}")

    rooms: list[list[str]] = [[] for _ in sizes]
    placed: dict[str, int] = {}

    def seat(user: str, idx: int) -> None:
        rooms[idx].append(user)
        placed[user] = idx

    def free(idx: int) -> int:
        return sizes[idx] - len(rooms[idx])

    # Preferences, cleaned: no self-picks, no ghosts, deterministic order.
    wants = {m: sorted({p for p in preferences.get(m, []) if p in set(roster) and p != m})
             for m in roster}

    conflicts: list[dict] = []
    biggest = max(sizes)

    # 1. Mutual groups, largest first, into the tightest room that holds them.
    #    Best-fit, not first-fit: two trios and rooms of 4 and 3 only work if the
    #    first trio takes the 3.
    for group in _components(mutuals, set(roster)):
        if len(group) > biggest:
            conflicts.append({"kind": "oversized_group",
                              "members": group, "max_room": biggest})
            continue  # seated as individuals below
        fits = [i for i in range(len(rooms)) if free(i) >= len(group)]
        if not fits:
            conflicts.append({"kind": "group_unplaceable", "members": group})
            continue
        target = min(fits, key=lambda i: (free(i), i))
        for m in group:
            seat(m, target)

    # 2 & 3. Everyone else, each pulling their picks along.
    for m in roster:
        if m in placed:
            continue
        with_friend = [i for i in range(len(rooms))
                       if free(i) > 0 and any(f in rooms[i] for f in wants[m])]
        target = with_friend[0] if with_friend else next(
            i for i in range(len(rooms)) if free(i) > 0)
        seat(m, target)
        for friend in wants[m]:
            if friend not in placed and free(target) > 0:
                seat(friend, target)

    for m in roster:
        if wants[m] and not any(f in rooms[placed[m]] for f in wants[m]):
            conflicts.append({"kind": "preference_unmet", "user": m, "wanted": wants[m]})

    return {"rooms": [sorted(r) for r in rooms], "conflicts": conflicts}
