"""Apple's favourite (★) songs <-> one named Spotify playlist, both ways."""
import pytest

from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import FAVS, Engine
from tests.fakes import Fake, world


@pytest.fixture
def env(tmp_path):
    w = world(
        ("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
        ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"),
        ("ISRC-C", "Gamma", "Artist Three", 220000, "am-c", "sp-c"),
    )
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_liked=False, sync_playlists=True)
    cfg.apple_user_token = "x"
    cfg.spotify_favorites_playlist = "Lieblingstitel"
    return apple, spotify, State(cfg.state_path), cfg


def run(env, seed=False):
    apple, spotify, st, cfg = env
    e = Engine(apple, spotify, st, cfg)
    e._sleep = lambda s: None
    e.seed(union=True) if seed else e.sync()
    return e


def test_union_seed_merges_stars_and_the_playlist(env):
    apple, spotify, st, cfg = env
    apple.favorites_ = ["am-a", "am-b"]
    spotify.playlists_["sp-fav"] = {"name": "Lieblingstitel", "items": ["sp-b", "sp-c"]}
    run(env, seed=True)
    assert sorted(apple.favorites_) == ["am-a", "am-b", "am-c"]
    assert sorted(spotify.playlists_["sp-fav"]["items"]) == ["sp-a", "sp-b", "sp-c"]
    assert apple.playlists_ == {}                       # never copied to Apple as a plain playlist
    assert st.is_seeded(FAVS)


def test_star_and_unstar_flow_both_ways(env):
    apple, spotify, st, cfg = env
    apple.favorites_ = ["am-a"]
    spotify.playlists_["sp-fav"] = {"name": "Lieblingstitel", "items": ["sp-a"]}
    run(env, seed=True)
    apple.favorites_.append("am-b")                     # star on the iPhone
    run(env)
    assert spotify.playlists_["sp-fav"]["items"] == ["sp-a", "sp-b"]
    spotify.playlists_["sp-fav"]["items"].remove("sp-a")  # drop from the Spotify playlist
    run(env)
    run(env)
    assert apple.favorites_ == ["am-b"]                 # un-starred on Apple


def test_missing_playlist_is_created_with_the_stars(env):
    apple, spotify, st, cfg = env
    apple.favorites_ = ["am-a", "am-c"]
    run(env, seed=True)
    (pid, pl), = spotify.playlists_.items()
    assert pl["name"] == "Lieblingstitel" and pl["items"] == ["sp-a", "sp-c"]


def test_deleting_the_playlist_does_not_unstar_anything(env):
    apple, spotify, st, cfg = env
    apple.favorites_ = ["am-a", "am-b"]
    run(env, seed=True)
    spotify.playlists_.clear()                          # user deletes the playlist
    assert not run(env).needs_review                    # one listing without it proves nothing
    e = run(env)
    assert sorted(apple.favorites_) == ["am-a", "am-b"]
    (pid, pl), = spotify.playlists_.items()
    assert sorted(pl["items"]) == ["sp-a", "sp-b"]      # recreated from the stars
    assert any("recreating" in r for r in e.needs_review)


def test_unstar_that_does_not_take_is_retried_not_resurrected(env):
    apple, spotify, st, cfg = env
    apple.favorites_ = ["am-a", "am-b"]
    spotify.playlists_["sp-fav"] = {"name": "Lieblingstitel", "items": ["sp-a", "sp-b"]}
    run(env, seed=True)
    apple.stuck = {"am-a"}                              # Apple accepts the unstar but keeps the star
    spotify.playlists_["sp-fav"]["items"].remove("sp-a")
    run(env)
    e = run(env)
    assert "am-a" in apple.favorites_
    assert any("still there" in r for r in e.needs_review)
    assert spotify.playlists_["sp-fav"]["items"] == ["sp-b"]        # not copied back
    apple.stuck = set()
    run(env)                                            # retried as a removal
    assert apple.favorites_ == ["am-b"]
    assert spotify.playlists_["sp-fav"]["items"] == ["sp-b"]


def test_off_when_not_configured(env):
    apple, spotify, st, cfg = env
    cfg.spotify_favorites_playlist = None
    apple.favorites_ = ["am-a"]
    spotify.playlists_["sp-fav"] = {"name": "Lieblingstitel", "items": ["sp-b"]}
    run(env, seed=True)
    assert apple.favorites_ == ["am-a"]
    assert [p["name"] for p in apple.playlists_.values()] == ["Lieblingstitel"]   # plain playlist pair
