"""Pairs seen in the real libraries that must normalize equal."""
import pytest

from music_sync.models import normalize, primary_artist


@pytest.mark.parametrize("a, b", [
    ("After Midnight - Supertaste Remix", "After Midnight (Supertaste Remix)"),
    ("Body Over Here - Club Version", "Body Over Here (Club Version)"),
    ("Up All Night - Oliver Remix", "Up All Night (Oliver Remix)"),
    ("Star-Crossed", "Starcrossed"),
    ("Ooh Nah Nah (feat. Masego)", "Ooh Nah Nah"),
    ("Four Fifths - Tiny Room Sessions", "Four Fifths (feat. Ruslan Sirota & Chesley Allen)"),
    ("Hold On, We\u2019re Going Home", "Hold On, We're Going Home"),
    ("Caf\u00e9 del Mar", "Cafe Del Mar"),
])
def test_titles_fold_together(a, b):
    assert normalize(a) == normalize(b)


@pytest.mark.parametrize("a, b", [
    ("The Dave Weckl Band", "Dave Weckl Band"),
    ("JON VINYL", "Jon Vinyl"),
    ("Drake, Majid Jordan", "Drake & Majid Jordan"),
    ("KAYTRANADA, Rochelle Jordan", "KAYTRANADA & Rochelle Jordan"),
])
def test_artists_fold_together(a, b):
    assert normalize(primary_artist(a)) == normalize(primary_artist(b))


def test_different_songs_stay_apart():
    assert normalize("Meet Me At Your Place") != normalize("All I Need")
    assert normalize("Slant") != normalize("Big Duck")
