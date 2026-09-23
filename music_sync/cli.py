"""music-sync command line.

  auth spotify   one-time OAuth on a machine with a browser; writes the refresh token
  seed           first run: Spotify := Apple Music exactly (Apple untouched)
  sync           bidirectional run
  status         credentials, pairs, quarantine, last runs
  quarantine     list items that could not be matched
  journal        recent actions

Exit codes: 0 ok, 1 error, 2 needs review, 3 credentials dead, 4 throttled.
"""
from __future__ import annotations

import argparse
import http.server
import logging
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

from . import notify
from .config import Config
from .models import APPLE
from .providers.apple import Apple
from .providers.base import AuthError, Throttled
from .providers.spotify import Spotify, authorize_url, exchange_code
from .state import State
from .sync import Engine

log = logging.getLogger("music-sync")


def _providers(cfg: Config, state: State) -> tuple[Apple, Spotify]:
    missing = cfg.missing()
    if missing:
        sys.exit("missing configuration:\n  " + "\n  ".join(missing))
    return (
        Apple(cfg.apple_user_token, state, cfg.apple_storefront, liked_mode=cfg.apple_liked),
        Spotify(cfg.spotify_client_id, cfg.spotify_client_secret, cfg.spotify_refresh_token_file, state=state),
    )


def _run(args, cfg: Config, seed: bool) -> int:
    state = State(cfg.state_path)
    apple, spotify = _providers(cfg, state)
    engine = Engine(apple, spotify, state, cfg, dry_run=args.dry_run)
    try:
        if seed:
            engine.seed(master=APPLE, prune_playlists=not args.keep_extra_playlists)
        else:
            engine.sync()
    except AuthError as e:
        msg = f"{e}\n\nThe stored credential no longer works. Re-auth steps are in the README."
        print(msg, file=sys.stderr)
        state.log(engine.run, "auth_error", detail=str(e))
        notify.send(cfg.notify_email, cfg.notify_from, "credentials dead", msg)
        return 3
    except Throttled as e:
        print(e, file=sys.stderr)
        state.log(engine.run, "throttled", detail=str(e))
        return 4
    finally:
        if not args.dry_run:
            state.prune_journal()
    report = engine.report()
    print(report)
    if engine.needs_review:
        notify.send(cfg.notify_email, cfg.notify_from, "needs review", report)
        return 2
    return 0


def _auth_spotify(args, cfg: Config) -> int:
    if not cfg.spotify_client_id or not cfg.spotify_client_secret:
        sys.exit("set MUSIC_SYNC_SPOTIFY_CLIENT_ID_FILE and _SECRET_FILE first")
    out = Path(args.out or cfg.spotify_refresh_token_file or "spotify-refresh-token")
    state_token = secrets.token_urlsafe(16)
    url = authorize_url(cfg.spotify_client_id, cfg.spotify_redirect_uri, state_token)
    parsed = urllib.parse.urlparse(cfg.spotify_redirect_uri)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            got.update(q)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"music-sync: you can close this tab.")

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80), Handler)
    print("open this URL in a browser on this machine:\n\n  " + url + "\n", flush=True)
    webbrowser.open(url)
    srv.handle_request()
    if got.get("state") != state_token or "code" not in got:
        sys.exit(f"authorization failed: {got.get('error', 'no code')}")
    tok = exchange_code(cfg.spotify_client_id, cfg.spotify_client_secret, got["code"], cfg.spotify_redirect_uri)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(tok["refresh_token"] + "\n")
    out.chmod(0o600)
    print(f"refresh token written to {out}")
    return 0


def _status(args, cfg: Config) -> int:
    state = State(cfg.state_path)
    miss = cfg.missing()
    print("config:", "ok" if not miss else "missing " + "; ".join(miss))
    dev = state.get_meta("apple_dev_token")
    if dev:
        import json
        exp = json.loads(dev)["exp"]
        print(f"apple developer token: expires {time.strftime('%Y-%m-%d', time.gmtime(exp))}")
    for key, label in (("apple_throttled_until", "apple"), ("spotify_throttled_until", "spotify"),
                       ("spotify_search_throttled_until", "spotify search")):
        until = float(state.get_meta(key, "0") or 0)
        if until > time.time():
            print(f"{label}: throttled until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}")
    print("collections:")
    for r in state.pairs():
        print(f"  {r['label']:<32} seeded={bool(r['seeded'])} last_known={len(state.last_known(r['collection']))}")
    q = state.quarantined()
    print(f"quarantine: {len(q)} item(s)")
    last = state.recent(1)
    if last:
        print("last action:", time.strftime("%Y-%m-%d %H:%M", time.localtime(last[0]["ts"])), last[0]["action"])
    return 0


def _quarantine(args, cfg: Config) -> int:
    state = State(cfg.state_path)
    for r in state.quarantined():
        print(f"{r['side']:<8} {r['label']:<60} {r['reason']} (x{r['attempts']})")
    return 0


def _journal(args, cfg: Config) -> int:
    state = State(cfg.state_path)
    for r in reversed(state.recent(args.n)):
        ts = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        print(f"{ts} {r['action']:<16} {r['side'] or '':<8} {r['collection'] or '':<16} {r['label'] or ''} {r['detail'] or ''}"[:160])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="music-sync", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth")
    a.add_argument("service", choices=["spotify"])
    a.add_argument("--out", help="where to write the refresh token")

    s = sub.add_parser("seed")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--keep-extra-playlists", action="store_true",
                   help="leave playlists that exist only on Spotify (default: delete them, so Spotify matches exactly)")

    y = sub.add_parser("sync")
    y.add_argument("--dry-run", action="store_true")

    sub.add_parser("status")
    sub.add_parser("quarantine")
    j = sub.add_parser("journal")
    j.add_argument("-n", type=int, default=50)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = Config()
    if args.cmd == "auth":
        return _auth_spotify(args, cfg)
    if args.cmd == "seed":
        return _run(args, cfg, seed=True)
    if args.cmd == "sync":
        return _run(args, cfg, seed=False)
    if args.cmd == "status":
        return _status(args, cfg)
    if args.cmd == "quarantine":
        return _quarantine(args, cfg)
    if args.cmd == "journal":
        return _journal(args, cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
