"""Seed-time reconciliation must recognise the same recording across sides."""
from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import LIKED, Engine
from tests.fakes import Fake, world


def test_one_second_duration_difference_still_reconciles(tmp_path):
    w = world(("ISRC-A", "Alpha", "Artist One", 213999, "am-a", "sp-a"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    # Spotify reports the same recording one second longer, across a 2 s bucket boundary.
    spotify._item_dur = {"sp-a": 214600}
    orig = spotify._item
    spotify._item = lambda n, pos=None: orig(n, pos).enriched() if n not in spotify._item_dur else orig(n, pos).__class__(
        **{**orig(n, pos).__dict__, "duration_ms": spotify._item_dur[n]})
    apple.liked_, spotify.liked_ = ["am-a"], ["sp-a"]
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    e = Engine(apple, spotify, State(cfg.state_path), cfg)
    e.seed()
    assert not any(c[0] in ("like", "unlike") for c in spotify.calls)   # recognised: nothing removed or re-added


def test_unique_fuzzy_candidate_is_accepted_without_round_trip(tmp_path):
    # Spotify's copy carries an id the isrc: search does not return (another edition).
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    spotify.lookup_codes = lambda kind, codes: {}          # round trip finds nothing
    apple.liked_, spotify.liked_ = ["am-a"], ["sp-a"]
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    Engine(apple, spotify, State(cfg.state_path), cfg).seed()
    assert spotify.liked_ == ["sp-a"] and not spotify.calls
