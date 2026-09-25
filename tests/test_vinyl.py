import pytest

from pebble_index_mcp.vinyl import VinylClient, VinylError


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = ""

    def json(self):
        return self._body


def _album(artist="Spiritbox", album="Eternal Blue", year=2021, count=1, **extra):
    d = {
        "artist": artist,
        "album": album,
        "year": year,
        "edition": "blue marble 2xLP",
        "format": "12in",
        "pressing_count": count,
    }
    d.update(extra)
    return d


def _client():
    return VinylClient(url="https://vinyl.test", timeout=5.0)


# --- command routing / query cleanup ---------------------------------------

def test_search_terms_strip_conversational_filler():
    c = _client()
    assert c._search_terms("do I own Eternal Blue by Spiritbox") == "eternal blue spiritbox"
    assert c._search_terms("random record") == ""
    assert c._search_terms("   ") == ""


def test_lookup_routes_random(monkeypatch):
    c = _client()
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return 200, _album()

    monkeypatch.setattr(c, "_get_raw", fake_get)
    out = c.lookup("pick a random record")
    assert any("/api/random" in u for u in calls)
    assert "Eternal Blue" in out


def test_lookup_routes_top_plays(monkeypatch):
    c = _client()
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return 200, [
            {"artist": "Bilmuri", "album": "American Motor Sports", "play_count": 12},
            {"artist": "Spiritbox", "album": "Eternal Blue", "play_count": 9},
        ]

    monkeypatch.setattr(c, "_get_raw", fake_get)
    out = c.lookup("what are my most played records")
    assert any("/api/plays/top" in u for u in calls)
    assert "Bilmuri" in out and "12 plays" in out


def test_lookup_search_found(monkeypatch):
    c = _client()
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return 200, [_album(count=2)]

    monkeypatch.setattr(c, "_get_raw", fake_get)
    out = c.lookup("do I own Eternal Blue")
    assert "/api/records" in calls[0] and "eternal+blue" in calls[0]
    assert "You own it" in out and "2 pressings" in out


def test_lookup_search_multiple(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c, "_get_raw",
        lambda url, **kw: (
            200,
            [_album("Spiritbox", "Eternal Blue"), _album("Spiritbox", "Rotoscope")],
        ),
    )
    out = c.lookup("spiritbox")
    assert "2 albums" in out and "Rotoscope" in out


def test_lookup_search_no_match(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c, "_get_raw", lambda url, **kw: (200, [])
    )
    out = c.lookup("some album i heard about")
    assert "do not" in out.lower() or "don't" in out.lower()


def test_lookup_empty_terms(monkeypatch):
    c = _client()
    with pytest.raises(VinylError):
        c.lookup("own")


def test_lookup_http_error(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_get_raw", lambda url, **kw: (500, "boom"))
    with pytest.raises(VinylError):
        c.lookup("spiritbox")


def test_search_result_capped(monkeypatch):
    c = _client()
    many = [_album("Aardvark", f"Album {i}") for i in range(30)]
    monkeypatch.setattr(
        c, "_get_raw", lambda url, **kw: (200, many)
    )
    out = c.lookup("aardvark")
    assert "25 more" in out
