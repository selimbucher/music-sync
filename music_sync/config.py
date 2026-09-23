"""Runtime configuration. Secrets are read from files so nothing lands in the
Nix store or the journal; the NixOS module points these at /etc/secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    return p.read_text().strip() if p.exists() else None


def _cred(name: str, env: str) -> str | None:
    """Explicit *_FILE env var, else the systemd credential of that name."""
    if os.environ.get(env):
        return os.environ[env]
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    return f"{d}/{name}" if d else None


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes")


@dataclass
class Config:
    state_path: Path = field(default_factory=lambda: Path(os.environ.get("MUSIC_SYNC_STATE", "~/.local/state/music-sync/state.db")).expanduser())

    apple_user_token: str | None = field(default_factory=lambda: _read(_cred("apple-user-token", "MUSIC_SYNC_APPLE_USER_TOKEN_FILE")))
    apple_storefront: str | None = field(default_factory=lambda: os.environ.get("MUSIC_SYNC_APPLE_STOREFRONT"))

    spotify_client_id: str | None = field(default_factory=lambda: _read(_cred("spotify-client-id", "MUSIC_SYNC_SPOTIFY_CLIENT_ID_FILE")))
    spotify_client_secret: str | None = field(default_factory=lambda: _read(_cred("spotify-client-secret", "MUSIC_SYNC_SPOTIFY_CLIENT_SECRET_FILE")))
    spotify_refresh_token_file: Path | None = field(default_factory=lambda: Path(os.environ["MUSIC_SYNC_SPOTIFY_REFRESH_TOKEN_FILE"]) if os.environ.get("MUSIC_SYNC_SPOTIFY_REFRESH_TOKEN_FILE") else None)
    spotify_redirect_uri: str = field(default_factory=lambda: os.environ.get("MUSIC_SYNC_SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8765/callback"))

    # What Spotify "Liked Songs" means on Apple: "library" (Add to Library, +)
    # or "favorites" (the star). Selim's Apple library and Spotify likes were
    # already the same set, so library is the default.
    apple_liked: str = field(default_factory=lambda: os.environ.get("MUSIC_SYNC_APPLE_LIKED", "library"))

    sync_playlists: bool = field(default_factory=lambda: _env_bool("MUSIC_SYNC_PLAYLISTS", True))
    sync_liked: bool = field(default_factory=lambda: _env_bool("MUSIC_SYNC_LIKED", True))
    sync_albums: bool = field(default_factory=lambda: _env_bool("MUSIC_SYNC_ALBUMS", True))

    # Safety rails (see merge.Guard). Playlist-level deletions per run are
    # capped separately because one is a whole collection.
    max_delete_ratio: float = field(default_factory=lambda: float(os.environ.get("MUSIC_SYNC_MAX_DELETE_RATIO", "0.2")))
    max_delete_count: int = field(default_factory=lambda: int(os.environ.get("MUSIC_SYNC_MAX_DELETE_COUNT", "50")))
    max_playlist_deletes: int = field(default_factory=lambda: int(os.environ.get("MUSIC_SYNC_MAX_PLAYLIST_DELETES", "2")))

    notify_email: str | None = field(default_factory=lambda: os.environ.get("MUSIC_SYNC_NOTIFY_EMAIL"))
    notify_from: str = field(default_factory=lambda: os.environ.get("MUSIC_SYNC_NOTIFY_FROM", "music-sync@localhost"))

    def missing(self) -> list[str]:
        out = []
        if not self.apple_user_token:
            out.append("MUSIC_SYNC_APPLE_USER_TOKEN_FILE (media-user-token cookie)")
        if not self.spotify_client_id or not self.spotify_client_secret:
            out.append("MUSIC_SYNC_SPOTIFY_CLIENT_ID_FILE / _SECRET_FILE")
        if not self.spotify_refresh_token_file or not self.spotify_refresh_token_file.exists():
            out.append("MUSIC_SYNC_SPOTIFY_REFRESH_TOKEN_FILE (run: music-sync auth spotify)")
        return out
