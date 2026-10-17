"""Merge-first seed: both sides end as the union; nothing is ever removed;
an add only counts once a fresh listing shows it."""
import pytest

from music_sync.config import Config
from music_sync.merge import Abort, Guard, Mode, plan_merge
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import LIKED, Engine
from tests.fakes import Fake, world, writes


@pytest.fixture
def env(tmp_path):
    w = world(
        ("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
        ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"),
        ("ISRC-C", "Gamma", "Artist Three", 220000, "am-c", "sp-c"),
        ("ISRC-D", "Delta", "Artist Four", 230000, "am-d", "sp-d"),
    )
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False)
    cfg.apple_user_token = "x"
    return apple, spotify, State(cfg.state_path), cfg


def run(env, union_seed=False, dry=False):
    apple, spotify, st, cfg = env
    e = Engine(apple, spotify, st, cfg, dry_run=dry)
    e._sleep = lambda s: None
    e.seed(union=True) if union_seed else e.sync()
    return e


def removals(fake):
    return [c for c in fake.calls if c[0] in ("remove", "unlike", "delete_playlist")]


def test_union_seed_merges_and_removes_nothing(env):
    apple, spotify, st, cfg = env
    apple.liked_ = ["am-a", "am-b"]
    spotify.liked_ = ["sp-b", "sp-c"]
    run(env, union_seed=True)
    assert sorted(apple.liked_) == ["am-a", "am-b", "am-c"]
    assert sorted(spotify.liked_) == ["sp-a", "sp-b", "sp-c"]
    assert not removals(apple) and not removals(spotify)
    assert st.is_seeded(LIKED)
    assert st.last_known(LIKED) == {"track:ISRC-A", "track:ISRC-B", "track:ISRC-C"}


def test_after_union_seed_deletions_propagate(env):
    apple, spotify, st, cfg = env
    apple.liked_, spotify.liked_ = ["am-a"], ["sp-b"]
    run(env, union_seed=True)
    spotify.liked_.remove("sp-a")
    run(env)
    assert apple.liked_ == ["am-b"]


def test_playlists_created_both_ways_in_source_order(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    apple.playlists_["am-pl-1"] = {"name": "Only Apple", "items": ["am-c", "am-a", "am-b"]}
    spotify.playlists_["sp-pl-1"] = {"name": "Only Spotify", "items": ["sp-d", "sp-a"]}
    spotify.playlists_["sp-pl-2"] = {"name": "Both", "items": ["sp-a"]}
    apple.playlists_["am-pl-2"] = {"name": "both", "items": ["am-b"]}
    e = run(env, union_seed=True)
    by_name = {p["name"]: p["items"] for p in spotify.playlists_.values()}
    assert by_name["Only Apple"] == ["sp-c", "sp-a", "sp-b"]           # Apple's order, not id order
    assert sorted(by_name["Both"]) == ["sp-a", "sp-b"]
    a_by_name = {p["name"]: p["items"] for p in apple.playlists_.values()}
    assert a_by_name["Only Spotify"] == ["am-d", "am-a"]
    assert sorted(a_by_name["both"]) == ["am-a", "am-b"]
    assert not removals(apple) and not removals(spotify)
    assert all(o.skipped is None for o in e.outcomes)


def test_silently_dropped_add_is_quarantined_never_mirrored_as_delete(env):
    apple, spotify, st, cfg = env
    spotify.liked_ = ["sp-a", "sp-b"]
    apple.ignore = {"am-b"}                       # Apple answers 202 but never adds it
    e = run(env, union_seed=True)
    assert "am-b" not in apple.liked_
    assert st.last_known(LIKED) == {"track:ISRC-A"}
    assert any("never appeared" in r for r in e.needs_review)
    run(env)                                      # next run: b must stay on Spotify
    assert "sp-b" in spotify.liked_


def test_lagging_add_is_confirmed_after_retry(env):
    apple, spotify, st, cfg = env
    spotify.liked_ = ["sp-a"]
    apple.lag = 2                                 # shows up on the third listing
    e = run(env, union_seed=True)
    assert apple.liked_ == ["am-a"]
    assert st.last_known(LIKED) == {"track:ISRC-A"}
    assert not any("never appeared" in r for r in e.needs_review)


def test_unpinned_pair_is_not_duplicated(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists, cfg.sync_liked = True, False
    apple.playlists_["am-pl-1"] = {"name": "Mix", "items": ["am-a"]}
    spotify.playlists_["sp-pl-1"] = {"name": "Mix", "items": ["sp-a"]}
    # Titles differ between services, so reconciliation cannot pin the pair.
    orig = spotify._item
    spotify._item = lambda n, pos=None: orig(n, pos).__class__(**{**orig(n, pos).__dict__, "title": "Alpha (Live Edit)"})
    spotify.lookup_codes = lambda kind, codes: {c: [orig("sp-a")] for c in codes if c == "ISRC-A"}
    apple.search = lambda kind, artist, title: apple.lookup_codes(kind, ["ISRC-A"])["ISRC-A"]
    run(env, union_seed=True)
    assert spotify.playlists_["sp-pl-1"]["items"] == ["sp-a"]
    assert apple.playlists_["am-pl-1"]["items"] == ["am-a"]


def test_sync_unions_collections_that_appear_after_a_union_seed(env):
    apple, spotify, st, cfg = env
    apple.liked_, spotify.liked_ = ["am-a"], ["sp-b"]
    run(env, union_seed=True)
    cfg.sync_playlists = True
    apple.playlists_["am-pl-1"] = {"name": "Late", "items": ["am-c"]}
    spotify.playlists_["sp-pl-1"] = {"name": "late", "items": ["sp-d"]}
    run(env)
    assert sorted(spotify.playlists_["sp-pl-1"]["items"]) == ["sp-c", "sp-d"]
    assert sorted(apple.playlists_["am-pl-1"]["items"]) == ["am-c", "am-d"]


def test_mirror_seed_still_available(env):
    apple, spotify, st, cfg = env
    apple.liked_, spotify.liked_ = ["am-a"], ["sp-b"]
    e = Engine(apple, spotify, st, cfg)
    e.seed()                                      # Engine default is still the mirror
    assert spotify.liked_ == ["sp-a"]


def test_flood_guard_after_seed_only():
    big = {f"i{n}" for n in range(100)}
    seeded = plan_merge("c", "c", big, {"x"}, {"x"})                 # 100 new on Apple, 1 on Spotify
    with pytest.raises(Abort, match="flood"):
        Guard().check(seeded, {APPLE: 101, SPOTIFY: 1})
    first = plan_merge("c", "c", big, {"x"}, set(), mode=Mode.UNION)
    Guard().check(first, {APPLE: 100, SPOTIFY: 1})                   # the seed may add a lot
    copy = plan_merge("c", "c", big, set(), set())
    Guard().check(copy, {APPLE: 100, SPOTIFY: 0})                    # fresh playlist copy is fine


def test_dry_union_writes_nothing_and_reports_new_playlists(env):
    apple, spotify, st, cfg = env
    cfg.sync_playlists = True
    apple.liked_ = ["am-a"]
    apple.playlists_["am-pl-1"] = {"name": "Only Apple", "items": ["am-a", "am-b"]}
    e = run(env, union_seed=True, dry=True)
    assert not writes(spotify) and not writes(apple)
    assert any("Only Apple' with 2 tracks" in r for r in e.needs_review)
    assert not st.is_seeded(LIKED) and st.get_meta("seed_mode") is None
