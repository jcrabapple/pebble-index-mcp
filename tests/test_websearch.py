import httpx
import pytest

from pebble_index_mcp.websearch import ExaClient, ExaError


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = ""

    def json(self):
        return self._body


def _client():
    return ExaClient(api_key="exa-key-123")


def test_search_success_formats_results(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(
            200,
            {
                "results": [
                    {"title": "Result A", "url": "https://a.example/x", "text": "Snippet A"},
                    {"title": "Result B", "url": "https://b.example/y", "text": "Snippet B"},
                ]
            },
        )

    monkeypatch.setattr("pebble_index_mcp.websearch.httpx.post", fake_post)
    out = _client().search("who won the game", num_results=2)
    assert "Result A" in out and "https://a.example/x" in out and "Snippet A" in out
    assert "Result B" in out
    assert captured["url"] == "https://api.exa.ai/search"
    assert captured["headers"]["x-api-key"] == "exa-key-123"
    assert captured["json"]["numResults"] == 2
    assert captured["json"]["query"] == "who won the game"


def test_search_snippet_truncated(monkeypatch):
    long_text = "x" * 5000

    def fake_post(**kwargs):
        return FakeResponse(
            200, {"results": [{"title": "T", "url": "https://a", "text": long_text}]}
        )

    monkeypatch.setattr("pebble_index_mcp.websearch.httpx.post", fake_post)
    out = _client().search("q")
    assert out.count("x") < 1000


def test_search_no_results(monkeypatch):
    monkeypatch.setattr(
        "pebble_index_mcp.websearch.httpx.post",
        lambda **kwargs: FakeResponse(200, {"results": []}),
    )
    out = _client().search("q")
    assert "No results" in out


def test_search_http_error(monkeypatch):
    class BodyResponse(FakeResponse):
        def __init__(self):
            super().__init__(401)
            self.text = "bad key"

    monkeypatch.setattr(
        "pebble_index_mcp.websearch.httpx.post", lambda **kwargs: BodyResponse()
    )
    with pytest.raises(ExaError) as exc:
        _client().search("q")
    assert "HTTP 401" in str(exc.value)


def test_search_timeout(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("pebble_index_mcp.websearch.httpx.post", fake_post)
    with pytest.raises(ExaError):
        _client().search("q")


def test_search_connect_error(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("pebble_index_mcp.websearch.httpx.post", fake_post)
    with pytest.raises(ExaError):
        _client().search("q")


def test_search_bad_shape(monkeypatch):
    monkeypatch.setattr(
        "pebble_index_mcp.websearch.httpx.post",
        lambda **kwargs: FakeResponse(200, {"nope": 1}),
    )
    with pytest.raises(ExaError):
        _client().search("q")


def test_missing_key_short_circuits():
    c = ExaClient(api_key="")
    assert "not configured" in c.search("q").lower()
