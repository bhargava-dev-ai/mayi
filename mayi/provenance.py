"""
Heuristic detection of values copied out of untrusted text.

This is NOT full taint tracking: mayi sees only the strings you register as
untrusted and the arguments the model proposes, not what happens in between.
It catches the common injection shape -- an email address, URL, ID or phrase
the model lifted from an email, web page or document into a sensitive
argument -- including copies that were lightly disguised in the source
(different case, odd whitespace, zero-width characters, HTML entities,
percent-encoding, base64 blobs, "name at domain dot com" spellings). A value
the model paraphrased, translated, split across fields or computed from the
text will not be caught.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata
import urllib.parse
from typing import Iterable

MIN_LEN = 4  # shorter values match innocent text far too often to be a useful signal

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_OBFUSCATED_AT = re.compile(r"\s*[\[(]\s*at\s*[\])]\s*|\s+at\s+", re.I)
_OBFUSCATED_DOT = re.compile(r"\s*[\[(]\s*dot\s*[\])]\s*|\s+dot\s+", re.I)
_B64 = re.compile(r"[A-Za-z0-9+/_\-]{16,}={0,2}")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", s).strip().casefold()


def _variants(text: str) -> set[str]:
    """The text as written plus decoded readings of it, all normalised."""
    out = {text}
    cur = text
    for _ in range(2):  # a couple of layers of encoding is plenty
        cur = urllib.parse.unquote(html.unescape(cur))
        out.add(cur)
    for m in _B64.finditer(text):
        blob = m.group(0)
        pad = blob + "=" * (-len(blob) % 4)
        for dec in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                raw = dec(pad)
                s = raw.decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                continue
            if s.isprintable():
                out.add(s)
    for v in list(out):
        d = _OBFUSCATED_DOT.sub(".", _OBFUSCATED_AT.sub("@", v))
        out.add(d)
    return {_norm(v) for v in out}


def _entities(norm_text: str) -> set[str]:
    return set(_EMAIL.findall(norm_text)) | set(_URL.findall(norm_text))


def copied_from(value: object, texts: Iterable[str]) -> bool:
    """True if a string `value` was lifted from any of the untrusted `texts`.

    Matches when the (normalised) value appears inside any reading of a text, or when
    the value contains an email address or URL that appears in the text.
    """
    if not isinstance(value, str):
        return False
    v = _norm(value)
    if len(v) < MIN_LEN:
        return False
    v_ents = _entities(v)
    for t in texts:
        if not isinstance(t, str):
            continue
        for reading in _variants(t):
            if v in reading:
                return True
            if v_ents and v_ents & _entities(reading):
                return True
    return False
