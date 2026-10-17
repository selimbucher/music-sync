"""Cross-service identity.

Spotify stopped exposing ISRC/UPC in February 2026; Apple still does (via the
catalog). So identity is anchored on the Apple-side code, and matching is
directional:

  Apple -> Spotify   code search on Spotify (``isrc:`` / ``upc:`` filter),
                     disambiguated by duration.
  Spotify -> Apple   text search on Apple, then round-trip every candidate's
                     code through Spotify's code search and accept the one
                     that resolves back to the originating Spotify id. Exact,
                     without Spotify ever revealing the code.

Anything that does not resolve stays one-sided and quarantined -- never
guessed. A wrong match is worse than a missing one, because the merge would
later mirror its deletion.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import ALBUM, APPLE, ARTIST, SPOTIFY, TRACK, Item, normalize, primary_artist
from .providers.base import Provider

log = logging.getLogger(__name__)

# The two services report the same recording a few seconds apart at times;
# a different edit or a live take differs by far more.
DURATION_TOLERANCE_MS = 5000


@dataclass
class Match:
    identity: str
    far: Item


def identity_of(item: Item) -> str:
    """Stable identity for an item whose code is known, else provisional."""
    if item.code:
        return f"{item.kind}:{item.code}"
    return f"{item.kind}:{item.side}:{item.native_id}"


def _duration_ok(a: Item, b: Item) -> bool:
    if a.duration_ms is None or b.duration_ms is None:
        return True
    return abs(a.duration_ms - b.duration_ms) <= DURATION_TOLERANCE_MS


def _closest(origin: Item, candidates: list[Item]) -> Item | None:
    """Among candidates sharing a code, prefer the closest duration, then the
    normalized-title match. Reject everything outside tolerance."""
    ok = [c for c in candidates if _duration_ok(origin, c)]
    if not ok:
        return None
    if origin.duration_ms is None:
        titled = [c for c in ok if normalize(c.title) == normalize(origin.title)]
        return (titled or ok)[0]
    return min(ok, key=lambda c: abs((c.duration_ms or 0) - origin.duration_ms))


def resolve_apple_to_spotify(item: Item, spotify: Provider) -> Match | None:
    """``item`` is an enriched Apple item (code present)."""
    if not item.code:
        return None
    found = spotify.lookup_codes(item.kind, [item.code]).get(item.code, [])
    best = _closest(item, found)
    return Match(identity_of(item), best) if best else None


# Spotify's search endpoint has a small daily quota in Development Mode, so a
# round trip is the last resort, for at most this many candidates.
ROUND_TRIP_MAX = 3


def resolve_spotify_to_apple(item: Item, apple: Provider, spotify: Provider) -> Match | None:
    """``item`` is a Spotify item with no code.

    Apple catalog candidates are narrowed to the same primary artist and core
    title within duration tolerance. One left: accepted without a Spotify
    search. Several: the exact title wins, then the closest duration. None
    left by name: a round trip through Spotify's code search over the closest
    few, which spends quota and may raise SearchQuota (the caller defers)."""
    if item.kind == ARTIST:
        return _resolve_artist(item, apple)
    found = apple.search(item.kind, primary_artist(item.artist), item.title)
    candidates = [c for c in apple.enrich(found) if c.code and _duration_ok(item, c)]
    if not candidates:
        return None
    same = [
        c for c in candidates
        if normalize(c.title) == normalize(item.title)
        and normalize(primary_artist(c.artist)) == normalize(primary_artist(item.artist))
    ]
    if same:
        exact = [c for c in same if c.title.casefold() == item.title.casefold()] or same
        best = min(exact, key=lambda c: abs((c.duration_ms or 0) - (item.duration_ms or 0)))
        return Match(identity_of(best), best)
    close = sorted(candidates, key=lambda c: abs((c.duration_ms or 0) - (item.duration_ms or 0)))[:ROUND_TRIP_MAX]
    back = spotify.lookup_codes(item.kind, list({c.code for c in close}))
    for cand in close:
        if any(b.native_id == item.native_id for b in back.get(cand.code, [])):
            return Match(identity_of(cand), cand)
    return None


def _resolve_artist(item: Item, far: Provider) -> Match | None:
    hits = far.search(ARTIST, item.artist or item.title, "")
    want = normalize(item.artist or item.title)
    exact = [h for h in hits if normalize(h.artist or h.title) == want]
    if len(exact) == 1:
        return Match(f"{ARTIST}:{want}", exact[0])
    return None


def resolve(item: Item, apple: Provider, spotify: Provider) -> Match | None:
    """Find ``item``'s counterpart on the other side, or None."""
    if item.side == APPLE:
        if item.kind == ARTIST:
            return _resolve_artist(item, spotify)
        return resolve_apple_to_spotify(item, spotify)
    if item.side == SPOTIFY:
        return resolve_spotify_to_apple(item, apple, spotify)
    raise ValueError(item.side)
