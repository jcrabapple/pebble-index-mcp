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

import datetime
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

_DEFAULT_MUSIC_ROOMS: list[tuple[str, str]] = []
_COMPOUND_GROCERY_ITEMS = (
    "mac and cheese",
    "macaroni and cheese",
    "peanut butter and jelly",
    "salt and pepper",
    "chips and salsa",
    "chips and queso",
    "bread and butter",
    "oil and vinegar",
    "half and half",
    "biscuits and gravy",
    "beans and rice",
    "rice and beans",
    "franks and beans",
    "berries and cream",
)


def parse_music_rooms_env(raw: str) -> list[tuple[str, str]]:
    """Parse HASS_MUSIC_ROOMS ('room=entity_id,room2=entity_id2') longest-key first."""
    pairs: list[tuple[str, str]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        key, entity = entry.split("=", 1)
        norm_key = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", key.lower().replace("'", ""))).strip()
        entity = entity.strip()
        if norm_key and entity:
            pairs.append((norm_key, entity))
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    return pairs


class HAError(Exception):
    pass


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def parse_command(text: str) -> tuple[str, list[str]]:
    """Return (action, target_tokens) from a plain-text command."""
    # Apostrophes become separators so "alex's" matches the entity_id
    # tokenization ("alex", "s", "lamp") instead of a dead "alex's" token.
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()
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
    words = [w for w in re.split(r"[^a-z0-9]+", raw) if w]
    tokens = set(words)
    tokens.update(_singular(w) for w in words)
    return tokens


class HAClient:
    def __init__(
        self,
        url: str = "http://127.0.0.1:8123",
        token: str = "",
        timeout: float = _TIMEOUT,
        music_rooms: list[tuple[str, str]] | dict[str, str] | None = None,
        default_players: list[str] | str | None = None,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        if isinstance(music_rooms, dict):
            rooms = [
                (self._clean_command(k), v.strip())
                for k, v in music_rooms.items()
                if k.strip() and v.strip()
            ]
            rooms.sort(key=lambda kv: len(kv[0]), reverse=True)
            self._music_rooms = rooms
        elif music_rooms is not None:
            rooms = [(self._clean_command(k), v.strip()) for k, v in music_rooms if k.strip() and v.strip()]
            rooms.sort(key=lambda kv: len(kv[0]), reverse=True)
            self._music_rooms = rooms
        else:
            self._music_rooms = list(_DEFAULT_MUSIC_ROOMS)

        if isinstance(default_players, str):
            self._default_players = [p.strip() for p in default_players.split(",") if p.strip()]
        elif default_players:
            self._default_players = [p.strip() for p in default_players if p.strip()]
        else:
            self._default_players = []
        self._client = httpx.Client(timeout=timeout)

    # -- transport (split so tests can stub get/post independently) --------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _get_raw(self, path: str) -> tuple[int, object]:
        try:
            r = self._client.get(
                f"{self.url}{path}", headers=self._headers(), timeout=self.timeout
            )
        except httpx.TimeoutException as e:
            raise HAError(f"Home Assistant timeout on GET {path}: {e}") from e
        except httpx.TransportError as e:
            raise HAError(f"Home Assistant transport error on GET {path}: {e}") from e
        return r.status_code, _safe_json(r)

    def _post_raw(self, path: str, payload: dict) -> tuple[int, object]:
        try:
            r = self._client.post(
                f"{self.url}{path}",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise HAError(f"Home Assistant timeout on POST {path}: {e}") from e
        except httpx.TransportError as e:
            raise HAError(f"Home Assistant transport error on POST {path}: {e}") from e
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

        if domain == "cover":
            expected = {
                "on": "open",
                "open": "open",
                "off": "closed",
                "close": "closed",
            }.get(action)
            service = {
                "on": "open_cover",
                "open": "open_cover",
                "off": "close_cover",
                "close": "close_cover",
            }.get(action, "toggle")
        else:
            expected = {
                "on": "on",
                "open": "on",
                "off": "off",
                "close": "off",
            }.get(action)
            service = {
                "on": "turn_on",
                "open": "turn_on",
                "off": "turn_off",
                "close": "turn_off",
            }.get(action, "toggle")

        if expected and pre_state == expected:
            return f"{friendly} is already {pre_state}."

        self._post(f"/api/services/{domain}/{service}", {"entity_id": eid})

        # HA's state machine lags the service call, so a single immediate
        # readback reports the PRE-call state. Pause briefly before the first
        # readback so physical bridges (Hue/Zigbee) can report in.
        time.sleep(0.12)
        for i in range(5):
            updated = self._get(f"/api/states/{eid}")
            state = updated.get("state", "unknown") if isinstance(updated, dict) else "unknown"
            if state not in _BAD_STATES and state != pre_state:
                return f"{friendly} is now {state}."
            if i < 4:
                time.sleep(0.25)

        if expected and pre_state == expected:
            return f"{friendly} is {pre_state}."
        return f"{friendly} did not respond (state: {pre_state})."

    def announce(self, message: str) -> str:
        """Speak a message on every live assist_satellite (Voice PE).

        HA returns 200 for announce even when the device is offline, so the
        live-entity precheck IS the verification; there is no state readback
        for an announcement.
        """
        if not self.token:
            raise HAError("Announcements unavailable: HASS_TOKEN not configured.")
        message = message.strip()
        if not message:
            raise HAError("Empty message.")
        states = self._get("/api/states")
        live = [
            s["entity_id"]
            for s in states
            if s["entity_id"].startswith("assist_satellite.")
            and s["state"] not in _BAD_STATES
        ]
        if not live:
            raise HAError("No available speaker to announce on.")
        self._post(
            "/api/services/assist_satellite/announce",
            {"entity_id": live, "message": message, "preannounce": True},
        )
        noun = "speaker" if len(live) == 1 else "speakers"
        return f"Announced on {len(live)} {noun}."

    # -- music (Music Assistant players surfaced as media_player.*_2) --------

    _GENERIC_PLACES = {"speaker", "speakers", "here", "house", "everywhere", "the house"}
    _MUSIC_POLL_BUDGET_S = 10.0

    @classmethod
    def _clean_command(cls, command: str) -> str:
        s = command.lower().replace("'", "").replace("\u2019", "")
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    @classmethod
    def _discover_music_rooms(cls, states: list[dict]) -> list[tuple[str, str]]:
        """Build (normalized_room_key, entity_id) pairs from live HA media_players.

        Prefers Music Assistant players (`media_player.*_2`) over base cast
        entities and available players over unavailable ones.
        """
        players = [s for s in states if s.get("entity_id", "").startswith("media_player.")]
        # Sort so Music Assistant `_2` entities and available entities come first.
        players.sort(
            key=lambda s: (
                s.get("state") not in _BAD_STATES,
                s["entity_id"].endswith("_2"),
            ),
            reverse=True,
        )
        seen_keys: set[str] = set()
        discovered: list[tuple[str, str]] = []
        for s in players:
            eid = s["entity_id"]
            friendly = (s.get("attributes") or {}).get("friendly_name", "")
            candidates: list[str] = []
            if friendly:
                clean_f = cls._clean_command(friendly)
                candidates.append(clean_f)
                trimmed = re.sub(r"\s+(?:speaker|display|tv|group|media player)$", "", clean_f).strip()
                if trimmed and trimmed != clean_f:
                    candidates.append(trimmed)
            # Also derive from entity_id stem (e.g. basement_speaker_1_2 -> basement)
            stem = eid.split(".", 1)[1]
            stem = re.sub(r"(?:_1)?_2$", "", stem)
            stem = re.sub(r"_s_", "s_", stem)
            clean_s = cls._clean_command(stem.replace("_", " "))
            if clean_s:
                candidates.append(clean_s)
                trimmed_s = re.sub(r"\s+(?:speaker|display|tv|group|media player)$", "", clean_s).strip()
                if trimmed_s and trimmed_s != clean_s:
                    candidates.append(trimmed_s)
            for key in candidates:
                if key and len(key) >= 3 and key not in seen_keys:
                    seen_keys.add(key)
                    discovered.append((key, eid))
        discovered.sort(key=lambda kv: len(kv[0]), reverse=True)
        return discovered

    def _effective_music_rooms(self, states: list[dict]) -> list[tuple[str, str]]:
        """Combine explicit configured room aliases with dynamically discovered players."""
        explicit_keys = {k for k, _ in self._music_rooms}
        discovered = [
            (k, eid)
            for k, eid in self._discover_music_rooms(states)
            if k not in explicit_keys
        ]
        combined = list(self._music_rooms) + discovered
        combined.sort(key=lambda kv: len(kv[0]), reverse=True)
        return combined

    def _pick_default_player(self, states: list[dict]) -> tuple[str, str]:
        """Return (entity_id, label) for a default player when no room is named."""
        by_id = {s["entity_id"]: s for s in states if s.get("entity_id", "").startswith("media_player.")}
        for eid in self._default_players:
            st = by_id.get(eid)
            if st and st.get("state") not in _BAD_STATES:
                label = (st.get("attributes") or {}).get("friendly_name") or eid.split(".", 1)[1]
                return eid, label.lower()
        # Fall back to any live Voice PE media_player
        for s in states:
            eid = s.get("entity_id", "")
            if (
                eid.startswith("media_player.home_assistant_voice_")
                and not eid.endswith("_media_player")
                and s.get("state") not in _BAD_STATES
            ):
                return eid, "voice pe"
        # If configured defaults exist but are all offline, return the first so
        # the caller produces the clear unavailable error naming that room.
        if self._default_players:
            eid = self._default_players[0]
            st = by_id.get(eid)
            label = ((st.get("attributes") or {}).get("friendly_name") if st else None) or "default"
            return eid, label.lower()
        # Otherwise pick first live Music Assistant `_2` player if any
        for s in states:
            eid = s.get("entity_id", "")
            if eid.startswith("media_player.") and eid.endswith("_2") and s.get("state") not in _BAD_STATES:
                label = (s.get("attributes") or {}).get("friendly_name") or eid.split(".", 1)[1]
                return eid, label.lower()
        raise HAError("No available speaker found right now.")

    def _resolve_player(self, cleaned: str, states: list[dict]) -> tuple[str, str, str]:
        """Return (entity_id, room_key, media_text) for a music command."""
        rooms = self._effective_music_rooms(states)
        room_key = None
        entity = None
        media = cleaned
        m = re.search(r"\b(?:on|in|to)\s+(?:the\s+)?([a-z0-9 ]+)$", cleaned)
        if m:
            phrase = m.group(1).strip()
            for key, eid in rooms:  # longest keys first
                if key in phrase or phrase == key:
                    room_key = key
                    entity = eid
                    media = (cleaned[: m.start()] + " " + cleaned[m.end():])
                    break
            else:
                if phrase not in self._GENERIC_PLACES:
                    names = ", ".join(k for k, _ in rooms[:10])
                    raise HAError(
                        f"Unknown room '{phrase}'. Known rooms: {names}."
                    )
        if room_key is None:
            for key, eid in rooms:
                if f" {key} " in f" {cleaned} " and key != "voice pe":
                    room_key = key
                    entity = eid
                    media = cleaned.replace(key, " ")
                    break
        if entity is None:
            entity, room_key = self._pick_default_player(states)
        return entity, room_key or "speaker", media.strip()

    @staticmethod
    def _media_parts(media: str) -> tuple[str, str]:
        """Return (media_content_type, media_content_id) from the text."""
        for word, ctype in (
            ("album", "album"), ("song", "track"),
            ("track", "track"), ("playlist", "playlist"), ("artist", "artist"),
        ):
            m = re.search(rf"\b(?:the\s+)?{word}\s+(.+)$", media)
            if m:
                return ctype, m.group(1).strip()
        return "artist", media

    def music(self, command: str) -> str:
        """Play/stop Deezer music (via Music Assistant) on a room speaker."""
        if not self.token:
            raise HAError("Music control unavailable: HASS_TOKEN not configured.")
        cleaned = self._clean_command(command)
        if not cleaned:
            raise HAError("Empty command.")

        states = self._get("/api/states")
        entity, room_key, media = self._resolve_player(cleaned, states)
        state = next(
            (s for s in states if s["entity_id"] == entity), None
        )
        if state is None or state["state"] in _BAD_STATES:
            raise HAError(f"The {room_key} speaker is not available right now.")

        if re.match(r"^(?:stop|pause)\b", media):
            service = "media_pause" if media.startswith("pause") else "media_stop"
            self._post(f"/api/services/media_player/{service}", {"entity_id": entity})
            return "Paused." if service == "media_pause" else "Stopped."

        media = re.sub(r"^(?:play|put on|start|queue)\s+(?:some\s+)?", "", media).strip()
        if not media:
            raise HAError("Name an artist, album, or track to play.")
        media = re.sub(r"\s+music$", "", media).strip()
        ctype, name = self._media_parts(media)

        # "the song Jaded by Spiritbox" -> try the full phrase, then the split
        # halves, since MA's search is finicky about "by".
        queries = [name]
        if " by " in name:
            title, artist = name.split(" by ", 1)
            queries += [f"{artist} {title}", title, artist]

        attempts = [ctype] + [t for t in ("artist", "track", "album") if t != ctype]
        deadline = time.monotonic() + self._MUSIC_POLL_BUDGET_S
        for attempt in attempts:
            for query in queries:
                if time.monotonic() >= deadline:
                    break
                try:
                    self._post(
                        "/api/services/media_player/play_media",
                        {
                            "entity_id": entity,
                            "media_content_id": query,
                            "media_content_type": attempt,
                        },
                    )
                except HAError:
                    # MA returns HTTP 500 through HA when a search resolves
                    # to nothing (STT mangles names); that is a miss, not a
                    # fatal error — try the next interpretation.
                    continue
                # MA has to search Deezer and start the cast; poll briefly.
                for _ in range(6):
                    updated = self._get(f"/api/states/{entity}")
                    attrs = updated.get("attributes", {}) if isinstance(updated, dict) else {}
                    title = attrs.get("media_title")
                    if updated.get("state") == "playing" and title:
                        artist = attrs.get("media_artist") or ""
                        by = f" by {artist}" if artist else ""
                        return f"Playing {title}{by} on the {room_key}."
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.25)
        return f"I could not find '{name}' on Deezer for the {room_key}. Try naming the artist or checking the wording."

    # -- shopping list (todo.shopping_list) -----------------------------------

    _TODO_ENTITY = "todo.shopping_list"
    _LIST_TAIL_RE = re.compile(
        r"\s+(?:to|on|from|off|in)\s+(?:my\s+|the\s+)?(?:shopping|grocery|todo|to-do)?\s*list\.?$",
        re.IGNORECASE,
    )

    def _get_todo_items(self) -> list[str]:
        body = self._post(
            "/api/services/todo/get_items?return_response",
            {"entity_id": self._TODO_ENTITY, "status": ["needs_action"]},
        )
        if not isinstance(body, dict):
            return []
        svc = body.get("service_response") or body
        ent = svc.get(self._TODO_ENTITY) if isinstance(svc, dict) else None
        items = ent.get("items", []) if isinstance(ent, dict) else []
        return [
            str(it.get("summary", "")).strip()
            for it in items
            if isinstance(it, dict) and str(it.get("summary", "")).strip()
        ]

    @classmethod
    def _clean_todo_item(cls, raw: str) -> str:
        cleaned = cls._LIST_TAIL_RE.sub("", raw.strip())
        cleaned = re.sub(r"^(?:the|some|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip(" .,?!")

    @classmethod
    def _split_todo_items(cls, raw: str) -> list[str]:
        cleaned = cls._clean_todo_item(raw)
        if not cleaned:
            return []
        low = cleaned.lower()
        if any(compound in low for compound in _COMPOUND_GROCERY_ITEMS):
            parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        else:
            parts = [
                p.strip()
                for p in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", cleaned, flags=re.IGNORECASE)
                if p.strip()
            ]
        out = []
        for part in parts:
            item = re.sub(r"^(?:the|some|a|an)\s+", "", part, flags=re.IGNORECASE).strip(" .,?!")
            if item:
                out.append(item)
        return out

    def shopping_list(self, command: str) -> str:
        """Read, add to, or check off items on Home Assistant's shopping list."""
        if not self.token:
            raise HAError("Shopping list unavailable: HASS_TOKEN not configured.")
        text = command.strip()
        if not text:
            raise HAError("Empty shopping list command.")

        m_remove = re.match(
            r"^(?:please\s+)?(?:check\s+off|cross\s+off|remove|delete|complete|mark\s+off)\s+(.+)$",
            text,
            re.IGNORECASE,
        )
        if m_remove:
            target = self._clean_todo_item(m_remove.group(1))
            if not target:
                raise HAError("Name the item to check off.")
            items = self._get_todo_items()
            low = target.lower()
            match = next((it for it in items if it.lower() == low), None)
            if match is None:
                # Only match items that contain the target phrase (e.g. "oat" -> "Oat milk"),
                # never the reverse ("almond milk" must NOT delete "Milk").
                partials = [it for it in items if low in it.lower()]
                if len(partials) == 1:
                    match = partials[0]
                elif len(partials) > 1:
                    return f"Multiple matches for '{target}': {', '.join(partials[:5])}."
            if match is None:
                return f"'{target}' is not on the shopping list."
            self._post(
                "/api/services/todo/remove_item",
                {"entity_id": self._TODO_ENTITY, "item": match},
            )
            return f"Checked off {match}."

        m_add = re.match(
            r"^(?:please\s+)?(?:add|put|buy|(?:we|i)\s+need|need)\s+(.+)$",
            text,
            re.IGNORECASE,
        )
        if m_add:
            new_items = self._split_todo_items(m_add.group(1))
            if not new_items:
                raise HAError("Name an item to add to the shopping list.")
            for item in new_items:
                self._post(
                    "/api/services/todo/add_item",
                    {"entity_id": self._TODO_ENTITY, "item": item},
                )
            if len(new_items) == 1:
                return f"Added {new_items[0]} to the shopping list."
            return f"Added {', '.join(new_items)} to the shopping list."

        # Default: read current items
        items = self._get_todo_items()
        if not items:
            return "Your shopping list is empty."
        noun = "item" if len(items) == 1 else "items"
        return f"Shopping list ({len(items)} {noun}): {', '.join(items)}."

    # -- weather & sun (weather.home + sun sensors) ---------------------------

    _WEEKDAYS = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )

    @staticmethod
    def _format_local_time(iso_str: str) -> str:
        dt = datetime.datetime.fromisoformat(iso_str).astimezone()
        return dt.strftime("%-I:%M %p")

    @staticmethod
    def _format_forecast_day(
        label: str,
        entry: dict,
        temp_unit: str = "°F",
        precip_unit: str = "in",
    ) -> str:
        cond = str(entry.get("condition", "unknown")).replace("-", " ")
        high = entry.get("temperature")
        low = entry.get("templow")
        precip = entry.get("precipitation") or 0.0
        parts = [f"{label}: {cond}"]
        if high is not None and low is not None:
            parts.append(f"high {round(high)}{temp_unit}, low {round(low)}{temp_unit}")
        elif high is not None:
            parts.append(f"{round(high)}{temp_unit}")
        if precip and float(precip) > 0:
            parts.append(f"{precip} {precip_unit} rain")
        return ", ".join(parts)

    def weather(self, query: str = "") -> str:
        """Return local weather conditions, forecast, or sunrise/sunset from HA."""
        if not self.token:
            raise HAError("Weather unavailable: HASS_TOKEN not configured.")
        q = query.lower().strip()

        if re.search(r"\b(sunrise|sunset|dawn|dusk)\b", q):
            rising = self._get("/api/states/sensor.sun_next_rising")
            setting = self._get("/api/states/sensor.sun_next_setting")
            r_str = self._format_local_time(rising["state"]) if isinstance(rising, dict) and rising.get("state") not in _BAD_STATES else "unknown"
            s_str = self._format_local_time(setting["state"]) if isinstance(setting, dict) and setting.get("state") not in _BAD_STATES else "unknown"
            return f"Next sunrise is {r_str}; next sunset is {s_str}."

        current = self._get("/api/states/weather.home")
        if not isinstance(current, dict) or current.get("state") in _BAD_STATES:
            raise HAError("Home weather entity is unavailable.")
        cond = str(current.get("state", "unknown")).replace("-", " ")
        attrs = current.get("attributes") or {}
        temp = attrs.get("temperature")
        unit = attrs.get("temperature_unit", "°F")
        precip_unit = attrs.get("precipitation_unit", "in")
        wind = attrs.get("wind_speed")
        wind_unit = attrs.get("wind_speed_unit", "mph")

        cur_bits = [f"Currently {round(temp)}{unit} and {cond}" if temp is not None else f"Currently {cond}"]
        if wind is not None and float(wind) >= 10:
            cur_bits.append(f"wind {round(float(wind))} {wind_unit}")
        cur_sentence = ", ".join(cur_bits) + "."

        try:
            fc_resp = self._post(
                "/api/services/weather/get_forecasts?return_response",
                {"entity_id": "weather.home", "type": "daily"},
            )
            svc = (fc_resp.get("service_response") or fc_resp) if isinstance(fc_resp, dict) else {}
            forecast = (svc.get("weather.home") or {}).get("forecast") or []
        except HAError:
            forecast = []

        if not forecast:
            return cur_sentence

        # Check if user asked about a specific weekday or tomorrow
        for idx, entry in enumerate(forecast):
            dt_raw = entry.get("datetime")
            if not dt_raw:
                continue
            try:
                day_name = datetime.datetime.fromisoformat(dt_raw).strftime("%A").lower()
            except ValueError:
                continue
            if day_name in q and idx > 0:
                return f"{cur_sentence} {self._format_forecast_day(day_name.capitalize(), entry, unit, precip_unit)}."

        if "tomorrow" in q and len(forecast) > 1:
            return f"{cur_sentence} {self._format_forecast_day('Tomorrow', forecast[1], unit, precip_unit)}."

        segments = [cur_sentence, self._format_forecast_day("Today", forecast[0], unit, precip_unit) + "."]
        if len(forecast) > 1:
            segments.append(self._format_forecast_day("Tomorrow", forecast[1], unit, precip_unit) + ".")
        return " ".join(segments)


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:200]
