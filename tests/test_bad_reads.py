"""Listings that come back short but claim to be complete.

Replays the October 2026 incidents: Spotify answered /me/playlists without
the user's own playlists (the ★ playlist was recreated three times, two Apple
playlists deleted per run), and an Apple library page dropped nine ★ songs
for one run (removed from Spotify, re-added 15 minutes later)."""
import pytest

from music_sync import cli
from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import FAVS, Engine
from tests.fakes import Fake, world, writes


@pytest.fixture
def env(tmp_path):
    w = world(*[(f"ISRC-{c}", f"Song {c}", f"Artist {c}", 200000 + i, f"am-{c}", f"sp-{c}")
                for i, c in enumerate("abcdef")])
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_liked=False, sync_playlists=True)
    cfg.apple_user_token = "x"
    cfg.spotify_favorites_playlist = "Lieblingstitel"
    apple.favorites_ = ["am-a", "am-b", "am-c"]
    spotify.playlists_["sp-fav"] = {"name": "Lieblingstitel", "items": ["sp-a", "sp-b", "sp-c"]}
    for n in "1234":
        apple.playlists_[f"am-{n}"] = {"name": f"Mix {n}", "items": ["am-d", "am-e"]}
        spotify.playlists_[f"sp-{n}"] = {"name": f"Mix {n}", "items": ["sp-d", "sp-e"]}
    st = State(cfg.state_path)
    run(apple, spotify, st, cfg, seed=True)
    apple.calls.clear(); spotify.calls.clear()
    return apple, spotify, st, cfg


def run(apple, spotify, st, cfg, seed=False):
    e = Engine(apple, spotify, st, cfg)
    e._sleep = lambda s: None
    e.seed(union=True) if seed else e.sync()
    return e


def test_spotify_listing_without_own_playlists_changes_nothing(env):
    apple, spotify, st, cfg = env
    spotify.unlisted = set(spotify.playlists_)              # the glitch
    e = run(*env)
    assert not writes(apple) and not writes(spotify)
    assert len(spotify.playlists_) == 5 and len(apple.playlists_) == 4
    assert [r for r in e.needs_review if "bad read" in r] and e.transient
    spotify.unlisted = set()
    e = run(*env)
    assert not writes(apple) and not writes(spotify) and not e.needs_review


def test_one_missed_listing_does_not_recreate_the_star_playlist(env):
    apple, spotify, st, cfg = env
    spotify.unlisted = {"sp-fav", "sp-1"}                   # under the cap: looks like two deletions
    e = run(*env)
    assert not writes(apple) and not writes(spotify) and not e.needs_review
    spotify.unlisted = set()
    e = run(*env)
    assert not writes(apple) and not writes(spotify) and not e.needs_review
    assert st.db.execute("SELECT spotify_id FROM pair WHERE collection=?", (FAVS,)).fetchone()[0] == "sp-fav"
    assert [p["name"] for p in spotify.playlists_.values()].count("Lieblingstitel") == 1


def test_a_real_playlist_deletion_lands_on_the_second_run(env):
    apple, spotify, st, cfg = env
    del spotify.playlists_["sp-1"]
    run(*env)
    assert "am-1" in apple.playlists_
    run(*env)
    assert "am-1" not in apple.playlists_ and len(apple.playlists_) == 3


def test_short_but_complete_page_does_not_remove(env):
    apple, spotify, st, cfg = env
    apple.short = {"am-b", "am-c"}                          # one page of the library skipped
    e = run(*env)
    assert not writes(spotify)
    assert "held" in next(o.line() for o in e.outcomes if o.collection == FAVS)
    apple.short = set()
    run(*env)
    assert not writes(spotify) and not writes(apple)
    assert spotify.playlists_["sp-fav"]["items"] == ["sp-a", "sp-b", "sp-c"]   # order kept, nothing re-added


def test_standing_issue_is_mailed_once(env, monkeypatch):
    apple, spotify, st, cfg = env
    sent = []
    monkeypatch.setattr(cli.notify, "send", lambda to, frm, subject, body: sent.append(body) or True)
    cfg.notify_email = "me@example.com"
    monkeypatch.setattr(cli, "State", lambda path: st)
    monkeypatch.setattr(cli, "_providers", lambda c, s: (apple, spotify))
    monkeypatch.setattr(cli, "Config", lambda: cfg)
    apple.playlists_["am-9"] = {"name": "Mix 1", "items": []}     # a duplicate name on both sides:
    spotify.playlists_["sp-9"] = {"name": "Mix 1", "items": []}   # a standing issue
    for _ in range(3):
        assert cli.main(["sync"]) == 2
    assert len(sent) == 1
    assert "Apple has 2 playlists named 'mix 1'" in sent[0] and "Spotify has 2" in sent[0]


def test_transient_issue_is_mailed_only_when_it_lasts(tmp_path):
    st = State(tmp_path / "s.db")
    t0 = 1_000_000.0
    assert st.review_due({"listing incomplete": True}, 3600, 7 * 86400, now=t0) == set()
    assert st.review_due({}, 3600, 7 * 86400, now=t0 + 900) == set()           # gone: streak broken
    assert st.review_due({"listing incomplete": True}, 3600, 7 * 86400, now=t0 + 1800) == set()
    assert st.review_due({"listing incomplete": True}, 3600, 7 * 86400, now=t0 + 1800 + 3600) == {"listing incomplete"}
    st.mark_mailed({"listing incomplete"}, now=t0 + 5400)
    assert st.review_due({"listing incomplete": True}, 3600, 7 * 86400, now=t0 + 9000) == set()
    assert st.review_due({"listing incomplete": True}, 3600, 7 * 86400, now=t0 + 5400 + 7 * 86400) == {"listing incomplete"}


def test_spotify_playlist_pages_short_of_total_raise():
    import tempfile
    from pathlib import Path
    from music_sync.providers.spotify import Spotify

    class R:
        def __init__(self, body): self._b = body
        def json(self): return self._b

    sp = Spotify("id", "secret", Path(tempfile.mkdtemp()) / "tok", state=None)
    sp._me = "me"
    sp._req = lambda method, url, **kw: R({"total": 3, "items": [{"id": "p1", "name": "a", "owner": {"id": "me"}}],
                                           "next": None})
    with pytest.raises(RuntimeError, match="1 of 3"):
        sp.playlists()
