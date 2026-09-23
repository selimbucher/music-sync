"""Spotify Web API, post-February-2026 surface.

What changed and why it matters here:
  * ``external_ids`` (ISRC/UPC) is gone from track and album objects, so this
    side never yields a code; identity comes from the Apple side and the
    ``isrc:``/``upc:`` search filters, which still work (limit <= 10).
  * Playlist contents live at ``/playlists/{id}/items`` and each row nests the
    track under ``item`` (``track`` is deprecated).
  * Library writes are one endpoint, ``PUT``/``DELETE /me/library?uris=..``
    (query parameter, <= 40 URIs), covering tracks, albums and playlists.
  * A Development Mode app needs its owner on Premium and is capped at
    5 users; both fine for a personal tool.
  * 429 carries ``Retry-After``; short waits are honoured, long ones abort.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from ..models import ALBUM, SPOTIFY, TRACK, Item
from .base import AuthError, Listing, PlaylistRef, Throttled

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
ACCOUNTS = "https://accounts.spotify.com"
TIMEOUT = 30
PAGE = 50
ITEMS_BATCH = 100      # playlist items add/remove
LIBRARY_BATCH = 40     # /me/library uris
MAX_WAIT_S = 60

SCOPES = (
    "playlist-read-private playlist-modify-private playlist-modify-public "
    "user-library-read user-library-modify"
)


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return ACCOUNTS + "/authorize?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state,
    })


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    r = requests.post(ACCOUNTS + "/api/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
    }, auth=(client_id, client_secret), timeout=TIMEOUT)
    if r.status_code != 200:
        # Spotify says why: invalid_client = id/secret wrong, invalid_grant =
        # code used/expired or redirect_uri differs from the registered one.
        raise SystemExit(f"token exchange failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


class Spotify:
    side = SPOTIFY

    def __init__(self, client_id: str, client_secret: str, refresh_token_file: Path):
        self.client_id, self.client_secret = client_id, client_secret
        self.refresh_file = Path(refresh_token_file)
        self.http = requests.Session()
        self._access: str | None = None
        self._expires = 0.0
        self._me: str | None = None

    # -- auth ---------------------------------------------------------------

    def _refresh(self) -> None:
        token = self.refresh_file.read_text().strip()
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self.http.post(ACCOUNTS + "/api/token", data={
            "grant_type": "refresh_token", "refresh_token": token,
        }, headers={"Authorization": f"Basic {basic}"}, timeout=TIMEOUT)
        if r.status_code in (400, 401):
            raise AuthError(f"spotify: refresh rejected: {r.text[:200]}")
        r.raise_for_status()
        body = r.json()
        self._access = body["access_token"]
        self._expires = time.time() + int(body.get("expires_in", 3600)) - 60
        if body.get("refresh_token") and body["refresh_token"] != token:
            try:
                self.refresh_file.write_text(body["refresh_token"] + "\n")
            except OSError as e:
                log.warning("spotify rotated the refresh token but %s is not writable: %s", self.refresh_file, e)

    def _headers(self) -> dict[str, str]:
        if not self._access or time.time() > self._expires:
            self._refresh()
        return {"Authorization": f"Bearer {self._access}", "Content-Type": "application/json"}

    def _req(self, method: str, path: str, ok=(200, 201, 202, 204), **kw) -> requests.Response:
        url = path if path.startswith("http") else API + path
        for attempt in range(3):
            r = self.http.request(method, url, headers=self._headers(), timeout=TIMEOUT, **kw)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5"))
                if wait > MAX_WAIT_S:
                    raise Throttled(f"spotify: 429, retry-after {wait}s")
                log.info("spotify 429; sleeping %ss", wait)
                time.sleep(wait + 1)
                continue
            if r.status_code == 401:
                self._access = None
                if attempt == 0:
                    continue
                raise AuthError(f"spotify: 401 on {method} {path}: {r.text[:200]}")
            if r.status_code == 403:
                raise AuthError(f"spotify: 403 on {method} {path}: {r.text[:200]}")
            if r.status_code not in ok:
                raise RuntimeError(f"spotify: {r.status_code} on {method} {path}: {r.text[:300]}")
            return r
        raise Throttled("spotify: retries exhausted")

    def probe(self) -> None:
        self.me()

    def me(self) -> str:
        if not self._me:
            self._me = self._req("GET", "/me").json()["id"]
        return self._me

    # -- paging -------------------------------------------------------------

    def _pages(self, path: str, params: dict | None = None):
        url: str | None = path
        first = True
        while url:
            body = self._req("GET", url, params=params if first else None).json()
            first = False
            yield from body.get("items", [])
            url = body.get("next")

    def _listing(self, path: str, kind: str, nested: str, params: dict | None = None) -> Listing:
        try:
            items = []
            for pos, row in enumerate(self._pages(path, params)):
                obj = row.get(nested) or row.get("track") or row.get("album")
                it = self._item(obj, kind, pos, row.get("is_local", False))
                if it:
                    items.append(it)
            return Listing(items=items, complete=True)
        except (AuthError, Throttled):
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("spotify listing %s failed: %s", path, e)
            return Listing(items=[], complete=False, error=str(e))

    @staticmethod
    def _item(obj: dict | None, kind: str, pos: int | None = None, is_local: bool = False) -> Item | None:
        if not obj or not obj.get("id") or is_local:
            return None
        if kind == TRACK and obj.get("type") not in (None, "track"):
            return None  # episodes etc.
        artists = ", ".join(a.get("name", "") for a in obj.get("artists", []))
        if kind == TRACK:
            return Item(kind=TRACK, side=SPOTIFY, native_id=obj["id"], title=obj.get("name", ""),
                        artist=artists, duration_ms=obj.get("duration_ms"), position=pos)
        if kind == ALBUM:
            return Item(kind=ALBUM, side=SPOTIFY, native_id=obj["id"], title=obj.get("name", ""),
                        artist=artists)
        return None

    # -- reads --------------------------------------------------------------

    def playlists(self) -> list[PlaylistRef]:
        me = self.me()
        out = []
        for p in self._pages("/me/playlists", {"limit": PAGE}):
            out.append(PlaylistRef(
                native_id=p["id"], name=p.get("name", ""),
                editable=(p.get("owner") or {}).get("id") == me,
                description=p.get("description") or "",
            ))
        return out

    def playlist_items(self, playlist_id: str) -> Listing:
        fields = "next,items(is_local,item(id,uri,name,type,duration_ms,artists(name)))"
        return self._listing(f"/playlists/{playlist_id}/items", TRACK, "item",
                             {"limit": PAGE, "fields": fields, "additional_types": "track"})

    def liked(self) -> Listing:
        return self._listing("/me/tracks", TRACK, "track", {"limit": PAGE})

    def albums(self) -> Listing:
        return self._listing("/me/albums", ALBUM, "album", {"limit": PAGE})

    # -- matching primitives ------------------------------------------------

    def enrich(self, items: list[Item]) -> list[Item]:
        return items  # nothing to add: Spotify exposes no codes any more

    def lookup_codes(self, kind: str, codes: list[str]) -> dict[str, list[Item]]:
        out: dict[str, list[Item]] = {}
        typ, key, bucket = ("track", "isrc", "tracks") if kind == TRACK else ("album", "upc", "albums")
        for code in codes:
            body = self._req("GET", "/search", params={"q": f"{key}:{code}", "type": typ, "limit": 10}).json()
            hits = [self._item(o, kind) for o in body.get(bucket, {}).get("items", [])]
            out[code] = [h for h in hits if h]
        return out

    def search(self, kind: str, artist: str, title: str) -> list[Item]:
        typ, bucket = ("track", "tracks") if kind == TRACK else ("album", "albums")
        q = f'artist:"{artist}" {"track" if kind == TRACK else "album"}:"{title}"' if artist else title
        body = self._req("GET", "/search", params={"q": q, "type": typ, "limit": 10}).json()
        return [h for h in (self._item(o, kind) for o in body.get(bucket, {}).get("items", [])) if h]

    # -- writes -------------------------------------------------------------

    @staticmethod
    def _uri(kind: str, native_id: str) -> str:
        return f"spotify:{ {TRACK: 'track', ALBUM: 'album'}[kind] }:{native_id}"

    def create_playlist(self, name: str, description: str = "") -> str:
        r = self._req("POST", "/me/playlists", json={"name": name, "public": False, "description": description})
        return r.json()["id"]

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        self._req("PUT", f"/playlists/{playlist_id}", json={"name": name})

    def delete_playlist(self, playlist_id: str) -> None:
        # Spotify has no delete; unfollowing your own playlist removes it.
        self._req("DELETE", "/me/library", params={"uris": f"spotify:playlist:{playlist_id}"})

    def add_to_playlist(self, playlist_id: str, items: list[Item]) -> None:
        uris = [self._uri(TRACK, it.native_id) for it in items]
        for i in range(0, len(uris), ITEMS_BATCH):
            self._req("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + ITEMS_BATCH]})

    def remove_from_playlist(self, playlist_id: str, items: list[Item]) -> None:
        uris = [{"uri": self._uri(TRACK, it.native_id)} for it in items]
        for i in range(0, len(uris), ITEMS_BATCH):
            self._req("DELETE", f"/playlists/{playlist_id}/items", json={"items": uris[i:i + ITEMS_BATCH]})

    def replace_playlist_items(self, playlist_id: str, items: list[Item]) -> None:
        uris = [self._uri(TRACK, it.native_id) for it in items]
        # PUT replaces with at most 100; the rest are appended in order.
        self._req("PUT", f"/playlists/{playlist_id}/items", json={"uris": uris[:ITEMS_BATCH]})
        for i in range(ITEMS_BATCH, len(uris), ITEMS_BATCH):
            self._req("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + ITEMS_BATCH]})

    def _library(self, method: str, kind: str, items: list[Item]) -> None:
        uris = [self._uri(kind, it.native_id) for it in items]
        for i in range(0, len(uris), LIBRARY_BATCH):
            self._req(method, "/me/library", params={"uris": ",".join(uris[i:i + LIBRARY_BATCH])})

    def like(self, items: list[Item]) -> None:
        self._library("PUT", TRACK, items)

    def unlike(self, items: list[Item]) -> None:
        self._library("DELETE", TRACK, items)

    def save_albums(self, items: list[Item]) -> None:
        self._library("PUT", ALBUM, items)

    def unsave_albums(self, items: list[Item]) -> None:
        self._library("DELETE", ALBUM, items)
