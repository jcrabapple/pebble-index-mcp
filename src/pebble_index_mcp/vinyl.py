"""Vinyl catalog client for vinyl_lookup (record-store duplicate checks).

Talks to the user's self-hosted catalog API (FastAPI + sqlite FTS). The
base URL is operator config (VINYL_URL), never a committed default, so the
public repo stays free of personal deployment hostnames.

Three modes routed off the command text:
- "random" -> /api/random (dormancy-weighted pick)
- "top"/"most played" -> /api/plays/top
- anything else -> /api/records?q=... (artist/album full-text search)
Conversational filler ("do I own", "have I got") is stripped before the
search so FTS gets real terms.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

_TIMEOUT = 10.0
_MAX_RESULTS = 5

_SEARCH_FILLER = frozenset(
    """do does did i my me we own owned have has had got get buy bought buying
    a an the any some of in on at record records album albums lp vinyl
    what whats is there to for already by random pick suggest top most
    played plays""".split()
)


class VinylError(Exception):
    pass


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


class VinylClient:
    def __init__(self, url: str = "", timeout: float = _TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout

    # -- transport -----------------------------------------------------------

    def _get_raw(self, url: str) -> tuple[int, Any]:
        r = httpx.get(url, timeout=self.timeout)
        return r.status_code, _safe_json(r)

    def _get(self, path: str) -> Any:
        code, body = self._get_raw(f"{self.url}{path}")
        if code != 200:
            raise VinylError(f"Catalog HTTP {code} on GET {path}")
        return body

    # -- command routing ------------------------------------------------------

    def _search_terms(self, command: str) -> list[str]:
        # Keep RAW words for display; plural/possessive tolerance comes from
        # FTS5 prefix queries (see _fts_query), never from stripping letters.
        words = re.sub(r"[^a-z0-9 ]+", " ", command.lower()).split()
        return [w for w in words if w not in _SEARCH_FILLER]

    @staticmethod
    def _fts_query(terms: list[str]) -> str:
        # "crisis" -> "crisi*": matches crisis/crises; "bodies" -> "bodie*"
        # matches "bodies". Stripping an s would corrupt non-plural words
        # (crisis -> crisi) and FTS5 has no stemmer, so prefix instead.
        return " ".join(_singular(t) + "*" for t in terms)

    def lookup(self, command: str) -> str:
        """Route one lookup command and return a short spoken result."""
        if not self.url:
            raise VinylError("Vinyl lookup unavailable: VINYL_URL not configured.")
        t = re.sub(r"[^a-z0-9 ]+", " ", command.lower())
        if re.search(r"\brandom\b|\bpick\b|\bsuggest\b", t):
            return self._random()
        if re.search(r"\btop\b|\bmost\b|\bplayed\b|\bplays\b", t):
            return self._top()
        terms = self._search_terms(command)
        if not terms:
            raise VinylError(
                'Name an artist or album (e.g. "do I own Eternal Blue by Spiritbox?").'
            )
        return self._search(terms)

    # -- modes ----------------------------------------------------------------

    def _search(self, terms: list[str]) -> str:
        path = "/api/records?" + str(
            httpx.QueryParams({"q": self._fts_query(terms), "limit": "100"})
        )
        albums = self._get(path)
        if not isinstance(albums, list) or not albums:
            return f"You do not own anything matching '{' '.join(terms)}'."
        lines = [
            _album_line(a) for a in albums[:_MAX_RESULTS]
        ]
        prefix = (
            "You own it:"
            if len(albums) == 1
            else f"You own {len(albums)} albums:"
        )
        result = prefix + " " + "; ".join(lines) + "."
        if len(albums) > _MAX_RESULTS:
            result += f" And {len(albums) - _MAX_RESULTS} more."
        return result

    def _random(self) -> str:
        album = self._get("/api/random")
        return f"Spin this: {(_album_line(album))}."

    def _top(self) -> str:
        rows = self._get("/api/plays/top?limit=5")
        if not isinstance(rows, list) or not rows:
            return "No plays logged yet."
        lines = [
            f"{r.get('artist', '?')} - {r.get('album', '?')} ({r.get('play_count', '?')} plays)"
            for r in rows[:_MAX_RESULTS]
        ]
        return "Top plays: " + "; ".join(lines) + "."


def _album_line(a: dict) -> str:
    artist = a.get("artist") or "?"
    album = a.get("album") or "?"
    year = a.get("year")
    pressings = a.get("pressing_count")
    bits = []
    if year:
        bits.append(str(year))
    if pressings and pressings > 1:
        bits.append(f"{pressings} pressings")
    suffix = f" ({', '.join(bits)})" if bits else ""
    return f"{artist} - {album}{suffix}"


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:200]
