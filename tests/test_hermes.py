import httpx
import pytest

from pebble_index_mcp.hermes import HermesClient, HermesError, HermesTimeout


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def test_compose_answer_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse(200, {"choices": [{"message": {"content": "  The launch is Friday.  "}}]})

    monkeypatch.setattr("pebble_index_mcp.hermes.httpx.post", fake_post)
    c = HermesClient("http://x/v1/chat/completions", "key123", "pebble-ring")
    out = c.compose_answer("when is the launch", "SpaceX (https://x)\nLaunch Friday")
    assert out == "The launch is Friday."
    assert captured["json"]["model"] == "pebble-ring"
    system = captured["json"]["messages"][0]["content"]
    user = captured["json"]["messages"][1]["content"]
    assert "search results" in system.lower()
    assert "when is the launch" in user
    assert "Launch Friday" in user


def test_compose_answer_timeout(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("pebble_index_mcp.hermes.httpx.post", fake_post)
    c = HermesClient("http://x", "k", "m")
    with pytest.raises(HermesTimeout):
        c.compose_answer("q", "results")


def test_compose_answer_http_error(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("pebble_index_mcp.hermes.httpx.post", fake_post)
    c = HermesClient("http://x", "k", "m")
    with pytest.raises(HermesError):
        c.compose_answer("q", "results")


def test_ask_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(200, {"choices": [{"message": {"content": "  hi  "}}]})

    monkeypatch.setattr("pebble_index_mcp.hermes.httpx.post", fake_post)
    c = HermesClient("http://x/v1/chat/completions", "key123", "pebble-ring")
    assert c.ask("q") == "hi"
    assert captured["headers"]["Authorization"] == "Bearer key123"
    assert captured["json"]["model"] == "pebble-ring"
    assert "3 sentences" in captured["json"]["messages"][0]["content"]


def test_ask_timeout(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr("pebble_index_mcp.hermes.httpx.post", fake_post)
    with pytest.raises(HermesTimeout):
        HermesClient("http://x", "k", "m").ask("q")


def test_ask_http_error(monkeypatch):
    class BodyResponse(FakeResponse):
        def __init__(self):
            super().__init__(500)
            self.text = "upstream exploded"

    monkeypatch.setattr(
        "pebble_index_mcp.hermes.httpx.post", lambda **kwargs: BodyResponse()
    )
    with pytest.raises(HermesError) as exc:
        HermesClient("http://x", "k", "m").ask("q")
    assert "HTTP 500" in str(exc.value)
    assert "upstream exploded" in str(exc.value)


def test_ask_bad_shape(monkeypatch):
    monkeypatch.setattr(
        "pebble_index_mcp.hermes.httpx.post",
        lambda **kwargs: FakeResponse(200, {"nope": 1}),
    )
    with pytest.raises(HermesError):
        HermesClient("http://x", "k", "m").ask("q")


def test_ask_non_json_body(monkeypatch):
    class BadJson(FakeResponse):
        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(
        "pebble_index_mcp.hermes.httpx.post", lambda **kwargs: BadJson(200)
    )
    with pytest.raises(HermesError):
        HermesClient("http://x", "k", "m").ask("q")


def test_ask_connect_error(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("pebble_index_mcp.hermes.httpx.post", fake_post)
    with pytest.raises(HermesError):
        HermesClient("http://x", "k", "m").ask("q")
