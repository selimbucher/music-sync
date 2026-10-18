"""A failed add is isolated, quarantined with backoff, and not retried every run."""
from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.state import State
from music_sync.sync import Engine
from tests.fakes import Fake, world


def test_bad_item_is_isolated_and_backed_off(tmp_path):
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
              ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"),
              ("ISRC-C", "Gamma", "Artist Three", 220000, "am-c", "sp-c"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_liked=False); cfg.apple_user_token = "x"
    st = State(cfg.state_path)
    apple.playlists_["am-pl"] = {"name": "Mix", "items": []}
    spotify.playlists_["sp-pl"] = {"name": "Mix", "items": ["sp-a", "sp-b", "sp-c"]}
    apple.poison = {"am-b"}                                    # Apple 500s on this one track

    def run():
        e = Engine(apple, spotify, st, cfg); e._sleep = lambda s: None
        e.seed(union=True) if not st.any_seeded() else e.sync()
        return e

    e = run()
    assert sorted(apple.playlists_["am-pl"]["items"]) == ["am-a", "am-c"]   # the rest still landed
    assert [r for r in e.needs_review if "Beta" in r]
    e2 = run()                                                  # next run: in backoff, no retry, no mail
    assert not [c for c in apple.calls if c[0] == "add" and "am-b" in c[2]][1:]
    assert e2.needs_review == []
    assert e2.outcomes[0].plan.blocked == ["track:ISRC-B"]
