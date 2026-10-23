"""Every page must carry include/extend, not just the first."""
import tempfile
from pathlib import Path

from music_sync.providers.apple import Apple
from music_sync.state import State


class FakeResp:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def json(self):
        return self._body


def test_include_and_extend_are_resent_on_next_pages():
    a = Apple("tok", State(Path(tempfile.mkdtemp()) / "s.db"), "ch")
    calls = []

    def fake_req(method, url, ok=(200,), **kw):
        calls.append((url, kw.get("params")))
        if url.endswith("?offset=100"):
            return FakeResp({"data": [{"id": "i.2", "attributes": {"name": "b"}}]})
        return FakeResp({"data": [{"id": "i.1", "attributes": {"name": "a"}}], "next": "/v1/me/library/songs?offset=100"})

    a._req = fake_req
    rows = list(a._pages("/me/library/songs", a._LIB))
    assert [r["id"] for r in rows] == ["i.1", "i.2"]
    first, second = calls
    assert first[1] == a._LIB
    assert second[0].endswith("?offset=100")
    assert second[1] == {"include": "catalog", "extend": "inFavorites"}   # no limit/offset duplicates


def test_404_on_a_later_page_is_a_failure_not_the_end():
    a = Apple("tok", State(Path(tempfile.mkdtemp()) / "s.db"), "ch")

    def fake_req(method, url, ok=(200,), **kw):
        if url.endswith("?offset=100"):
            r = FakeResp({}); r.status_code = 404
            return r
        return FakeResp({"data": [{"id": "i.1", "attributes": {"name": "a"}}], "next": "/v1/me/library/songs?offset=100"})

    a._req = fake_req
    assert not a._listing("/me/library/songs", "track", a._LIB).complete
