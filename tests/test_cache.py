"""Unchanged Spotify collections are not re-listed; changed or written ones are."""
from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import Engine
from tests.fakes import Fake, world, writes


def setup(tmp_path):
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
              ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False); cfg.apple_user_token = "x"
    st = State(cfg.state_path)
    apple.playlists_["am-pl-1"] = {"name": "Mix", "items": ["am-a"]}
    apple.liked_ = ["am-a", "am-b"]
    Engine(apple, spotify, st, cfg).seed()
    return apple, spotify, st, cfg


def listings(fake):
    return [c for c in fake.calls if c[0] in ("list_playlist", "list_liked")]


def test_quiet_run_reuses_cached_spotify_listings(tmp_path):
    apple, spotify, st, cfg = setup(tmp_path)
    Engine(apple, spotify, st, cfg).sync()               # first run after the seed's writes re-lists once
    spotify.calls.clear(); apple.calls.clear()
    Engine(apple, spotify, st, cfg).sync()
    assert listings(spotify) == []                       # snapshot + signature unchanged: no listing calls
    assert len(listings(apple)) == 2                     # Apple has no cheap signal: still listed


def test_changed_snapshot_relists_and_syncs(tmp_path):
    apple, spotify, st, cfg = setup(tmp_path)
    sp_pl = next(pid for pid, p in spotify.playlists_.items() if p["name"] == "Mix")
    spotify.playlists_[sp_pl]["items"].append("sp-b")     # user adds on Spotify -> snapshot changes
    spotify.calls.clear()
    Engine(apple, spotify, st, cfg).sync()
    assert ("list_playlist", sp_pl) in spotify.calls
    assert apple.playlists_["am-pl-1"]["items"] == ["am-a", "am-b"]


def test_liked_signature_change_relists(tmp_path):
    apple, spotify, st, cfg = setup(tmp_path)
    spotify.liked_.remove("sp-b")
    spotify.calls.clear()
    Engine(apple, spotify, st, cfg).sync()
    assert ("list_liked",) in spotify.calls
    assert apple.liked_ == ["am-a"]


def test_own_write_invalidates_cache(tmp_path):
    apple, spotify, st, cfg = setup(tmp_path)
    apple.liked_.append("am-b")                          # nothing new: b already on both sides
    apple.playlists_["am-pl-1"]["items"].append("am-b")  # Apple add -> engine writes to Spotify
    Engine(apple, spotify, st, cfg).sync()
    spotify.calls.clear()
    Engine(apple, spotify, st, cfg).sync()
    # the write changed the snapshot, so the next run lists once and then settles
    assert len([c for c in spotify.calls if c[0] == "list_playlist"]) == 1
