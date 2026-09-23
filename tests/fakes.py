"""In-memory providers sharing one 'world' of recordings.

world: isrc -> dict(title, artist, duration_ms, apple, spotify)
Apple library rows get per-context ids ("i.<n>") like the real API, with the
catalog id in ``catalog_id``; ISRC only appears after ``enrich``. Spotify rows
never carry a code, like the real API since Feb 2026.
"""
from __future__ import annotations

import itertools

from music_sync.models import ALBUM, APPLE, SPOTIFY, TRACK, Item, normalize
from music_sync.providers.base import AuthError, Listing, PlaylistRef

_seq = itertools.count(1)


class Fake:
    def __init__(self, side: str, world: dict):
        self.side = side
        self.world = world
        self.playlists_: dict[str, dict] = {}   # id -> {name, items: [native ids]}
        self.liked_: list[str] = []
        self.albums_: list[str] = []
        self.calls: list[tuple] = []
        self.complete = True
        self.dead = False
        self.fail_add = False

    # -- helpers ------------------------------------------------------------
    def _rec(self, native: str) -> tuple[str, dict] | None:
        for isrc, r in self.world.items():
            if r[self.side] == native:
                return isrc, r
        return None

    def _item(self, native: str, position=None) -> Item:
        found = self._rec(native)
        if not found:
            return Item(kind=TRACK, side=self.side, native_id=native, title=f"unknown {native}", artist="?", position=position)
        isrc, r = found
        if self.side == APPLE:
            return Item(kind=TRACK, side=APPLE, native_id=f"i.{next(_seq)}", title=r["title"], artist=r["artist"],
                        duration_ms=r["duration_ms"], position=position).with_catalog(native)
        return Item(kind=TRACK, side=SPOTIFY, native_id=native, title=r["title"], artist=r["artist"],
                    duration_ms=r["duration_ms"], position=position)

    def _listing(self, natives: list[str]) -> Listing:
        if not self.complete:
            return Listing(items=[], complete=False, error="truncated")
        return Listing(items=[self._item(n, i) for i, n in enumerate(natives)], complete=True)

    def _native_of(self, it: Item) -> str:
        if self.side == APPLE:
            return it.catalog_id or it.native_id
        return it.native_id

    # -- Provider surface ----------------------------------------------------
    def probe(self):
        if self.dead:
            raise AuthError(f"{self.side}: dead")

    def playlists(self):
        return [PlaylistRef(pid, p["name"]) for pid, p in self.playlists_.items()]

    def playlist_items(self, pid):
        return self._listing(self.playlists_[pid]["items"])

    def liked(self):
        return self._listing(self.liked_)

    def albums(self):
        return Listing(items=[], complete=True)

    def enrich(self, items):
        if self.side != APPLE:
            return items
        out = []
        for it in items:
            found = self._rec(it.catalog_id) if it.catalog_id else None
            out.append(it.enriched(isrc=found[0]) if found else it)
        return out

    def lookup_codes(self, kind, codes):
        out = {}
        for c in codes:
            r = self.world.get(c)
            if r and r.get(self.side):
                out[c] = [Item(kind=TRACK, side=self.side, native_id=r[self.side], title=r["title"],
                               artist=r["artist"], duration_ms=r["duration_ms"],
                               isrc=c if self.side == APPLE else None).with_catalog(r[self.side] if self.side == APPLE else None)]
        return out

    def search(self, kind, artist, title):
        hits = []
        for c, r in self.world.items():
            if r.get(self.side) and normalize(r["title"]) == normalize(title):
                hits.append(Item(kind=TRACK, side=self.side, native_id=r[self.side], title=r["title"], artist=r["artist"],
                                 duration_ms=r["duration_ms"], isrc=c if self.side == APPLE else None
                                 ).with_catalog(r[self.side] if self.side == APPLE else None))
        return hits

    def create_playlist(self, name, description=""):
        pid = f"{self.side[:2]}-pl-{next(_seq)}"
        self.playlists_[pid] = {"name": name, "items": []}
        self.calls.append(("create_playlist", name))
        return pid

    def rename_playlist(self, pid, name):
        self.playlists_[pid]["name"] = name
        self.calls.append(("rename_playlist", pid, name))

    def delete_playlist(self, pid):
        del self.playlists_[pid]
        self.calls.append(("delete_playlist", pid))

    def add_to_playlist(self, pid, items):
        if self.fail_add:
            raise RuntimeError("boom")
        for it in items:
            self.playlists_[pid]["items"].append(self._native_of(it))
        self.calls.append(("add", pid, [self._native_of(i) for i in items]))

    def remove_from_playlist(self, pid, items):
        for it in items:
            self.playlists_[pid]["items"].remove(self._native_of(it))
        self.calls.append(("remove", pid, [self._native_of(i) for i in items]))

    def replace_playlist_items(self, pid, items):
        self.playlists_[pid]["items"] = [self._native_of(i) for i in items]
        self.calls.append(("replace", pid, [self._native_of(i) for i in items]))

    def like(self, items):
        if self.fail_add:
            raise RuntimeError("boom")
        self.liked_ += [self._native_of(i) for i in items]
        self.calls.append(("like", [self._native_of(i) for i in items]))

    def unlike(self, items):
        for it in items:
            self.liked_.remove(self._native_of(it))
        self.calls.append(("unlike", [self._native_of(i) for i in items]))

    def save_albums(self, items):
        pass

    def unsave_albums(self, items):
        pass


def world(*rows):
    """rows: (isrc, title, artist, duration_ms, apple_catalog_id, spotify_id)"""
    return {r[0]: {"title": r[1], "artist": r[2], "duration_ms": r[3], "apple": r[4], "spotify": r[5]} for r in rows}
