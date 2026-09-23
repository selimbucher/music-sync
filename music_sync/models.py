"""Canonical item model shared by both providers."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace

APPLE = "apple"
SPOTIFY = "spotify"
SIDES = (APPLE, SPOTIFY)

TRACK = "track"
ALBUM = "album"
ARTIST = "artist"


def other_side(side: str) -> str:
    return SPOTIFY if side == APPLE else APPLE


_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")
_FEAT = re.compile(r"\s*(?:feat\.?|ft\.?|featuring|with)\s+.*$", re.I)
_NOISE = re.compile(
    r"\s*-\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|remix|mono|stereo|deluxe|"
    r"radio edit|single version|album version|bonus track|live)\b.*$",
    re.I,
)
_NONWORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold a title/artist to a comparable form.

    Strips parentheticals, featured-artist tails and remaster/edition noise,
    then accents and punctuation. Used only for the fuzzy fallback; ISRC/UPC
    matching never goes through here.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKD", text)
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = _PAREN.sub(" ", out)
    out = _NOISE.sub("", out)
    out = _FEAT.sub("", out)
    out = _NONWORD.sub(" ", out)
    return _SPACE.sub(" ", out).strip().lower()


def primary_artist(artist: str) -> str:
    """First credited artist only — the two services disagree on the rest."""
    for sep in ("&", ",", " x ", " X ", "/", " and "):
        if sep in artist:
            artist = artist.split(sep)[0]
    return artist.strip()


@dataclass(frozen=True)
class Item:
    """One track, album or artist as seen on one side."""

    kind: str
    native_id: str
    side: str
    title: str = ""
    artist: str = ""
    isrc: str | None = None
    upc: str | None = None
    duration_ms: int | None = None
    # Catalog id on Apple (library rows have their own ids); unused on Spotify.
    catalog_id: str | None = None
    # Index within a playlist listing, for the journal and for Spotify reorders.
    position: int | None = None

    def with_catalog(self, catalog_id: str | None) -> "Item":
        return replace(self, catalog_id=catalog_id) if catalog_id else self

    def at(self, position: int) -> "Item":
        return replace(self, position=position)

    def enriched(self, **fields) -> "Item":
        """Fill only the fields that are still empty."""
        current = {k: getattr(self, k) for k in fields}
        return replace(self, **{k: v for k, v in fields.items() if v is not None and not current[k]})

    @property
    def code(self) -> str | None:
        """The authoritative cross-service code, when the provider gave us one."""
        if self.kind == TRACK:
            return self.isrc.upper() if self.isrc else None
        if self.kind == ALBUM:
            return self.upc if self.upc else None
        return None

    @property
    def fuzzy_key(self) -> str:
        """Fallback identity: primary artist + title. Duration is checked
        separately with a tolerance; bucketing it here split near-boundary
        pairs of the same recording."""
        return f"{normalize(primary_artist(self.artist))}|{normalize(self.title)}"

    def describe(self) -> str:
        if self.kind == ARTIST:
            return self.artist or self.title
        return f"{self.artist} - {self.title}"


@dataclass
class Collection:
    """A syncable set of items: one playlist, or a whole library facet.

    ``key`` is stable across runs and is what state.py indexes on.
    """

    key: str
    kind: str
    label: str
    items: dict[str, list[Item]] = field(default_factory=dict)
    # Provider-native container ids, per side. None for library facets.
    container: dict[str, str | None] = field(default_factory=dict)
    # False when a listing was truncated or errored; blocks all deletions.
    complete: dict[str, bool] = field(default_factory=dict)
