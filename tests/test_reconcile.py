"""Seed-time reconciliation must recognise the same recording across sides."""
from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import LIKED, Engine
from tests.fakes import Fake, world, writes


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
    assert not writes(spotify)   # recognised: nothing removed or re-added


def test_unique_fuzzy_candidate_is_accepted_without_round_trip(tmp_path):
    # Spotify's copy carries an id the isrc: search does not return (another edition).
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    spotify.lookup_codes = lambda kind, codes: {}          # round trip finds nothing
    apple.liked_, spotify.liked_ = ["am-a"], ["sp-a"]
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    Engine(apple, spotify, State(cfg.state_path), cfg).seed()
    assert spotify.liked_ == ["sp-a"] and not writes(spotify)


def test_unique_candidate_costs_no_spotify_search(tmp_path):
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
              ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    searches = []
    orig = spotify.lookup_codes
    spotify.lookup_codes = lambda kind, codes: (searches.append(codes), orig(kind, codes))[1]
    apple.liked_, spotify.liked_ = ["am-a", "am-b"], ["sp-a", "sp-b"]
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    Engine(apple, spotify, State(cfg.state_path), cfg).seed()
    assert searches == []                                   # both pairs unique: zero lookups
    assert not writes(spotify)                              # and nothing removed or re-added


def test_ambiguous_candidates_are_settled_by_round_trip(tmp_path):
    # Two Apple songs with the same artist+title+duration (two editions); only one is this Spotify id.
    w = world(("ISRC-A1", "Alpha", "Artist One", 200000, "am-a1", "sp-x"),
              ("ISRC-A2", "Alpha", "Artist One", 200500, "am-a2", "sp-a"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    apple.liked_, spotify.liked_ = ["am-a1", "am-a2"], ["sp-a"]
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    st = State(cfg.state_path)
    Engine(apple, spotify, st, cfg).seed()
    assert st.native_id("track:ISRC-A2", SPOTIFY) == "sp-a"      # paired with the right edition
    assert "sp-a" in spotify.liked_ and "sp-x" in spotify.liked_  # the other edition was added, not swapped
