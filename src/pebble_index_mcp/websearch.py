"""Web search clients for web_search (Trivet metasearch primary + Exa fallback)."""
from __future__ import annotations

import base64
import re

import httpx

_SNIPPET_CHARS = 300
_TRIVET_SNIPPET_CHARS = 400
_TIMEOUT = 15.0
_NEWS_QUERY_RE = re.compile(r"\b(latest\s+news|news\s+about|headlines|breaking\s+news)\b", re.IGNORECASE)


class ExaError(Exception):
    pass


class TrivetError(ExaError):
    pass


class TrivetClient:
    """Self-hosted Trivet metasearch client (GET /api/search with optional Basic auth)."""

    def __init__(
        self,
        url: str = "",
        username: str = "",
        password: str = "",
        timeout: float = _TIMEOUT,
    ):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        if self.username and self.password:
            tok = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {tok}"}
        return {}

    def search(self, query: str, num_results: int = 3) -> str:
        """Query Trivet /api/search and return title/url/snippet lines."""
        if not self.url:
            raise TrivetError("TRIVET_URL not configured")
        params: dict[str, str | int] = {"q": query, "limit": max(1, min(int(num_results), 10))}
        if _NEWS_QUERY_RE.search(query):
            params["tab"] = "news"
        try:
            r = self._client.get(
                url=f"{self.url}/api/search",
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise TrivetError(f"timeout: {e}") from e
        except httpx.TransportError as e:
            raise TrivetError(str(e)) from e
        if r.status_code != 200:
            raise TrivetError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            payload = r.json()
            results = payload["results"]
        except (ValueError, KeyError, TypeError) as e:
            raise TrivetError(f"Unexpected response shape: {type(e).__name__}") from e
        if not results:
            return "No results found."
        lines = []
        for item in results[:num_results]:
            title = (item.get("title") or "(untitled)").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("snippet") or "").strip().replace("\n", " ")[:_TRIVET_SNIPPET_CHARS]
            lines.append(f"{title} ({url})\n{snippet}")
        return "\n\n".join(lines)


class ExaClient:
    def __init__(self, api_key: str, url: str = "https://api.exa.ai/search"):
        self.api_key = api_key
        self.url = url
        self._client = httpx.Client(timeout=_TIMEOUT)

    def search(self, query: str, num_results: int = 3) -> str:
        """Query the Exa search API and return title/url/snippet lines."""
        if not self.api_key:
            return "Web search unavailable: EXA_API_KEY not configured"
        payload = {
            "query": query,
            "numResults": num_results,
            "contents": {"text": {"maxCharacters": _SNIPPET_CHARS}},
        }
        try:
            r = self._client.post(
                url=self.url,
                json=payload,
                headers={"x-api-key": self.api_key},
                timeout=_TIMEOUT,
            )
        except httpx.TimeoutException as e:
            raise ExaError(f"timeout: {e}") from e
        except httpx.TransportError as e:
            raise ExaError(str(e)) from e
        if r.status_code != 200:
            raise ExaError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            results = r.json()["results"]
        except (ValueError, KeyError, TypeError) as e:
            raise ExaError(f"Unexpected response shape: {type(e).__name__}") from e
        if not results:
            return "No results found."
        lines = []
        for item in results:
            title = (item.get("title") or "(untitled)").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("text") or "").strip().replace("\n", " ")[:_SNIPPET_CHARS]
            lines.append(f"{title} ({url})\n{snippet}")
        return "\n\n".join(lines)
