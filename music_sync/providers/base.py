"""Provider interface. Both sides implement exactly this surface."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Item


class AuthError(Exception):
    """The session credential is dead or rejected. Needs a human."""


class Throttled(Exception):
    """Rate-limited. Abort the run; the caller persists a cool-down."""


@dataclass
class PlaylistRef:
    native_id: str
    name: str
    editable: bool = True
    description: str = ""


@dataclass
class Listing:
    """A collection read. ``complete`` is False on any truncation or error;
    merge.py refuses deletions against an incomplete listing."""

    items: list[Item] = field(default_factory=list)
    complete: bool = True
    error: str | None = None


class Provider(Protocol):
    side: str

    # -- session -------------------------------------------------------------
    def probe(self) -> None:
        """Raise AuthError if the stored credential no longer works."""
        ...

    # -- reads -------------------------------------------------------------
    def playlists(self) -> list[PlaylistRef]: ...
    def playlist_items(self, playlist_id: str) -> Listing: ...
    def liked(self) -> Listing: ...
    def albums(self) -> Listing: ...

    # -- matching primitives ------------------------------------------------
    def enrich(self, items: list[Item]) -> list[Item]:
        """Fill in cross-service codes (ISRC/UPC) and durations where the
        listing itself did not carry them. No-op where it did."""
        ...

    def lookup_codes(self, kind: str, codes: list[str]) -> dict[str, list[Item]]:
        """code -> candidate items on this side. Several per code is normal."""
        ...

    def search(self, kind: str, artist: str, title: str) -> list[Item]: ...

    # -- writes ------------------------------------------------------------
    def create_playlist(self, name: str, description: str = "") -> str: ...
    def rename_playlist(self, playlist_id: str, name: str) -> None: ...
    def delete_playlist(self, playlist_id: str) -> None: ...
    def add_to_playlist(self, playlist_id: str, items: list[Item]) -> None: ...
    def remove_from_playlist(self, playlist_id: str, items: list[Item]) -> None: ...
    def replace_playlist_items(self, playlist_id: str, items: list[Item]) -> None:
        """Set the playlist to exactly ``items`` in this order. Raises
        NotImplementedError where the service cannot reorder."""
        ...
    def like(self, items: list[Item]) -> None: ...
    def unlike(self, items: list[Item]) -> None: ...
    def save_albums(self, items: list[Item]) -> None: ...
    def unsave_albums(self, items: list[Item]) -> None: ...
