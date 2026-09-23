import pytest

from music_sync import merge
from music_sync.merge import Abort, Guard, Mode
from music_sync.models import APPLE, SPOTIFY


def test_merge_truth_table():
    last = {"both", "gone_from_spotify", "gone_from_apple", "gone_from_both"}
    apple = {"both", "gone_from_spotify", "new_on_apple"}
    spotify = {"both", "gone_from_apple", "new_on_spotify"}
    p = merge.plan_merge("c", "c", apple, spotify, last)
    assert p.add[SPOTIFY] == {"new_on_apple"}
    assert p.add[APPLE] == {"new_on_spotify"}
    assert p.remove[APPLE] == {"gone_from_spotify"}
    assert p.remove[SPOTIFY] == {"gone_from_apple"}
    assert p.stable == {"both"}


def test_first_merge_with_empty_last_known_never_deletes():
    p = merge.plan_merge("c", "c", {"a", "b"}, {"b", "c"}, set())
    assert not p.remove[APPLE] and not p.remove[SPOTIFY]
    assert p.add[SPOTIFY] == {"a"} and p.add[APPLE] == {"c"}


def test_seed_mirrors_master_and_never_touches_it():
    p = merge.plan_seed("c", "c", APPLE, {"a", "b"}, {"b", "x"})
    assert p.mode is Mode.SEED
    assert p.add[SPOTIFY] == {"a"} and p.remove[SPOTIFY] == {"x"}
    assert not p.add[APPLE] and not p.remove[APPLE]
    assert p.stable == {"b"}


def test_guard_ratio_and_count():
    p = merge.plan_merge("c", "c", set(), {f"i{n}" for n in range(30)}, {f"i{n}" for n in range(30)})
    with pytest.raises(Abort):
        Guard(max_delete_ratio=0.2, max_delete_count=50).check(p, {APPLE: 0, SPOTIFY: 30})
    small = merge.plan_merge("c", "c", set(), {"a"}, {"a"})
    Guard().check(small, {APPLE: 0, SPOTIFY: 1})  # below floor: allowed
    big = merge.plan_merge("c", "c", set(), {f"i{n}" for n in range(60)}, {f"i{n}" for n in range(60)})
    with pytest.raises(Abort):
        Guard(max_delete_ratio=1.0, max_delete_count=50).check(big, {APPLE: 0, SPOTIFY: 60})


def test_confirmed_last_known_excludes_failed_adds():
    p = merge.plan_merge("c", "c", {"both", "a1", "a2"}, {"both"}, {"both"})
    assert p.add[SPOTIFY] == {"a1", "a2"}
    lk = merge.confirmed_last_known(p, {SPOTIFY: {"a1"}})
    assert lk == {"both", "a1"}      # a2 stays one-sided, retried next run


def test_require_complete():
    with pytest.raises(Abort):
        merge.require_complete({APPLE: True, SPOTIFY: False}, "x")
    merge.require_complete({APPLE: True, SPOTIFY: True}, "x")
