"""Row parsing for amp-api responses, from the documented shapes."""
from music_sync.models import ALBUM, TRACK
from music_sync.providers.apple import Apple


def library_song(inline_catalog=True, favorite=True):
    row = {
        "id": "i.abc123", "type": "library-songs",
        "attributes": {"name": "Around the World", "artistName": "Daft Punk", "durationInMillis": 429000,
                       "inFavorites": favorite, "playParams": {"id": "i.abc123", "catalogId": "696886431"}},
    }
    if inline_catalog:
        row["relationships"] = {"catalog": {"data": [{
            "id": "696886431", "type": "songs",
            "attributes": {"isrc": "GBDUW0600009", "durationInMillis": 429533, "name": "Around the World"},
        }]}}
    return row


def test_track_row_prefers_inline_catalog():
    it = Apple._row(library_song(), TRACK)
    assert it.native_id == "i.abc123"          # what removal needs
    assert it.catalog_id == "696886431"        # what adds and favorites need
    assert it.isrc == "GBDUW0600009" and it.code == "GBDUW0600009"
    assert it.duration_ms == 429533


def test_track_row_without_inline_catalog_keeps_catalog_id_for_enrich():
    it = Apple._row(library_song(inline_catalog=False), TRACK)
    assert it.isrc is None and it.code is None
    assert it.catalog_id == "696886431"


def test_local_file_row_has_no_catalog_id():
    row = {"id": "i.local", "type": "library-songs",
           "attributes": {"name": "Demo", "artistName": "Me", "playParams": {"id": "i.local"}}}
    it = Apple._row(row, TRACK)
    assert it.catalog_id is None and it.code is None


def test_album_row():
    row = {"id": "l.xyz", "type": "library-albums",
           "attributes": {"name": "Discovery", "artistName": "Daft Punk", "playParams": {"catalogId": "697194953"}},
           "relationships": {"catalog": {"data": [{"id": "697194953", "type": "albums",
                                                     "attributes": {"upc": "0724384960650"}}]}}}
    it = Apple._row(row, ALBUM)
    assert it.upc == "0724384960650" and it.code == "0724384960650" and it.catalog_id == "697194953"
