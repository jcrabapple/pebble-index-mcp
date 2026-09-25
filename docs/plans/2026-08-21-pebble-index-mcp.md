# Pebble Index MCP Bridge — Implementation Plan

**Goal:** Build the pebble-index-mcp service: a bearer-token-protected Streamable HTTP MCP server exposing 4 tools (vault_search, vault_read, vault_append, ask_hermes) plus a persona prompt, deployed on the desktop behind a cloudflared tunnel, wired into the Pebble app's double-click sandbox.

**Architecture:** Python FastMCP app (official mcp SDK, `mcp.server.fastmcp`) with a Starlette auth middleware, run by systemd as a user service on 127.0.0.1:8765. A cloudflared named tunnel exposes it publicly. ask_hermes calls the Hermes API server on localhost:8642 with model alias `pebble-ring`, routed via `model_routes` to deepseek-v4-flash.

**Tech Stack:** Python 3.14 (system), mcp SDK >=1.10 (FastMCP), httpx, starlette, uvicorn, pytest + pytest-asyncio, ripgrep, systemd user units, cloudflared.

**Reference:** spec at `docs/superpowers/specs/2026-08-21-pebble-index-mcp-design.md`. Verified facts: API server on 127.0.0.1:8642 returns OpenAI-shaped JSON (3.5s trivial round trip, ~34K prompt tokens/call); `model_routes` schema confirmed in api_server.py; cloudflared pattern from `cloudflared-hermes-webui.service`; `API_SERVER_KEY` lives in `~/.hermes/.env`.

---

### Task 1: Project scaffold

**Objective:** Create the repo skeleton and a working venv.

**Files:**
- Create: `pyproject.toml`
- Create: `src/pebble_index_mcp/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)

**Step 1: Write `pyproject.toml`**

```toml
[project]
name = "pebble-index-mcp"
version = "0.1.0"
description = "MCP bridge exposing the Obsidian vault and Hermes to the Pebble Index 01 ring"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.10,<3",
    "httpx>=0.27",
    "starlette>=0.40",
    "uvicorn>=0.30",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 2: Create venv and install**

Run: `cd ~/projects/pebble-index-mcp && python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"`
Expected: install completes. If a wheel fails on Python 3.14, retry with `python3.12 -m venv .venv` (if 3.12 exists) and note it in the commit message.

**Step 3: Verify import**

Run: `.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print(FastMCP.__name__)"`
Expected: prints `FastMCP`. If `streamable_http_app` is missing on this FastMCP (check: `.venv/bin/python -c "from mcp.server.fastmcp import FastMCP; print(hasattr(FastMCP, 'streamable_http_app'))"` → True), pin `mcp==1.29.0` in pyproject and reinstall.

**Step 4: Commit**

```bash
git add pyproject.toml src/pebble_index_mcp/__init__.py tests/__init__.py
git commit -m "chore: scaffold pebble-index-mcp project"
```

---

### Task 2: Vault path sandbox

**Objective:** `Vault._resolve` with traversal, absolute-path, and symlink-escape rejection.

**Files:**
- Create: `src/pebble_index_mcp/vault.py`
- Create: `tests/test_vault.py`

**Step 1: Write `vault.py` skeleton + `_resolve`**

```python
"""Obsidian vault access with path sandboxing."""
from __future__ import annotations

from pathlib import Path


class VaultAccessError(Exception):
    pass


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _resolve(self, rel: str) -> Path:
        if not rel or not rel.strip():
            raise VaultAccessError("Empty path")
        p = Path(rel)
        if p.is_absolute():
            raise VaultAccessError("Absolute paths not allowed")
        full = (self.root / p).resolve()
        if full != self.root and not full.is_relative_to(self.root):
            raise VaultAccessError("Path outside vault")
        return full
```

**Step 2: Write failing tests** (`tests/test_vault.py`)

```python
import pytest
from pathlib import Path

from pebble_index_mcp.vault import Vault, VaultAccessError


@pytest.fixture()
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.root.mkdir(parents=True)
    return v


class TestResolve:
    def test_normal_relative_path(self, vault):
        assert vault._resolve("notes/idea.md") == vault.root / "notes" / "idea.md"

    def test_absolute_path_rejected(self, vault):
        with pytest.raises(VaultAccessError):
            vault._resolve("/etc/passwd")

    def test_parent_traversal_rejected(self, vault):
        with pytest.raises(VaultAccessError):
            vault._resolve("../escape.md")

    def test_symlink_escape_rejected(self, vault):
        (vault.root / "notes").mkdir()
        outside = vault.root.parent / "outside.txt"
        outside.write_text("secret")
        (vault.root / "notes" / "link.md").symlink_to(outside)
        with pytest.raises(VaultAccessError):
            vault._resolve("notes/link.md")

    def test_symlink_inside_vault_ok(self, vault):
        (vault.root / "a.md").write_text("hello")
        (vault.root / "b.md").symlink_to(vault.root / "a.md")
        assert vault._resolve("b.md").is_file()
```

**Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_vault.py::TestResolve -v`
Expected: 5 passed.

**Step 4: Commit**

```bash
git add src/pebble_index_mcp/vault.py tests/test_vault.py
git commit -m "feat: vault path sandbox with symlink-escape rejection"
```

---

### Task 3: vault_append

**Objective:** Append-only writes: create file + parents, timestamped `- HH:MM text` line, no overwrite.

**Files:**
- Modify: `src/pebble_index_mcp/vault.py`
- Modify: `tests/test_vault.py`

**Step 1: Add `append` to `Vault`**

```python
    def append(self, rel: str, text: str) -> str:
        if not text.strip():
            raise VaultAccessError("Empty text")
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        import datetime

        stamp = datetime.datetime.now().strftime("%H:%M")
        line = f"- {stamp} {text.strip()}"
        try:
            if p.exists() and p.stat().st_size > 0:
                line = "\n" + line
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            raise VaultAccessError(str(e)) from e
        return f"Appended to {rel}"
```

**Step 2: Add failing tests** (`TestAppend` class in tests/test_vault.py)

```python
class TestAppend:
    def test_creates_file_and_dirs(self, vault):
        out = vault.append("Writing/ideas.md", "ring idea")
        p = vault.root / "Writing" / "ideas.md"
        assert p.is_file()
        assert out == "Appended to Writing/ideas.md"
        assert "ring idea" in p.read_text()

    def test_appends_to_existing(self, vault):
        p = vault.root / "notes.md"
        p.write_text("- 08:00 first\n")
        vault.append("notes.md", "second")
        lines = p.read_text().strip().splitlines()
        assert len(lines) == 2 and lines[1].endswith("second")

    def test_rejects_empty_text(self, vault):
        with pytest.raises(VaultAccessError):
            vault.append("n.md", "   ")

    def test_rejects_traversal(self, vault):
        with pytest.raises(VaultAccessError):
            vault.append("../evil.md", "x")
```

**Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_vault.py::TestAppend -v`
Expected: 4 passed.

**Step 4: Commit**

```bash
git add src/pebble_index_mcp/vault.py tests/test_vault.py
git commit -m "feat: vault_append with timestamped appends"
```

---

### Task 4: vault_read

**Objective:** Read with truncation and clean errors.

**Files:**
- Modify: `src/pebble_index_mcp/vault.py`
- Modify: `tests/test_vault.py`

**Step 1: Add `read` to `Vault`**

```python
    def read(self, rel: str, max_chars: int = 1500) -> str:
        max_chars = max(1, min(int(max_chars), 4000))
        p = self._resolve(rel)
        if not p.is_file():
            raise VaultAccessError(f"Note not found: {rel}")
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[:max_chars] + ("..." if len(text) > max_chars else "")
```

**Step 2: Add failing tests** (`TestRead` class)

```python
class TestRead:
    def test_reads_note(self, vault):
        (vault.root / "n.md").write_text("hello world")
        assert vault.read("n.md") == "hello world"

    def test_missing_note(self, vault):
        with pytest.raises(VaultAccessError):
            vault.read("nope.md")

    def test_truncates(self, vault):
        (vault.root / "n.md").write_text("x" * 100)
        assert vault.read("n.md", max_chars=10) == "x" * 10 + "..."
```

**Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_vault.py::TestRead -v`
Expected: 3 passed.

**Step 4: Commit**

```bash
git add src/pebble_index_mcp/vault.py tests/test_vault.py
git commit -m "feat: vault_read with truncation"
```

---

### Task 5: vault_search

**Objective:** ripgrep-backed search, one excerpt per file, hidden files excluded.

**Files:**
- Modify: `src/pebble_index_mcp/vault.py`
- Modify: `tests/test_vault.py`

**Step 1: Add `search` to `Vault`**

```python
import subprocess  # top of file

    def search(self, query: str, max_results: int = 5) -> str:
        max_results = max(1, min(int(max_results), 10))
        files = subprocess.run(
            ["rg", "-l", "-i", "--glob", "!.obsidian", "--", query, str(self.root)],
            capture_output=True, text=True, timeout=15,
        )
        paths = [line for line in files.stdout.splitlines() if line.strip()][:max_results]
        if not paths:
            return "No matches."
        out = []
        for p in paths:
            try:
                rel = str(Path(p).relative_to(self.root))
            except ValueError:
                continue
            excerpt = subprocess.run(
                ["rg", "-i", "--max-count", "1", "--", query, p],
                capture_output=True, text=True, timeout=15,
            )
            first = excerpt.stdout.splitlines()[0].strip() if excerpt.stdout.strip() else ""
            out.append(f"{rel}: {first[:200]}")
        return "\n".join(out)
```

(ripgrep skips hidden files/dirs by default, which covers `.git`, `.obsidian`, and dotfiles; the explicit glob is belt-and-braces.)

**Step 2: Add failing tests** (`TestSearch` class)

```python
class TestSearch:
    def test_finds_match(self, vault):
        (vault.root / "mead.md").write_text("racked the cyser today")
        out = vault.search("cyser")
        assert "mead.md" in out and "racked" in out

    def test_no_match(self, vault):
        assert vault.search("zzzznope") == "No matches."

    def test_excludes_hidden_dirs(self, vault):
        (vault.root / ".obsidian").mkdir()
        (vault.root / ".obsidian" / "cfg.json").write_text("cyser cfg")
        (vault.root / "real.md").write_text("no cyser here")
        out = vault.search("cyser")
        assert "real.md" not in out
```

**Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_vault.py::TestSearch -v`
Expected: 3 passed.

**Step 4: Commit**

```bash
git add src/pebble_index_mcp/vault.py tests/test_vault.py
git commit -m "feat: vault_search via ripgrep"
```

---

### Task 6: Hermes client

**Objective:** `HermesClient.ask` with 3-sentence instruction postfix, timeout and error taxonomy.

**Files:**
- Create: `src/pebble_index_mcp/hermes.py`
- Create: `tests/test_hermes.py`

**Step 1: Write `hermes.py`**

```python
"""Hermes API client for ask_hermes."""
from __future__ import annotations

import httpx

ANSWER_HINT = "\n\nAnswer in at most 3 sentences. The answer will be shown as a phone notification."


class HermesTimeout(Exception):
    pass


class HermesError(Exception):
    pass


class HermesClient:
    def __init__(self, url: str, api_key: str, model: str, timeout: float = 60.0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def ask(self, question: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": question + ANSWER_HINT}],
        }
        try:
            r = httpx.post(
                self.url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise HermesTimeout(str(e)) from e
        if r.status_code != 200:
            raise HermesError(f"HTTP {r.status_code}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise HermesError(f"Unexpected response shape: {type(e).__name__}") from e
```

**Step 2: Write failing tests** (`tests/test_hermes.py`)

```python
import httpx
import pytest

from pebble_index_mcp.hermes import HermesClient, HermesError, HermesTimeout


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


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
    monkeypatch.setattr(
        "pebble_index_mcp.hermes.httpx.post", lambda **kwargs: FakeResponse(500)
    )
    with pytest.raises(HermesError):
        HermesClient("http://x", "k", "m").ask("q")


def test_ask_bad_shape(monkeypatch):
    monkeypatch.setattr(
        "pebble_index_mcp.hermes.httpx.post",
        lambda **kwargs: FakeResponse(200, {"nope": 1}),
    )
    with pytest.raises(HermesError):
        HermesClient("http://x", "k", "m").ask("q")
```

**Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_hermes.py -v`
Expected: 4 passed.

**Step 4: Commit**

```bash
git add src/pebble_index_mcp/hermes.py tests/test_hermes.py
git commit -m "feat: Hermes API client with timeout taxonomy"
```

---

### Task 7: FastMCP app — tools and prompt

**Objective:** Register the 4 tools and the persona prompt; expose the ASGI app behind auth middleware.

**Files:**
- Create: `src/pebble_index_mcp/server.py`
- Create: `tests/test_server.py`

**Step 1: Write `server.py`**

```python
"""Pebble Index MCP bridge: vault + Hermes tools over Streamable HTTP."""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .hermes import HermesClient, HermesError, HermesTimeout
from .vault import Vault, VaultAccessError

VAULT = Vault(os.environ.get("VAULT_PATH", "/home/<user>/Documents/Obsidian Vault"))
TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
HERMES = HermesClient(
    url=os.environ.get("HERMES_API_URL", "http://127.0.0.1:8642/v1/chat/completions"),
    api_key=os.environ.get("HERMES_API_KEY", ""),
    model=os.environ.get("RING_MODEL", "pebble-ring"),
)

mcp = FastMCP("pebble-index-mcp")


@mcp.tool()
def vault_search(query: str, max_results: int = 5) -> str:
    """Search the user's Obsidian vault (case-insensitive text search).

    Use when the user asks where something is or whether a note exists.
    Returns "relative/path.md: excerpt" lines. Facts about the vault must
    come from this tool or vault_read; never invent them.
    """
    return VAULT.search(query, max_results)


@mcp.tool()
def vault_read(note_path: str, max_chars: int = 1500) -> str:
    """Read a note from the Obsidian vault. note_path is relative to the vault
    root, e.g. "Writing/ideas.md". Returns the first max_chars characters."""
    try:
        return VAULT.read(note_path, max_chars)
    except VaultAccessError as e:
        return str(e)


@mcp.tool()
def vault_append(note_path: str, text: str) -> str:
    """Append a line to a note in the Obsidian vault, creating it if needed.

    Use only when the user explicitly asks to save or add something. Appends
    a timestamped "- HH:MM text" line. Never overwrites or deletes."""
    try:
        return VAULT.append(note_path, text)
    except (VaultAccessError, OSError) as e:
        return f"Could not append: {e}"


@mcp.tool()
def ask_hermes(question: str) -> str:
    """Forward a question or task to the user's AI agent (Hermes).

    Use for questions the vault tools cannot answer: lookups, research, or
    tasks. Returns Hermes's final answer (kept short by instruction)."""
    try:
        return HERMES.ask(question)
    except HermesTimeout:
        return (
            "Hermes is still working on it. It will finish shortly; "
            "re-ask or check Telegram."
        )
    except HermesError as e:
        return f"Hermes unavailable ({e})"


@mcp.prompt()
def ring_persona() -> str:
    """Persona for the Pebble cloud agent. Select this prompt in the Pebble
    app's MCP server settings."""
    return (
        "You are a voice assistant for the user, answering through their "
        "Pebble Index ring. The user’s Obsidian vault lives at the vault root; "
        "long-form writing lives under Writing/. Hobbies tracked in the vault: "
        "vinyl records, mead-making, sci-fi writing, electronics.\n"
        "Rules:\n"
        "- Answer in at most 3 short sentences. No markdown. The answer is "
        "shown as a phone notification.\n"
        "- Facts about the vault must come from vault_search or vault_read. "
        "Never invent note contents or file names.\n"
        "- When asked to save something, use vault_append; if the target note "
        "is unclear, use Inbox.md.\n"
        "- For anything beyond the vault, forward to ask_hermes rather than "
        "guessing.\n"
        "- If you do not know, say so."
    )


def make_app():
    async def auth_dispatch(request: Request, call_next):
        if not TOKEN or request.headers.get("Authorization", "") != f"Bearer {TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    return Middleware(mcp.streamable_http_app(), dispatch=auth_dispatch)


app = make_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "pebble_index_mcp.server:app",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8765")),
        log_level="info",
    )
```

**Step 2: Write auth + handshake tests** (`tests/test_server.py`)

```python
import importlib

import httpx
import pytest


@pytest.fixture()
def app_with_token(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    },
}


def test_unauthorized_no_header(app_with_token):
    r = httpx.post(
        "http://test/mcp/", json=INIT_PAYLOAD, transport=httpx.ASGITransport(app=app_with_token)
    )
    assert r.status_code == 401


def test_unauthorized_bad_token(app_with_token):
    r = httpx.post(
        "http://test/mcp/",
        json=INIT_PAYLOAD,
        headers={"Authorization": "Bearer wrong"},
        transport=httpx.ASGITransport(app=app_with_token),
    )
    assert r.status_code == 401


def test_initialize_with_token(app_with_token):
    r = httpx.post(
        "http://test/mcp/",
        json=INIT_PAYLOAD,
        headers={
            "Authorization": "Bearer sekret",
            "Accept": "application/json, text/event-stream",
        },
        transport=httpx.ASGITransport(app=app_with_token),
    )
    assert r.status_code == 200
```

**Step 3: Run tests**

Run: `.venv/bin/pytest tests/test_server.py -v`
Expected: 3 passed. Note: the app binds env at import; the fixture reloads the module after setting the token, which is why `TOKEN` is a module-level read.

**Step 4: Commit**

```bash
git add src/pebble_index_mcp/server.py tests/test_server.py
git commit -m "feat: FastMCP app with vault + Hermes tools, persona prompt, bearer auth"
```

---

### Task 8: Local live E2E

**Objective:** Prove the server works over real HTTP before any deployment.

**Step 1: Start server with a temp vault**

Run (background): `cd ~/projects/pebble-index-mcp && VAULT_PATH=/tmp/ring-test-vault MCP_BEARER_TOKEN=devtoken MCP_PORT=8765 .venv/bin/python -m pebble_index_mcp.server`

Create fixture: `mkdir -p /tmp/ring-test-vault && echo "racked the cyser today" > /tmp/ring-test-vault/mead.md`

**Step 2: Handshake + tools/list**

Run:

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp/ \
  -H "Authorization: Bearer devtoken" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"e2e","version":"0.1"}}}'
```

Expected: 200 with server info `pebble-index-mcp`.

Then list tools (same endpoint, `method: "tools/list"`, note: streamable HTTP sessions may require the `mcp-session-id` header from the initialize response; capture it and pass it back). Expected tool names: `vault_search`, `vault_read`, `vault_append`, `ask_hermes`.

**Step 3: Call vault_search for real**

Run a `tools/call` for `vault_search` with `{"query": "cyser"}`. Expected result contains `mead.md: racked the cyser today`.

**Step 4: Call ask_hermes live**

Run a `tools/call` for `ask_hermes` with `{"question": "What is 2 plus 2?"}`, using env `HERMES_API_KEY=<key from ~/.hermes/.env>` at server start. Expected before Task 10: either a real answer or a 400 from the API server (unknown model `pebble-ring`). Record the observed behavior and timing; it defines Task 10's verification baseline.

**Step 5: Kill the dev server**

Run: `pkill -f pebble_index_mcp.server`

**Step 6: Commit (if any E2E notes were added to docs/)**

No code changes expected; skip commit if none.

---

### Task 9: Gateway model_routes for pebble-ring

**Objective:** Route ring-originated asks to a cheap model.

**Files:**
- Modify: `~/.hermes/config.yaml` (via terminal, NOT write_file/patch — config is protected; use `hermes config` CLI or careful sed on a backup)

**Step 1: Backup config**

Run: `cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak-$(date +%Y%m%d)`

**Step 2: Add the route**

Add under `platforms.api_server.extra` (the enabled api_server block):

```yaml
      model_routes:
        pebble-ring:
          model: deepseek/deepseek-v4-flash
          provider: openrouter
```

**Step 3: Restart gateway**

Run: `hermes gateway restart` (or `systemctl --user restart hermes-gateway.service`)
Expected: gateway healthy (`hermes gateway status`).

**Step 4: Verify route**

Run (key from `~/.hermes/.env`, never echoed):

```bash
curl -sS -m 120 -X POST http://127.0.0.1:8642/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"pebble-ring","messages":[{"role":"user","content":"Reply with one word: pong"}]}'
```

Expected: 200, and `"model"` in the response reflects the routed model (flash alias), not `pebble-ring`. Latency should be comparable to or better than the 4.5s deepseek-v4-pro baseline.

**Step 5: Re-run ask_hermes E2E**

Repeat Task 8 step 4 against the running dev server with `RING_MODEL=pebble-ring`. Expected: real answer, record latency.

**Step 6: Commit plan notes**

Record measured latencies in `docs/plans/2026-08-21-pebble-index-mcp.md` (append a "Measured results" section). Commit if changed.

---

### Task 10: systemd unit + env file

**Objective:** Run the service persistently as a user unit.

**Files:**
- Create: `deploy/pebble-index-mcp.service`
- Create: `~/.config/pebble-index-mcp/env` (chmod 600, NOT committed)

**Step 1: Write unit** (`deploy/pebble-index-mcp.service`)

```ini
[Unit]
Description=Pebble Index MCP bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/projects/pebble-index-mcp
EnvironmentFile=%h/.config/pebble-index-mcp/env
ExecStart=%h/projects/pebble-index-mcp/.venv/bin/python -m pebble_index_mcp.server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

**Step 2: Create env file**

Generate token: `TOKEN=$(openssl rand -hex 32)`; extract `HERMES_API_KEY` value from `~/.hermes/.env`; write both into `~/.config/pebble-index-mcp/env` (0600):

```
VAULT_PATH=/home/<user>/Documents/Obsidian Vault
MCP_HOST=127.0.0.1
MCP_PORT=8765
MCP_BEARER_TOKEN=<generated>
HERMES_API_URL=http://127.0.0.1:8642/v1/chat/completions
HERMES_API_KEY=<from ~/.hermes/.env>
RING_MODEL=pebble-ring
```

**Step 3: Also store the token in Infisical**

Run: `infisical secrets set MCP_BEARER_TOKEN="<generated>"` (project already selected by infisical-login helper). Keeps the credential out of the repo and recoverable.

**Step 4: Install and start**

```bash
mkdir -p ~/.config/systemd/user
cp deploy/pebble-index-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pebble-index-mcp.service
systemctl --user status pebble-index-mcp.service --no-pager | head -6
```

Expected: active (running). Verify: `curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/` → 404 or 405 (server is up; MCP endpoints need POST) — the point is the port answers.

**Step 5: Commit**

```bash
git add deploy/pebble-index-mcp.service
git commit -m "deploy: systemd user unit for pebble-index-mcp"
```

---

### Task 11: Cloudflared tunnel

**Objective:** Public HTTPS for the app.

**Files:**
- Create: `deploy/cloudflared-pebble-index.yml`
- Create: `deploy/cloudflared-pebble-index.service`
- Create (runtime): `~/.cloudflared/pebble-index.yml`, credentials file

**Step 1: Create named tunnel and DNS route**

```bash
cloudflared tunnel login   # only if no existing cert
cloudflared tunnel create pebble-index
cloudflared tunnel route dns pebble-index pebble-index.example.com
```

**Step 2: Write tunnel config** (`deploy/cloudflared-pebble-index.yml`, copy to `~/.cloudflared/pebble-index.yml`)

```yaml
tunnel: pebble-index
credentials-file: ~/.cloudflared/pebble-index.json
ingress:
  - hostname: pebble-index.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

(Path of credentials-file comes from `cloudflared tunnel create` output; adjust if it differs.)

**Step 3: Write unit** (`deploy/cloudflared-pebble-index.service`)

```ini
[Unit]
Description=Cloudflare Tunnel for Pebble Index MCP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel --config %h/.cloudflared/pebble-index.yml run pebble-index
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

**Step 4: Install and verify from the internet**

```bash
cp deploy/cloudflared-pebble-index.service ~/.config/systemd/user/
cp deploy/cloudflared-pebble-index.yml ~/.cloudflared/pebble-index.yml
systemctl --user daemon-reload
systemctl --user enable --now cloudflared-pebble-index.service
```

Verify: `curl -sS -o /dev/null -w "%{http_code}\n" https://pebble-index.example.com/mcp/` → 401 (TLS + auth working end to end). Then repeat the Task 8 handshake with the real bearer token against the public hostname. Expected 200.

**Step 5: Commit**

```bash
git add deploy/cloudflared-pebble-index.yml deploy/cloudflared-pebble-index.service
git commit -m "deploy: cloudflared tunnel for pebble-index MCP"
```

---

### Task 12: README + app setup checklist

**Objective:** User-facing docs: what this is, privacy note, phone setup steps.

**Files:**
- Create: `README.md`
- Create: `docs/ring-checklist.md`

**Step 1: Write `README.md`** covering: what the service does, architecture diagram (from spec), local dev commands, deployment notes, privacy statement (double-click transits Repebble cloud agent + OpenRouter; single-click stays local).

**Step 2: Write `docs/ring-checklist.md`** with the phone-side setup steps and the manual test matrix for when the ring arrives:

1. Pebble app → Index → MCP & Tool Settings → create sandbox group, model type `Default`.
2. Add MCP server: name `pebble-index`, URL `https://pebble-index.example.com/mcp`, type Streamable, Authorization `Bearer <token from Infisical>`.
3. Assign group to "Double click and hold".
4. Select the `ring_persona` prompt in the server settings.
5. Test matrix: "search my notes for cyser" → vault_search; "read Writing/ideas.md" → vault_read; "add <thought> to Writing/ideas.md" → vault_append; "ask Hermes <question>" → ask_hermes. For each: expected result, notification latency, and a pass/fail box. Note the 30s tool-list cache when testing changes.

**Step 3: Commit**

```bash
git add README.md docs/ring-checklist.md
git commit -m "docs: README and ring setup checklist"
```

---

### Task 13: Final verification pass

**Objective:** Full green before handing over.

**Step 1: Tests**

Run: `cd ~/projects/pebble-index-mcp && .venv/bin/pytest -v`
Expected: all tests pass (17 tests).

**Step 2: Services**

Run: `systemctl --user status pebble-index-mcp.service cloudflared-pebble-index.service --no-pager | grep -E "Active|pebble"`
Expected: both active (running).

**Step 3: End-to-end through the tunnel**

Repeat the initialize + tools/list + one vault_search call against `https://pebble-index.example.com/mcp` with the production token. Expected: 200s and a correct search result.

**Step 4: ask_hermes latency table**

Three live asks through the tunnel (short factual / vault-referencing / open-ended), record each latency in the plan's measured-results section.

**Step 5: Commit**

```bash
git add -A && git commit -m "docs: final verification results" # if anything changed
```

---

## Execution notes

- Secrets never enter git: env file is 0600 and gitignored (add `*.env` and `env` to `.gitignore` in Task 10 if not already covered).
- The vault is real user data: during development, point VAULT_PATH at `/tmp/ring-test-vault`; only the production env file points at the real vault.
- Gateway config edits are outside this repo; they are tracked by the config backup in Task 9.
- If `mcp` 2.x drops `streamable_http_app`, pin `mcp==1.29.0` (known-good FastMCP).
