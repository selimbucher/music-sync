"""An exhausted search quota defers matching; it never causes a removal."""
from music_sync.config import Config
from music_sync.models import APPLE, SPOTIFY
from music_sync.providers.base import SearchQuota
from music_sync.state import State
from music_sync.sync import LIKED, Engine
from tests.fakes import Fake, world, writes


def env(tmp_path):
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
              ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    return apple, spotify, State(cfg.state_path), cfg


def block_search(fake):
    def boom(*a, **k):
        raise SearchQuota("spotify: search quota exhausted")
    fake.lookup_codes = boom
    fake.search = boom


def test_add_needing_a_search_is_deferred_and_retried(tmp_path):
    apple, spotify, st, cfg = env(tmp_path)
    apple.liked_ = ["am-a"]
    Engine(apple, spotify, st, cfg).seed()
    apple.liked_.append("am-b")                          # needs an isrc: search to land on Spotify
    block_search(spotify)
    e = Engine(apple, spotify, st, cfg); e.sync()
    assert e.outcomes[0].deferred == ["Artist Two - Beta"]
    assert "sp-b" not in spotify.liked_ and "am-b" in apple.liked_
    assert st.last_known(LIKED) == {"track:ISRC-A"}      # not confirmed, so not a future deletion
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"),
              ("ISRC-B", "Beta", "Artist Two", 210000, "am-b", "sp-b"))
    fresh = Fake(SPOTIFY, w); fresh.liked_ = spotify.liked_
    Engine(apple, fresh, st, cfg).sync()                 # quota back: the add lands
    assert "sp-b" in fresh.liked_


def test_seed_with_ambiguous_candidates_aborts_instead_of_guessing(tmp_path):
    w = world(("ISRC-A1", "Alpha", "Artist One", 200000, "am-a1", "sp-x"),
              ("ISRC-A2", "Alpha", "Artist One", 200500, "am-a2", "sp-a"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False, sync_playlists=False); cfg.apple_user_token = "x"
    st = State(cfg.state_path)
    apple.liked_, spotify.liked_ = ["am-a1", "am-a2"], ["sp-a"]
    block_search(spotify)
    e = Engine(apple, spotify, st, cfg); e.seed()
    assert e.outcomes[0].skipped and "search" in e.outcomes[0].skipped
    assert spotify.liked_ == ["sp-a"] and not writes(spotify)
    assert not st.is_seeded(LIKED)
