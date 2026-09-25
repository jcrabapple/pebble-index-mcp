"""Exa web search client for web_search."""
from __future__ import annotations

import httpx

_SNIPPET_CHARS = 300
_TIMEOUT = 15.0


class ExaError(Exception):
    pass


class ExaClient:
    def __init__(self, api_key: str, url: str = "https://api.exa.ai/search"):
        self.api_key = api_key
        self.url = url

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
            r = httpx.post(
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
