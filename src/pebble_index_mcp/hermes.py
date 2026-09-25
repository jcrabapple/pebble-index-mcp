"""Hermes API client for ask_hermes."""
from __future__ import annotations

import httpx

ANSWER_HINT = (
    "Answer in at most 3 sentences. The answer will be shown as a phone "
    "notification."
)

COMPOSE_HINT = (
    "Answer the question using only the search results provided. Answer in "
    "at most 3 short sentences, plain text, no markdown or URLs. The answer "
    "is spoken aloud and shown as a phone notification. If the results do "
    "not contain the answer, say so briefly."
)


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
        """Send a question to the Hermes API server and return its answer."""
        return self._chat(
            system=ANSWER_HINT,
            user=question,
        )

    def compose_answer(self, question: str, search_results: str) -> str:
        """Compose a short spoken answer for a question from search results."""
        return self._chat(
            system=COMPOSE_HINT,
            user=f"Question: {question}\n\nSearch results:\n{search_results}",
        )

    def _chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            r = httpx.post(
                url=self.url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise HermesTimeout(str(e)) from e
        except httpx.TransportError as e:
            raise HermesError(str(e)) from e
        if r.status_code != 200:
            raise HermesError(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise HermesError(f"Unexpected response shape: {type(e).__name__}") from e
