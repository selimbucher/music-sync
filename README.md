# music-sync

Bidirectional Apple Music <-> Spotify sync. Deletions propagate both ways. First run makes Spotify match Apple Music exactly.

| Spotify | Apple Music |
|---|---|
| Playlists | Playlists |
| Liked Songs | Library songs (+) by default; `appleLiked = "favorites"` for the star instead |
| Saved albums | Starred albums (the star). Apple's library-albums list is derived from single songs, so only the star means "saved" |

## Prerequisites

- Spotify Premium on the account that owns the app (Development Mode requirement since 2026-03-09)
- Apple Music subscription; no Apple Developer account needed

## Secrets

Four files. Put them in `hetzner-secrets` (or any 0600 files):

```
music-sync/apple-user-token        # see "Apple token" below
music-sync/spotify-client-id
music-sync/spotify-client-secret
music-sync/spotify-refresh-token   # written by `music-sync auth spotify`
```

### Spotify app

1. https://developer.spotify.com/dashboard -> Create app
2. Redirect URI: `http://127.0.0.1:8765/callback`
3. Copy Client ID and Client secret into the files above

### Spotify refresh token (once, on a machine with a browser)

```sh
nix develop
export MUSIC_SYNC_SPOTIFY_CLIENT_ID_FILE=<path to client-id file>
export MUSIC_SYNC_SPOTIFY_CLIENT_SECRET_FILE=<path to client-secret file>
python -m music_sync.cli auth spotify --out <path to write spotify-refresh-token>
```

### Apple token (once, then roughly every 6 months)

1. Desktop browser -> https://music.apple.com -> sign in
2. DevTools -> Application -> Cookies -> `media-user-token` -> copy value; note the Expires date
3. Write the value to `music-sync/apple-user-token`
4. Close the tab. Do not sign out (that revokes the token).

When it expires the service mails `notifyEmail` with subject `[music-sync] credentials dead`. Repeat these steps.

## NixOS (Hetzner)

flake.nix:

```nix
inputs.music-sync = {
  url = "github:selimbucher/music-sync";
  inputs.nixpkgs.follows = "nixpkgs";
};
# modules:
music-sync.nixosModules.default
```

configuration.nix:

```nix
services.music-sync = {
  enable = true;
  interval = "15min";
  appleUserTokenFile      = "/etc/secrets/music-sync/apple-user-token";
  spotifyClientIdFile     = "/etc/secrets/music-sync/spotify-client-id";
  spotifyClientSecretFile = "/etc/secrets/music-sync/spotify-client-secret";
  spotifyRefreshTokenFile = "/var/lib/music-sync/spotify-refresh-token";  # service rewrites it on rotation
  notifyEmail = "me@selim.one";
  after = [ "hetzner-secrets.service" ];
};
```

secrets.nix, at the end of the `hetzner-secrets` install list (guarded: harmless until the files exist):

```
if [ -d "$REPO/music-sync" ]; then
  install -Dm600 "$REPO/music-sync/apple-user-token"      /etc/secrets/music-sync/apple-user-token
  install -Dm600 "$REPO/music-sync/spotify-client-id"     /etc/secrets/music-sync/spotify-client-id
  install -Dm600 "$REPO/music-sync/spotify-client-secret" /etc/secrets/music-sync/spotify-client-secret
  [ -e /var/lib/music-sync/spotify-refresh-token ] || \
    install -Dm600 -o music-sync -g music-sync "$REPO/music-sync/spotify-refresh-token" /var/lib/music-sync/spotify-refresh-token   # never overwrite a rotated token
fi
```

and add `"music-sync.service"` to its `before` list.

## First run

```sh
# on the server; runs with the service's credentials and environment
sudo music-sync-admin seed --dry-run     # lists every add, removal, playlist deletion and reorder
sudo music-sync-admin seed               # Spotify := Apple Music exactly, incl. track order
# --force            when the dry run showed a collection losing >20% / >50 items and that is intended
# --keep-extra-playlists to leave Spotify-only playlists in place
```

The timer is already active after the rebuild; `sync` is a no-op until seeded.

## Operate

```sh
sudo music-sync-admin status
sudo music-sync-admin quarantine        # items that never matched; fix manually or ignore
sudo music-sync-admin journal -n 100
sudo music-sync-admin sync --dry-run
journalctl -u music-sync -n 50
```

## Rotating the Apple token

```sh
# after updating music-sync/apple-user-token in hetzner-secrets:
sudo systemctl restart hetzner-secrets && sudo systemctl start music-sync
```

## Safety rails (not configurable off)

- Deletions only from listings the API reported complete
- A run never deletes more than 20% or 50 items of a collection, or more than 2 playlists; over that it stops and mails
- Items with no confident match are quarantined and retried with backoff; they are never deleted
- Follows/artists are not synced: Apple exposes no follow API

## Not synced

- Track order within playlists after the seed (seed copies Apple's order to Spotify once)
- Local files / uploaded tracks (no catalog id)
- Podcasts, audiobooks
