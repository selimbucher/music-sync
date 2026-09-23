"""Apple Music via the web player's host, ``amp-api.music.apple.com``.

The public ``api.music.apple.com`` can add but not remove; the web player's
host accepts the same paths plus DELETE. It only honours the anonymous
developer token embedded in music.apple.com's JS bundle (``iss: AMPWebPlay``,
~70-day rotation, harvested here automatically) together with the user's
``media-user-token`` cookie (~6 months, no refresh flow; a human pastes a new
one). Every request carries ``Origin: https://music.apple.com`` because the
token is origin-bound. Never send ``x-apple-client-version``: amp-api 500s.

"Liked songs" is either the library (Add to Library, +; ``liked_mode``
"library") or Favorites (the star; "favorites"). Favorites are read by paging
the library with the documented ``inFavorites`` attribute, written with the
documented ``POST /me/favorites``, and removed via ``DELETE /me/favorites``
with the ratings endpoint as fallback (the delete is not in the public docs).

Quirks handled below:
  * ``GET .../playlists/{id}/tracks`` is 404 for an empty playlist.
  * Playlist tracks are removed by their *relationship* id (``i.xxx``), not
    the catalog id, with ``?ids[library-songs]=..&mode=all`` and no body.
  * Library rows carry no ISRC. ``include=catalog`` inlines the catalog song
    (ISRC, duration); ``enrich`` fetches it for rows that came without one.
    Local/uploaded tracks have no catalog id and are unmatchable by design.
  * 429 arrives with no Retry-After on a rolling 60-minute window.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any

import requests

from ..models import ALBUM, APPLE, ARTIST, TRACK, Item
from .base import AuthError, Listing, PlaylistRef, Throttled

log = logging.getLogger(__name__)

AMP = "https://amp-api.music.apple.com/v1"
ORIGIN = "https://music.apple.com"
TIMEOUT = 30
PAGE = 100
CATALOG_BATCH = 100      # documented cap is 300; keep URLs short
ISRC_BATCH = 25          # documented cap for filter[isrc]
ADD_BATCH = 100
HARVEST_REFRESH_DAYS = 15
THROTTLE_COOLDOWN_S = 35 * 60

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
_BUNDLE_RE = re.compile(r"/assets/index[~-][^\"']+\.js")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _jwt_payload(token: str) -> dict[str, Any]:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def harvest_developer_token(session: requests.Session | None = None) -> tuple[str, int]:
    """Return (token, exp_unix) for the bundle's ``AMPWebPlay`` token."""
    s = session or requests.Session()
    page = s.get(f"{ORIGIN}/us/browse", headers=_UA, timeout=TIMEOUT)
    page.raise_for_status()
    m = _BUNDLE_RE.search(page.text)
    if not m:
        raise RuntimeError("could not locate the music.apple.com JS bundle")
    bundle = s.get(ORIGIN + m.group(0), headers=_UA, timeout=TIMEOUT)
    bundle.raise_for_status()
    best: tuple[str, int] | None = None
    for tok in set(_JWT_RE.findall(bundle.text)):
        try:
            p = _jwt_payload(tok)
        except Exception:
            continue
        if p.get("iss") == "AMPWebPlay" and "exp" in p:
            best = (tok, int(p["exp"]))
            break
        if best is None and "exp" in p:
            best = (tok, int(p["exp"]))
    if not best:
        raise RuntimeError("no developer token found in the bundle")
    return best


class Apple:
    side = APPLE

    def __init__(self, user_token: str, state, storefront: str | None = None, liked_mode: str = "library"):
        if liked_mode not in ("library", "favorites"):
            raise ValueError(f"liked_mode must be library or favorites, not {liked_mode!r}")
        self.user_token = user_token.strip()
        self.state = state
        self.liked_mode = liked_mode
        self.http = requests.Session()
        self._storefront = storefront
        self._dev: str | None = None

    # -- auth ---------------------------------------------------------------

    def developer_token(self) -> str:
        if self._dev:
            return self._dev
        cached = self.state.get_meta("apple_dev_token")
        if cached:
            tok, exp = json.loads(cached)["token"], json.loads(cached)["exp"]
            if exp - time.time() > HARVEST_REFRESH_DAYS * 86400:
                self._dev = tok
                return tok
        tok, exp = harvest_developer_token(self.http)
        self.state.set_meta("apple_dev_token", json.dumps({"token": tok, "exp": exp}))
        log.info("harvested Apple developer token, expires %s", time.strftime("%Y-%m-%d", time.gmtime(exp)))
        self._dev = tok
        return tok

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.developer_token()}",
            "Music-User-Token": self.user_token,
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            **_UA,
        }

    def _check_throttle(self) -> None:
        until = float(self.state.get_meta("apple_throttled_until", "0") or 0)
        if until > time.time():
            raise Throttled(f"apple: cooling down until {time.strftime('%H:%M', time.localtime(until))}")

    def _req(self, method: str, path: str, ok=(200, 201, 202, 204), **kw) -> requests.Response:
        self._check_throttle()
        url = path if path.startswith("http") else AMP + path
        r = self.http.request(method, url, headers=self._headers(), timeout=TIMEOUT, **kw)
        if r.status_code == 429:
            self.state.set_meta("apple_throttled_until", str(time.time() + THROTTLE_COOLDOWN_S))
            raise Throttled("apple: 429")
        if r.status_code in (401, 403):
            raise AuthError(f"apple: {r.status_code} on {method} {path}: {r.text[:200]}")
        if r.status_code not in ok:
            raise RuntimeError(f"apple: {r.status_code} on {method} {path}: {r.text[:300]}")
        return r

    def probe(self) -> None:
        self._req("GET", "/me/library/playlists", params={"limit": 1})

    def storefront(self) -> str:
        if not self._storefront:
            data = self._req("GET", "/me/storefront").json().get("data", [])
            self._storefront = data[0]["id"] if data else "us"
        return self._storefront

    # -- paging -------------------------------------------------------------

    # Apple's ``next`` link keeps offset/limit but drops these, so they must be
    # re-sent on every page or only the first 100 rows carry catalog data.
    _CARRY = ("include", "extend")

    def _pages(self, path: str, params: dict | None = None):
        """Yield ``data`` rows across ``next`` links. Raises on any failure so
        the caller marks the listing incomplete rather than short."""
        url: str | None = path
        first = True
        carry = {k: v for k, v in (params or {}).items() if k in self._CARRY}
        while url:
            r = self._req("GET", url, ok=(200, 404), params=params if first else (carry or None))
            first = False
            if r.status_code == 404:
                return  # empty relationship
            body = r.json()
            yield from body.get("data", [])
            nxt = body.get("next")
            url = ("https://amp-api.music.apple.com" + nxt) if nxt else None

    _LIB = {"limit": PAGE, "include": "catalog", "extend": "inFavorites"}

    def _listing(self, path: str, kind: str, params: dict | None = None,
                 keep=lambda row: True) -> Listing:
        try:
            items = [self._row(row, kind) for row in self._pages(path, params) if keep(row)]
            return Listing(items=[i for i in items if i], complete=True)
        except (AuthError, Throttled):
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("apple listing %s failed: %s", path, e)
            return Listing(items=[], complete=False, error=str(e))

    @staticmethod
    def _catalog_of(row: dict) -> dict | None:
        """The inlined catalog resource from ``include=catalog``, if any."""
        data = ((row.get("relationships") or {}).get("catalog") or {}).get("data") or []
        return data[0] if data else None

    @classmethod
    def _row(cls, row: dict, kind: str) -> Item | None:
        a = row.get("attributes", {})
        pp = a.get("playParams") or {}
        cat = cls._catalog_of(row)
        ca = (cat or {}).get("attributes", {})
        catalog = (cat or {}).get("id") or pp.get("catalogId") or (
            pp.get("id") if row.get("type", "").startswith("songs") else None)
        if kind == TRACK:
            return Item(
                kind=TRACK, side=APPLE,
                native_id=row["id"],                      # library/relationship id
                title=a.get("name", ""), artist=a.get("artistName", ""),
                duration_ms=ca.get("durationInMillis") or a.get("durationInMillis"),
                isrc=ca.get("isrc") or a.get("isrc"),
                position=None,
            ).with_catalog(catalog)
        if kind == ALBUM:
            return Item(
                kind=ALBUM, side=APPLE, native_id=row["id"],
                title=a.get("name", ""), artist=a.get("artistName", ""),
                upc=ca.get("upc") or a.get("upc"),
            ).with_catalog(catalog)
        if kind == ARTIST:
            return Item(kind=ARTIST, side=APPLE, native_id=row["id"], artist=a.get("name", ""))
        return None

    # -- reads --------------------------------------------------------------

    def playlists(self) -> list[PlaylistRef]:
        out = []
        for row in self._pages("/me/library/playlists", {"limit": PAGE}):
            a = row.get("attributes", {})
            out.append(PlaylistRef(
                native_id=row["id"], name=a.get("name", ""),
                editable=bool(a.get("canEdit", True)),
                description=(a.get("description") or {}).get("standard", ""),
            ))
        return out

    def playlist_items(self, playlist_id: str) -> Listing:
        lst = self._listing(f"/me/library/playlists/{playlist_id}/tracks", TRACK, self._LIB)
        # Positions are needed for nothing on Apple (removal is by id), but
        # keep ordering information for the journal.
        lst.items = [it.at(i) for i, it in enumerate(lst.items)]
        return lst

    def liked(self) -> Listing:
        if self.liked_mode == "library":
            return self._listing("/me/library/songs", TRACK, self._LIB)
        return self._listing("/me/library/songs", TRACK, self._LIB,
                             keep=lambda row: bool(row.get("attributes", {}).get("inFavorites")))

    def albums(self) -> Listing:
        """Starred albums. The library-albums list itself is derived (every
        album a single library song came from), so the star is the only
        signal that means "saved"."""
        return self._listing("/me/library/albums", ALBUM, self._LIB,
                             keep=lambda row: bool(row.get("attributes", {}).get("inFavorites")))

    def liked_signature(self) -> str | None:
        return None  # no cheap change signal on Apple; always list

    # -- matching primitives ------------------------------------------------

    def enrich(self, items: list[Item]) -> list[Item]:
        """Fill ISRC/UPC + duration from the catalog for library rows."""
        need = [it for it in items if not it.code and it.catalog_id]
        by_catalog: dict[str, dict] = {}
        sf = self.storefront()
        for kind, path in ((TRACK, "songs"), (ALBUM, "albums")):
            ids = list({it.catalog_id for it in need if it.kind == kind})
            for i in range(0, len(ids), CATALOG_BATCH):
                chunk = ids[i:i + CATALOG_BATCH]
                r = self._req("GET", f"/catalog/{sf}/{path}", params={"ids": ",".join(chunk)})
                for d in r.json().get("data", []):
                    by_catalog[d["id"]] = d.get("attributes", {})
        out = []
        for it in items:
            a = by_catalog.get(it.catalog_id or "")
            if a:
                it = it.enriched(
                    isrc=a.get("isrc"), upc=a.get("upc"),
                    duration_ms=a.get("durationInMillis") or it.duration_ms,
                )
            out.append(it)
        return out

    def lookup_codes(self, kind: str, codes: list[str]) -> dict[str, list[Item]]:
        sf = self.storefront()
        out: dict[str, list[Item]] = {}
        path, key = ("songs", "isrc") if kind == TRACK else ("albums", "upc")
        for i in range(0, len(codes), ISRC_BATCH):
            chunk = codes[i:i + ISRC_BATCH]
            r = self._req("GET", f"/catalog/{sf}/{path}", params={f"filter[{key}]": ",".join(chunk)})
            for d in r.json().get("data", []):
                it = self._catalog_item(d, kind)
                if it and it.code:
                    out.setdefault(it.code, []).append(it)
        return out

    def search(self, kind: str, artist: str, title: str) -> list[Item]:
        sf = self.storefront()
        types = {TRACK: "songs", ALBUM: "albums", ARTIST: "artists"}[kind]
        term = f"{artist} {title}".strip()
        r = self._req("GET", f"/catalog/{sf}/search", params={"term": term, "types": types, "limit": 10})
        data = r.json().get("results", {}).get(types, {}).get("data", [])
        return [it for it in (self._catalog_item(d, kind) for d in data) if it]

    @staticmethod
    def _catalog_item(d: dict, kind: str) -> Item | None:
        a = d.get("attributes", {})
        if kind == TRACK:
            return Item(kind=TRACK, side=APPLE, native_id=d["id"], title=a.get("name", ""),
                        artist=a.get("artistName", ""), isrc=a.get("isrc"),
                        duration_ms=a.get("durationInMillis")).with_catalog(d["id"])
        if kind == ALBUM:
            return Item(kind=ALBUM, side=APPLE, native_id=d["id"], title=a.get("name", ""),
                        artist=a.get("artistName", ""), upc=a.get("upc")).with_catalog(d["id"])
        if kind == ARTIST:
            return Item(kind=ARTIST, side=APPLE, native_id=d["id"], artist=a.get("name", ""))
        return None

    # -- writes -------------------------------------------------------------

    def create_playlist(self, name: str, description: str = "") -> str:
        attrs: dict[str, Any] = {"name": name}
        if description:
            attrs["description"] = description
        r = self._req("POST", "/me/library/playlists", json={"attributes": attrs})
        data = r.json().get("data", [])
        if not data:
            raise RuntimeError("apple: playlist create returned no id")
        return data[0]["id"]

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        self._req("PATCH", f"/me/library/playlists/{playlist_id}", json={"attributes": {"name": name}})

    def delete_playlist(self, playlist_id: str) -> None:
        self._req("DELETE", f"/me/library/playlists/{playlist_id}")

    def add_to_playlist(self, playlist_id: str, items: list[Item]) -> None:
        ids = [it.catalog_id or it.native_id for it in items]
        for i in range(0, len(ids), ADD_BATCH):
            data = [{"id": cid, "type": "songs"} for cid in ids[i:i + ADD_BATCH]]
            self._req("POST", f"/me/library/playlists/{playlist_id}/tracks", json={"data": data})

    def remove_from_playlist(self, playlist_id: str, items: list[Item]) -> None:
        for it in items:
            # it.native_id must be the relationship id from playlist_items().
            self._req("DELETE", f"/me/library/playlists/{playlist_id}/tracks",
                      params={"ids[library-songs]": it.native_id, "mode": "all"})

    def replace_playlist_items(self, playlist_id: str, items: list[Item]) -> None:
        raise NotImplementedError("Apple Music exposes no reorder; order is only mirrored onto Spotify")

    def like(self, items: list[Item]) -> None:
        ids = [it.catalog_id or it.native_id for it in items]
        if self.liked_mode == "library":
            for i in range(0, len(ids), ADD_BATCH):
                self._req("POST", "/me/library", params={"ids[songs]": ",".join(ids[i:i + ADD_BATCH])})
            return
        # Favorite (star). Apple adds favorited songs to the library itself.
        for i in range(0, len(ids), ADD_BATCH):
            self._req("POST", "/me/favorites", params={"ids[songs]": ",".join(ids[i:i + ADD_BATCH])})

    def unlike(self, items: list[Item]) -> None:
        if self.liked_mode == "library":
            for it in items:
                self._req("DELETE", f"/me/library/songs/{it.native_id}")
            return
        # Unfavorite. The library entry stays, as it does in the app.
        for it in items:
            cid = it.catalog_id or it.native_id
            r = self.http.request("DELETE", f"{AMP}/me/favorites", headers=self._headers(),
                                  params={"ids[songs]": cid}, timeout=TIMEOUT)
            if r.status_code in (200, 202, 204):
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"apple: {r.status_code} on DELETE /me/favorites")
            # Not public; fall back to clearing the rating, which the app treats as the star.
            self._req("DELETE", f"/me/ratings/songs/{cid}", ok=(200, 204, 404))

    def save_albums(self, items: list[Item]) -> None:
        """Star the album (which also puts it in the library)."""
        ids = [it.catalog_id or it.native_id for it in items]
        for i in range(0, len(ids), ADD_BATCH):
            self._req("POST", "/me/favorites", params={"ids[albums]": ",".join(ids[i:i + ADD_BATCH])})

    def unsave_albums(self, items: list[Item]) -> None:
        """Unstar; the library entry stays, as in the app."""
        for it in items:
            cid = it.catalog_id or it.native_id
            r = self.http.request("DELETE", f"{AMP}/me/favorites", headers=self._headers(),
                                  params={"ids[albums]": cid}, timeout=TIMEOUT)
            if r.status_code in (200, 202, 204):
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"apple: {r.status_code} on DELETE /me/favorites")
            self._req("DELETE", f"/me/ratings/albums/{cid}", ok=(200, 204, 404))
