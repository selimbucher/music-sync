"""POST /me/favorites accepts a single id per request."""
import tempfile
from pathlib import Path

from music_sync.models import TRACK, Item
from music_sync.providers.apple import Apple
from music_sync.state import State


def test_favorite_sends_one_id_per_request():
    a = Apple("tok", State(Path(tempfile.mkdtemp()) / "s.db"), "ch")
    sent = []
    a._req = lambda method, path, ok=(200,), **kw: sent.append((method, path, kw.get("params")))
    a.favorite([Item(kind=TRACK, side="apple", native_id=f"c{n}", catalog_id=f"c{n}") for n in range(3)])
    assert sent == [("POST", "/me/favorites", {"ids[songs]": f"c{n}"}) for n in range(3)]
