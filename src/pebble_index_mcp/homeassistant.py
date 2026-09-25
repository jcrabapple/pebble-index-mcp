"""Home Assistant REST client for ha_control (smart-home from the ring).

Design constraints shaped by the live registry:
- Room lights have a group entity `light.<room>_<room>` (friendly name is
  usually just the room name) plus per-fixture `light.<room>_<room>_lamp_N`
  entities, so "living room lights" must prefer the group.
- Duplicate friendly names exist ("Bedroom" twice, one stale/unavailable),
  so available entities beat unavailable ones before group preference.
- "storage closet" contains "close"; action words must be matched on word
  boundaries and picked from the END of the command.
No LLM in the loop: parse -> resolve -> service call -> verify readback,
all deterministic, so the ring round-trip stays around a second.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

_ALLOWED_DOMAINS = ("light", "switch", "fan", "cover", "media_player")
_BAD_STATES = {"unavailable", "unknown"}
_TIMEOUT = 10.0

# Words stripped from the target phrase before matching. Includes the
# action words themselves (an earlier occurrence like "on the nightstand"
# is filler; the LAST action word is the command).
_FILLER = frozenset(
    """turn switch please the a an my to in at now can you could would hey set
    make get shut kill is are do does what s it them on off open close
    toggle up down""".split()
)

_ACTION_RE = re.compile(r"\b(on|off|toggle|open|close)\b")
_STATUS_RE = re.compile(r"\b(status|state)\b")


class HAError(Exception):
    pass


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def parse_command(text: str) -> tuple[str, list[str]]:
    """Return (action, target_tokens) from a plain-text command."""
    t = re.sub(r"[^a-z0-9' ]+", " ", text.lower()).strip()
    if not t:
        raise HAError("Empty command.")
    if _STATUS_RE.search(t) or re.match(r"^(is|are|what)\b", t):
        return "status", _target_tokens(t)
    matches = list(_ACTION_RE.finditer(t))
    if not matches:
        raise HAError(
            'No action found; end the command with on, off, or toggle '
            '(e.g. "living room lights on").'
        )
    last = matches[-1]
    action = last.group(1)
    # Remove the action occurrence, keep the rest as the target phrase.
    stripped = (t[: last.start()] + " " + t[last.end():])
    return action, _target_tokens(stripped)


def _target_tokens(phrase: str) -> list[str]:
    return [_singular(w) for w in phrase.split() if w not in _FILLER]


def _text_tokens(entity_id: str, friendly: str) -> set[str]:
    raw = f"{entity_id.replace('_', ' ').replace('.', ' ')} {friendly.lower()}"
    return {w for w in re.split(r"[^a-z0-9']+", raw) if w}


class HAClient:
    def __init__(
        self,
        url: str = "http://127.0.0.1:8123",
        token: str = "",
        timeout: float = _TIMEOUT,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- transport (split so tests can stub get/post independently) --------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _get_raw(self, path: str) -> tuple[int, object]:
        r = httpx.get(
            f"{self.url}{path}", headers=self._headers(), timeout=self.timeout
        )
        return r.status_code, _safe_json(r)

    def _post_raw(self, path: str, payload: dict) -> tuple[int, object]:
        r = httpx.post(
            f"{self.url}{path}",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        return r.status_code, _safe_json(r)

    def _get(self, path: str) -> Any:
        code, body = self._get_raw(path)
        if code != 200:
            raise HAError(f"Home Assistant HTTP {code} on GET {path}")
        return body

    def _post(self, path: str, payload: dict) -> Any:
        code, body = self._post_raw(path, payload)
        if code not in (200, 201):
            raise HAError(f"Home Assistant HTTP {code} on POST {path}: {str(body)[:120]}")
        return body

    # -- resolution (pure; tests pass states directly) ----------------------

    def resolve(self, states: list[dict], tokens: list[str]) -> dict:
        """Pick the best entity for the target tokens.

        Ranking: full token matches first, then available over
        unavailable/unknown, then group entities over their members, then
        raw match count. Ties among distinct entities at the top are
        ambiguous and raise with the candidates listed.
        """
        tokens = [t for t in tokens if t]
        if not tokens:
            raise HAError("No device named in the command.")
        cands = [
            s for s in states if s["entity_id"].split(".", 1)[0] in _ALLOWED_DOMAINS
        ]
        if not cands:
            raise HAError("No controllable devices found in Home Assistant.")

        ids = {s["entity_id"] for s in cands}

        def is_group(eid: str) -> bool:
            return any(o != eid and o.startswith(eid + "_") for o in ids)

        scored = []
        for s in cands:
            text = _text_tokens(
                s["entity_id"], s["attributes"].get("friendly_name", "")
            )
            match = sum(1 for t in tokens if t in text)
            scored.append(
                {
                    "entity": s,
                    "match": match,
                    "full": tokens and match == len(tokens),
                    "available": s["state"] not in _BAD_STATES,
                    "group": is_group(s["entity_id"]),
                }
            )

        full = [c for c in scored if c["full"] and c["match"] > 0]
        if not full:
            partials = sorted(
                (c for c in scored if c["match"] > 0),
                key=lambda c: c["match"],
                reverse=True,
            )
            names = ", ".join(
                c["entity"]["attributes"].get("friendly_name", c["entity"]["entity_id"])
                for c in partials[:3]
            )
            hint = f" Closest: {names}." if names else ""
            raise HAError(
                f"No device matches '{' '.join(tokens)}'.{hint}"
            )

        full.sort(
            key=lambda c: (c["available"], c["group"], c["match"]),
            reverse=True,
        )
        best = full[0]
        tied = [
            c
            for c in full
            if (c["available"], c["group"], c["match"])
            == (best["available"], best["group"], best["match"])
        ]
        if len(tied) > 1:
            names = ", ".join(
                c["entity"]["attributes"].get("friendly_name", "?") for c in tied[:5]
            )
            raise HAError(f"Which one? Candidates: {names}.")
        return best["entity"]

    # -- high level ----------------------------------------------------------

    def control(self, command: str) -> str:
        """Run one smart-home command and return a short spoken result."""
        if not self.token:
            raise HAError("Smart home control unavailable: HASS_TOKEN not configured.")
        states = self._get("/api/states")
        action, tokens = parse_command(command)
        entity = self.resolve(states, tokens)
        eid = entity["entity_id"]
        domain = eid.split(".", 1)[0]
        friendly = entity["attributes"].get("friendly_name", eid)
        pre_state = entity["state"]

        if action == "status":
            return f"{friendly}: {pre_state}."

        expected = {"on": "on", "off": "off"}.get(action)
        if domain == "cover" and expected:
            expected = {"on": "open", "off": "closed"}[expected]

        if expected and pre_state == expected:
            return f"{friendly} is already {pre_state}."

        if domain == "cover":
            service = {"on": "open_cover", "off": "close_cover"}.get(action, "toggle")
        else:
            service = {"on": "turn_on", "off": "turn_off"}.get(action, "toggle")
        self._post(f"/api/services/{domain}/{service}", {"entity_id": eid})

        # HA's state machine lags the service call, so a single immediate
        # readback reports the PRE-call state. Poll briefly for the change.
        for _ in range(5):
            updated = self._get(f"/api/states/{eid}")
            state = updated.get("state", "unknown") if isinstance(updated, dict) else "unknown"
            if state not in _BAD_STATES and state != pre_state:
                return f"{friendly} is now {state}."
            time.sleep(0.3)

        if expected and pre_state == expected:
            return f"{friendly} is {pre_state}."
        return f"{friendly} did not respond (state: {pre_state})."


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:200]
