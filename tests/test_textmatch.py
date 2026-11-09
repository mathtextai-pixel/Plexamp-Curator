import pytest

from textmatch import contains_any_phrase, contains_phrase, normalize


@pytest.mark.parametrize("haystack,phrase,expected", [
    ("PUP", "pup", True),
    ("Puppy", "pup", False),
    ("Wire", "wire", True),
    ("Wireless", "wire", False),
    ("SAULT", "sault", True),
    ("Assault Unit", "sault", False),
    ("The Internet", "internet", True),
    ("R.E.M.", "r.e.m.", True),
    ("Khruangbin & Leon Bridges", "leon bridges", True),
    ("Drive-By Truckers", "truckers", True),
    ("Durand Jones & The Indications", "durand jones", True),
    ("The 1975", "the 1975", True),
    ("Sigur Rós", "sigur rós", True),
    ("Anything", "", False),
])
def test_contains_phrase(haystack, phrase, expected):
    assert contains_phrase(haystack, phrase) is expected


def test_contains_any_phrase():
    assert contains_any_phrase("Gillian Welch", ["prine", "welch"])
    assert not contains_any_phrase("Welcher", ["welch"])


def test_normalize():
    assert normalize("  Guns N’ Roses ") == "guns n roses"
    assert normalize("AC/DC") == "ac dc"
