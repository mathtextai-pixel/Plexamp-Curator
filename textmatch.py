"""
Name matching shared by vault/channel selection and Lidarr lookups.

`normalize` folds case, treats "&" as "and", drops dots/apostrophes (R.E.M. -> rem)
and turns other punctuation into spaces. `contains_phrase` then matches whole
words only, so the keyword "pup" matches "PUP" but not "Puppy", and "wire"
matches "Wire" but not "Wireless".
"""

import re

_DROP = re.compile(r"[.'’`]")
_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    n = (text or "").casefold().replace("&", " and ")
    n = _DROP.sub("", n)
    n = _NON_WORD.sub(" ", n)
    return _SPACES.sub(" ", n).strip()


def contains_phrase(haystack: str, phrase: str) -> bool:
    """True if `phrase` appears in `haystack` as whole words (after normalising both)."""
    p = normalize(phrase)
    return bool(p) and f" {p} " in f" {normalize(haystack)} "


def contains_any_phrase(haystack: str, phrases) -> bool:
    h = f" {normalize(haystack)} "
    for phrase in phrases:
        p = normalize(phrase)
        if p and f" {p} " in h:
            return True
    return False
