import pytest

from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import LIKED, Engine
from tests.fakes import Fake, world


@pytest.fixture
def env(tmp_path):
    w = world(
        ("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
        ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"),
        ("ISRC-C", "Gamma", "Artist Three", 220000, "am-c", "sp-c"),
        ("ISRC-D", "Delta", "Artist Four", 230000, "am-d", "sp-d"),
        ("ISRC-E", "Epsilon", "Artist Five", 240000, "am-e", "sp-e"),
    )
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False)
    cfg.apple_user_token = "x"
    st = State(cfg.state_path)
    return apple, spotify, st, cfg


def engine(env, dry=False):
    apple, spotify, st, cfg = env
    return Engine(apple, spotify, st, cfg, dry_run=dry)


def test_seed_makes_spotify_match_apple_without_touching_apple(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a", "am-b", "am-c"]
    spotify.liked_ = ["sp-b", "sp-d"]          # b is shared but unmapped; d is Spotify-only
    e = engine(env)
    e.seed()
    assert sorted(spotify.liked_) == ["sp-a", "sp-b", "sp-c"]
    assert apple.liked_ == ["am-a", "am-b", "am-c"]
    assert not any(c[0] in ("like", "unlike") for c in apple.calls)
    # b was reconciled by round-trip, so it was neither removed nor re-added
    assert ("unlike", ["sp-b"]) not in spotify.calls
    assert st.is_seeded(LIKED)
    assert st.last_known(LIKED) == {"track:ISRC-A", "track:ISRC-B", "track:ISRC-C"}


def test_sync_refuses_before_seed(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a"]
    e = engine(env)
    e.sync()
    assert e.outcomes[0].skipped and "seed" in e.outcomes[0].skipped
    assert spotify.liked_ == []


def test_bidirectional_add_and_delete_after_seed(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a", "am-b"]
    engine(env).seed()
    # user adds E on Spotify, removes A on Apple
    spotify.liked_.append("sp-e")
    apple.liked_.remove("am-a")
    engine(env).sync()
    assert sorted(apple.liked_) == ["am-b", "am-e"]
    assert sorted(spotify.liked_) == ["sp-b", "sp-e"]
    assert st.last_known(LIKED) == {"track:ISRC-B", "track:ISRC-E"}


def test_unmatched_item_is_quarantined_never_deleted(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a"]
    engine(env).seed()
    spotify.liked_.append("sp-zzz")              # not in the world: unmatchable
    e = engine(env)
    e.sync()
    assert "sp-zzz" in spotify.liked_
    assert e.outcomes[0].quarantined
    assert st.quarantined()
    # second run: still there, still not deleted, still not in last_known
    engine(env).sync()
    assert "sp-zzz" in spotify.liked_
    assert st.last_known(LIKED) == {"track:ISRC-A"}


def test_incomplete_listing_blocks_everything(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a", "am-b"]
    engine(env).seed()
    apple.liked_.remove("am-a")
    spotify.complete = False
    e = engine(env)
    e.sync()
    assert e.outcomes[0].skipped
    assert sorted(spotify.liked_) == ["sp-a", "sp-b"]   # nothing mirrored from a partial read
    assert st.last_known(LIKED) == {"track:ISRC-A", "track:ISRC-B"}


def test_failed_add_is_retried_not_mirrored_as_delete(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a"]
    engine(env).seed()
    apple.liked_.append("am-b")
    spotify.fail_add = True
    engine(env).sync()
    assert "sp-b" not in spotify.liked_
    assert st.last_known(LIKED) == {"track:ISRC-A"}     # b not confirmed
    spotify.fail_add = False
    engine(env).sync()
    assert "sp-b" in spotify.liked_                       # retried as an add
    assert "am-b" in apple.liked_                         # never deleted from Apple


def test_dry_run_writes_nothing(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a"]
    e = engine(env, dry=True)
    e.seed()
    assert spotify.liked_ == [] and not spotify.calls
    assert not st.is_seeded(LIKED)
    assert e.outcomes[0].plan.add[SPOTIFY] == {"track:ISRC-A"}


def test_guard_aborts_mass_deletion(env):
    apple, spotify, st, cfg = env
    ids = [f"am-{c}" for c in "abcde"]
    apple.liked_ = list(ids)
    engine(env).seed()
    apple.liked_ = ["am-a"]           # 4 of 5 vanish from Apple at once
    cfg.max_delete_ratio = 0.5
    e = engine(env)
    e.sync()
    assert e.outcomes[0].skipped and "deleted" in e.outcomes[0].skipped
    assert len(spotify.liked_) == 5


def test_playlist_pairing_seed_and_prune(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    apple.playlists_["am-pl-1"] = {"name": "Gym", "items": ["am-a", "am-b"]}
    apple.playlists_["am-pl-2"] = {"name": "Focus", "items": ["am-c"]}
    spotify.playlists_["sp-pl-1"] = {"name": "gym", "items": ["sp-b", "sp-d"]}      # same name, paired
    spotify.playlists_["sp-pl-9"] = {"name": "Spotify only", "items": ["sp-e"]}
    e = engine(env)
    e.seed()
    assert spotify.playlists_["sp-pl-1"]["items"] == ["sp-a", "sp-b"]              # d removed, a added, Apple's order
    assert [p["name"] for p in spotify.playlists_.values() if p["name"] == "Focus"]
    assert "sp-pl-9" not in spotify.playlists_                                      # exact mirror: Spotify-only playlist gone


def test_seed_can_keep_extra_playlists(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    spotify.playlists_["sp-pl-9"] = {"name": "Spotify only", "items": ["sp-e"]}
    e = engine(env)
    e.seed(prune_playlists=False)
    assert "sp-pl-9" in spotify.playlists_
    assert any("Spotify-only" in r for r in e.needs_review)


def test_seed_copies_order_and_sync_leaves_order_alone(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    apple.playlists_["am-pl-1"] = {"name": "Mix", "items": ["am-c", "am-a", "am-b"]}
    spotify.playlists_["sp-pl-1"] = {"name": "Mix", "items": ["sp-a", "sp-b", "sp-c"]}
    engine(env).seed()
    assert spotify.playlists_["sp-pl-1"]["items"] == ["sp-c", "sp-a", "sp-b"]
    spotify.playlists_["sp-pl-1"]["items"] = ["sp-b", "sp-c", "sp-a"]              # user reorders later
    engine(env).sync()
    assert spotify.playlists_["sp-pl-1"]["items"] == ["sp-b", "sp-c", "sp-a"]      # bidirectional sync never reorders


def test_playlist_created_and_deleted_after_seed(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    apple.playlists_["am-pl-1"] = {"name": "Gym", "items": ["am-a"]}
    engine(env).seed()
    spotify.playlists_["sp-pl-7"] = {"name": "Late", "items": ["sp-c"]}            # new on Spotify
    engine(env).sync()
    late = [pid for pid, p in apple.playlists_.items() if p["name"] == "Late"]
    assert late and apple.playlists_[late[0]]["items"] == ["am-c"]
    gym_sp = [pid for pid, p in spotify.playlists_.items() if p["name"] == "Gym"][0]
    del apple.playlists_["am-pl-1"]                                                  # deleted on Apple
    engine(env).sync()
    assert gym_sp not in spotify.playlists_


def test_rename_propagates(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    apple.playlists_["am-pl-1"] = {"name": "Gym", "items": []}
    engine(env).seed()
    apple.playlists_["am-pl-1"]["name"] = "Gym 2026"
    engine(env).sync()
    assert [p["name"] for p in spotify.playlists_.values()] == ["Gym 2026"]


def test_sync_before_any_seed_touches_nothing_not_even_playlists(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists = True
    spotify.playlists_["sp-pl-1"] = {"name": "Only here", "items": ["sp-a"]}
    e = engine(env)
    e.sync()
    assert apple.playlists_ == {} and not apple.calls
    assert e.outcomes[0].skipped
