"""Three-way merge and its safety rails.

Two modes:

  SEED   one-way. The master side is authoritative and is never written to;
         the mirror is made to match it exactly, including removals. Used for
         the first run of a collection so the two libraries are not unioned.

  MERGE  bidirectional. An item's fate is decided against ``last_known``:
         absent from a side AND present in last_known means the user removed
         it there; absent from both but present in last_known means it is gone.

Invariants that keep a sync from eating a library:

  1. Deletions are only ever proposed from a listing the provider reported as
     complete. A truncated or errored page aborts the collection.
  2. ``last_known`` is written from confirmed post-apply state, never from the
     plan. An add that failed must not enter it, or the next run reads that
     failure as a deliberate removal and deletes the original.
  3. A delete batch above the configured share/count of a collection stops the
     collection and is reported for review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import APPLE, SPOTIFY, Item, other_side


class Mode(Enum):
    SEED = "seed"
    MERGE = "merge"


class Abort(Exception):
    """Raised when a collection must not be applied."""


@dataclass
class Plan:
    collection: str
    label: str
    mode: Mode
    # side -> identities to add to / remove from that side
    add: dict[str, set[str]] = field(default_factory=lambda: {APPLE: set(), SPOTIFY: set()})
    remove: dict[str, set[str]] = field(default_factory=lambda: {APPLE: set(), SPOTIFY: set()})
    # Identities present on both sides already; carried into last_known as-is.
    stable: set[str] = field(default_factory=set)
    blocked: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(self.add.values()) and not any(self.remove.values())

    def counts(self) -> dict[str, int]:
        return {
            "add_apple": len(self.add[APPLE]),
            "add_spotify": len(self.add[SPOTIFY]),
            "del_apple": len(self.remove[APPLE]),
            "del_spotify": len(self.remove[SPOTIFY]),
        }

    def summary(self) -> str:
        c = self.counts()
        return (
            f"{self.label}: "
            f"+{c['add_apple']}/-{c['del_apple']} apple, "
            f"+{c['add_spotify']}/-{c['del_spotify']} spotify"
        )


@dataclass
class Guard:
    """Refuse implausibly large deletions rather than mirror a bad read."""

    max_delete_ratio: float = 0.2
    max_delete_count: int = 50
    floor: int = 5  # below this many items, ratio is meaningless

    def check(self, plan: Plan, sizes: dict[str, int]) -> None:
        for side in (APPLE, SPOTIFY):
            n = len(plan.remove[side])
            if not n:
                continue
            size = sizes.get(side, 0)
            if n > self.max_delete_count:
                raise Abort(
                    f"{plan.label}: {n} deletions on {side} exceeds the cap of "
                    f"{self.max_delete_count}"
                )
            if size >= self.floor and n / size > self.max_delete_ratio:
                raise Abort(
                    f"{plan.label}: {n} of {size} items ({n / size:.0%}) would be "
                    f"deleted on {side}, over the {self.max_delete_ratio:.0%} limit"
                )


def plan_seed(
    collection: str,
    label: str,
    master: str,
    master_ids: set[str],
    mirror_ids: set[str],
) -> Plan:
    """Make the mirror match the master. The master is never written to."""
    mirror = other_side(master)
    p = Plan(collection=collection, label=label, mode=Mode.SEED)
    p.add[mirror] = master_ids - mirror_ids
    p.remove[mirror] = mirror_ids - master_ids
    p.stable = master_ids & mirror_ids
    return p


def plan_merge(
    collection: str,
    label: str,
    apple_ids: set[str],
    spotify_ids: set[str],
    last_known: set[str],
) -> Plan:
    """Bidirectional three-way merge."""
    p = Plan(collection=collection, label=label, mode=Mode.MERGE)

    only_apple = apple_ids - spotify_ids
    only_spotify = spotify_ids - apple_ids

    # Present on one side and not in last_known -> added there since last run.
    p.add[SPOTIFY] = only_apple - last_known
    p.add[APPLE] = only_spotify - last_known

    # Present on one side, in last_known, gone from the other -> removed there.
    p.remove[APPLE] = only_apple & last_known
    p.remove[SPOTIFY] = only_spotify & last_known

    p.stable = apple_ids & spotify_ids
    return p


def suppress_quarantined(plan: Plan, state, collection: str) -> Plan:
    """Drop adds still inside their retry backoff.

    A suppressed add stays out of last_known too, so the item keeps its
    one-sided status instead of becoming a phantom deletion next run.
    """
    for side in (APPLE, SPOTIFY):
        keep = set()
        for identity in plan.add[side]:
            if state.is_suppressed(collection, identity, side):
                plan.blocked.append(identity)
            else:
                keep.add(identity)
        plan.add[side] = keep
    return plan


def confirmed_last_known(plan: Plan, applied: dict[str, set[str]]) -> set[str]:
    """last_known for the next run, from what actually landed.

    ``applied[side]`` is the set of identities the provider confirmed it added
    to ``side``. Anything that failed, was suppressed or was blocked is left
    out deliberately: it stays a one-sided item and gets retried, rather than
    being mistaken for a user deletion.
    """
    survived = set(plan.stable)
    survived |= applied.get(APPLE, set()) & plan.add[APPLE]
    survived |= applied.get(SPOTIFY, set()) & plan.add[SPOTIFY]
    # Anything deliberately removed this run is gone from both sides.
    survived -= plan.remove[APPLE] | plan.remove[SPOTIFY]
    return survived


def require_complete(collection_complete: dict[str, bool], label: str) -> None:
    """Invariant 1. Any incomplete listing forbids deletions for the whole set."""
    missing = [s for s, ok in collection_complete.items() if not ok]
    if missing:
        raise Abort(
            f"{label}: listing incomplete on {', '.join(missing)}; "
            "skipping to avoid mirroring a partial read as deletions"
        )
