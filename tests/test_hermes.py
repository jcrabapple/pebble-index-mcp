import httpx
import pytest

from pebble_index_mcp.hermes import HermesClient, HermesError, HermesTimeout


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def _stub_post(monkeypatch, client, fake_post):
    monkeypatch.setattr(client._client, "post", fake_post)


def test_compose_answer_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return FakeResponse(200, {"choices": [{"message": {"content": "  The launch is Friday.  "}}]})

    c = HermesClient("http://x/v1/chat/completions", "key123", "pebble-ring")
    _stub_post(monkeypatch, c, fake_post)
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

    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, fake_post)
    with pytest.raises(HermesTimeout):
        c.compose_answer("q", "results")


def test_compose_answer_http_error(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.ConnectError("refused")

    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, fake_post)
    with pytest.raises(HermesError):
        c.compose_answer("q", "results")


def test_ask_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return FakeResponse(200, {"choices": [{"message": {"content": "  hi  "}}]})

    c = HermesClient("http://x/v1/chat/completions", "key123", "pebble-ring")
    _stub_post(monkeypatch, c, fake_post)
    assert c.ask("q") == "hi"
    assert captured["headers"]["Authorization"] == "Bearer key123"
    assert captured["json"]["model"] == "pebble-ring"
    assert captured["json"]["max_tokens"] == 200
    assert "provider" not in captured["json"]
    assert "3 sentences" in captured["json"]["messages"][0]["content"]


def test_openrouter_provider_order(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]})

    c = HermesClient(
        "https://openrouter.ai/api/v1/chat/completions",
        "k",
        "deepseek/deepseek-v4-flash",
        provider_order="Crusoe, Cloudflare",
    )
    _stub_post(monkeypatch, c, fake_post)
    assert c.ask("q") == "hi"
    assert captured["json"]["provider"] == {
        "order": ["Crusoe", "Cloudflare"],
        "allow_fallbacks": True,
    }


def test_hermes_agent_model_and_reasoning_overrides(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResponse(200, {"choices": [{"message": {"content": "done"}}]})

    c = HermesClient(
        "http://hermes/v1/chat/completions",
        "k",
        "z-ai/glm-5.3-flashx",
        api_provider="openrouter",
        reasoning_effort="low",
    )
    _stub_post(monkeypatch, c, fake_post)
    assert c.ask("q") == "done"
    assert captured["json"]["provider"] == "openrouter"
    assert captured["json"]["model_options"] == {"reasoning_effort": "low"}


def test_ask_timeout(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.TimeoutException("slow")

    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, fake_post)
    with pytest.raises(HermesTimeout):
        c.ask("q")


def test_ask_http_error(monkeypatch):
    class BodyResponse(FakeResponse):
        def __init__(self):
            super().__init__(500)
            self.text = "upstream exploded"

    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, lambda **kwargs: BodyResponse())
    with pytest.raises(HermesError) as exc:
        c.ask("q")
    assert "HTTP 500" in str(exc.value)
    assert "upstream exploded" in str(exc.value)


def test_ask_bad_shape(monkeypatch):
    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, lambda **kwargs: FakeResponse(200, {"nope": 1}))
    with pytest.raises(HermesError):
        c.ask("q")


def test_ask_non_json_body(monkeypatch):
    class BadJson(FakeResponse):
        def json(self):
            raise ValueError("bad json")

    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, lambda **kwargs: BadJson(200))
    with pytest.raises(HermesError):
        c.ask("q")


def test_ask_connect_error(monkeypatch):
    def fake_post(**kwargs):
        raise httpx.ConnectError("connection refused")

    c = HermesClient("http://x", "k", "m")
    _stub_post(monkeypatch, c, fake_post)
    with pytest.raises(HermesError):
        c.ask("q")
