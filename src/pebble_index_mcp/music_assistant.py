"""Direct Music Assistant JSON-RPC client for ha_music.

Converges the voice profile's Music Assistant engine (smart search ranking,
karaoke/instrumental filtering, library preference, clean artist top-tracks,
queue/shuffle/repeat/favorite/transfer, TV exclusion) into pebble-index-mcp.
Uses shared HomeConfig (rooms, aliases, default_players, ASR corrections).
"""
from __future__ import annotations

import random
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .home_config import HomeConfig, HomeConfigLoader, norm_key

_TIMEOUT = 15.0
_LIBRARY_CACHE_TTL_S = 21600.0  # 6 hours

_SEARCH_TYPES = ("track", "album", "artist", "playlist", "radio")
_RESULT_KEYS = {
    "track": "tracks",
    "album": "albums",
    "artist": "artists",
    "playlist": "playlists",
    "radio": "radio",
}
_GENERIC_PLACES = {"speaker", "speakers", "here", "house", "the house"}
_EVERYWHERE_WORDS = {"everywhere", "all", "all speakers", "all rooms", "whole house"}

_UNWANTED_EDITION_TERMS = (
    "instrumental",
    "karaoke",
    "lullaby",
    "8-bit",
    "8 bit",
    "music box",
    "for cello",
    "piano session",
    "piano version",
    "tribute to",
    "made famous by",
    "originally performed by",
)


class MAError(Exception):
    pass


def _norm(text: str) -> str:
    s = (text or "").lower().replace("\u2019", "'")
    s = re.sub(r"[^\w\s']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _wants_special_edition(query: str) -> bool:
    q = query.lower()
    return any(term in q for term in _UNWANTED_EDITION_TERMS)


def _has_unwanted_edition(item: Mapping[str, Any]) -> bool:
    parts = [
        str(item.get("name") or ""),
        str(item.get("version") or ""),
        str((item.get("album") or {}).get("name") or "") if isinstance(item.get("album"), Mapping) else "",
    ]
    artists = item.get("artists") or []
    parts.extend(str(a.get("name") or "") for a in artists if isinstance(a, Mapping))
    blob = " ".join(parts).lower()
    return any(term in blob for term in _UNWANTED_EDITION_TERMS)


def _item_artists(item: Mapping[str, Any]) -> list[str]:
    out = []
    for a in item.get("artists") or []:
        if isinstance(a, Mapping) and a.get("name"):
            out.append(str(a["name"]))
        elif isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def _describe_current_item(current: Mapping[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not current:
        return None, None, None
    mi = current.get("media_item")
    if isinstance(mi, Mapping):
        title = str(mi.get("name") or current.get("name") or "").strip() or None
        ver = str(mi.get("version") or "").strip()
        if title and ver and ver.lower() not in title.lower():
            title = f"{title} ({ver})"
        artists = _item_artists(mi)
        artist = ", ".join(artists) if artists else None
        uri = str(mi.get("uri") or "") or None
        return title, artist, uri
    raw_name = str(current.get("name") or "").strip()
    if " - " in raw_name:
        artist, title = raw_name.split(" - ", 1)
        return title.strip(), artist.strip(), None
    return (raw_name or None), None, None


def _split_by_artist(query: str) -> tuple[str, str | None]:
    m = re.match(r"^(.+?)\s+by\s+(.+)$", query.strip(), flags=re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return query.strip(), None


def score_media_item(
    item: Mapping[str, Any],
    kind: str,
    query: str,
    title_hint: str,
    artist_hint: str | None,
    allow_special: bool,
    library_artists: set[str] | None = None,
    playlist_owners: frozenset[str] = frozenset({"music assistant"}),
) -> float:
    name_n = _norm(str(item.get("name") or ""))
    query_n = _norm(query)
    title_n = _norm(title_hint)
    artist_n = _norm(artist_hint) if artist_hint else ""
    artists_n = [_norm(a) for a in _item_artists(item)]
    uri = str(item.get("uri") or "")

    score = 0.0
    target_title = title_n if artist_n else query_n

    if name_n == target_title:
        score += 60.0
    elif target_title and name_n.startswith(target_title):
        score += 35.0
    elif target_title and target_title in name_n:
        score += 20.0

    if artist_n:
        if any(a == artist_n for a in artists_n):
            score += 55.0
        elif any(artist_n in a or a in artist_n for a in artists_n if a):
            score += 30.0
        elif kind != "artist":
            score -= 25.0
    elif kind in ("track", "album") and artists_n:
        for a in artists_n:
            if a and a in query_n and a != query_n:
                score += 25.0
                rem = _norm(query_n.replace(a, " "))
                if rem and name_n == rem:
                    score += 40.0

    if uri.startswith("library://"):
        score += 12.0
    if item.get("favorite"):
        score += 5.0
    if library_artists and not artist_n and kind in ("track", "album"):
        if any(a in library_artists for a in artists_n):
            score += 18.0

    meta = item.get("metadata")
    if isinstance(meta, Mapping):
        pop = meta.get("popularity")
        if isinstance(pop, (int, float)) and pop > 0:
            score += min(float(pop) / 100000.0, 10.0)

    if kind == "playlist":
        owner = norm_key(str(item.get("owner") or ""))
        if owner in playlist_owners:
            score += 15.0

    if not allow_special and _has_unwanted_edition(item):
        score -= 80.0

    return score


def pick_best_media(
    search_res: Mapping[str, Any],
    kind: str,
    query: str,
    library_artists: set[str] | None = None,
    playlist_owners: frozenset[str] = frozenset({"music assistant"}),
) -> tuple[str, dict[str, Any]]:
    title_hint, artist_hint = _split_by_artist(query)
    allow_special = _wants_special_edition(query)

    if kind != "auto":
        bucket = list(search_res.get(_RESULT_KEYS[kind]) or [])
        if not bucket:
            raise MAError(f"No {kind} found for '{query}'.")
        ranked = sorted(
            enumerate(bucket),
            key=lambda iv: (
                score_media_item(iv[1], kind, query, title_hint, artist_hint, allow_special, library_artists, playlist_owners),
                -iv[0],
            ),
            reverse=True,
        )
        return kind, ranked[0][1]

    query_n = _norm(query)
    candidates: list[tuple[float, int, str, dict[str, Any]]] = []

    if not artist_hint:
        for idx, artist in enumerate(search_res.get("artists") or []):
            aname = _norm(str(artist.get("name") or ""))
            s = score_media_item(artist, "artist", query, title_hint, None, allow_special, None, playlist_owners)
            if aname == query_n:
                s += 35.0
            candidates.append((s, -idx, "artist", artist))

        for idx, pl in enumerate(search_res.get("playlists") or []):
            pname = _norm(str(pl.get("name") or ""))
            s = score_media_item(pl, "playlist", query, title_hint, None, allow_special, None, playlist_owners)
            if "playlist" in query_n or (str(pl.get("uri") or "").startswith("library://") and pname == query_n):
                s += 40.0
            elif pname != query_n:
                s -= 15.0
            candidates.append((s, -idx, "playlist", pl))

    for k in ("track", "album"):
        for idx, it in enumerate(search_res.get(_RESULT_KEYS[k]) or []):
            s = score_media_item(it, k, query, title_hint, artist_hint, allow_special, library_artists, playlist_owners)
            if k == "track":
                s += 4.0
            candidates.append((s, -idx, k, it))

    if not candidates:
        raise MAError(f"Could not find '{query}' in Music Assistant.")

    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    _, _, chosen_kind, chosen_item = candidates[0]
    return chosen_kind, chosen_item


class MusicAssistantClient:
    """Direct Music Assistant HTTP JSON-RPC client with voice-grade command routing."""

    def __init__(
        self,
        url: str = "",
        token: str = "",
        home_config: HomeConfigLoader | HomeConfig | None = None,
        timeout: float = _TIMEOUT,
    ):
        clean_url = url.strip().rstrip("/")
        if clean_url.endswith("/api"):
            clean_url = clean_url[:-4]
        self.url = clean_url
        self.token = token.strip()
        self.timeout = timeout
        self._home_config = home_config if home_config is not None else HomeConfigLoader()
        self._client = httpx.Client(timeout=timeout)
        self._lib_artists_cache: tuple[float, set[str]] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _cfg(self) -> HomeConfig:
        if isinstance(self._home_config, HomeConfig):
            return self._home_config
        return self._home_config.get()

    def _post_raw(self, payload: dict[str, Any]) -> tuple[int, Any]:
        try:
            r = self._client.post(
                f"{self.url}/api",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise MAError(f"Music Assistant timeout: {e}") from e
        except httpx.TransportError as e:
            raise MAError(f"Music Assistant unreachable: {e}") from e
        try:
            body = r.json() if r.text.strip() else None
        except ValueError:
            body = r.text[:200]
        return r.status_code, body

    def call(self, command: str, **args: Any) -> Any:
        if not self.configured:
            raise MAError("Music Assistant unavailable: MASS_SERVER_URL or MASS_ACCESS_TOKEN not configured.")
        clean_args = {k: v for k, v in args.items() if v is not None}
        code, body = self._post_raw({"command": command, "args": clean_args})
        if code in (401, 403):
            raise MAError("Music Assistant authentication rejected.")
        if code != 200:
            detail = str(body)[:180] if body else f"HTTP {code}"
            raise MAError(f"Music Assistant error on {command}: {detail}")
        return body

    def _library_artists(self) -> set[str]:
        now = time.monotonic()
        if self._lib_artists_cache and now - self._lib_artists_cache[0] < _LIBRARY_CACHE_TTL_S:
            return self._lib_artists_cache[1]
        try:
            raw = self.call("music/artists/library_items", limit=500) or []
            items = raw.get("items", raw) if isinstance(raw, Mapping) else raw
            names = {_norm(str(a.get("name") or "")) for a in items if isinstance(a, Mapping) and a.get("name")}
            names.discard("")
            self._lib_artists_cache = (now, names)
            return names
        except MAError:
            return set()

    # -- room / player resolution --------------------------------------------

    def _room_maps(self, players: list[dict[str, Any]], cfg: HomeConfig) -> list[tuple[str, str, str]]:
        """Return (norm_key, ma_player_id, spoken_label) sorted longest key first."""
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for key, rname in cfg.all_room_keys_longest_first():
            spec = cfg.rooms.get(rname)
            if spec and spec.ma_player_id and key not in seen:
                seen.add(key)
                out.append((key, spec.ma_player_id, spec.spoken or f"in the {rname}"))
        # Supplement with live MA players by display_name / name
        for p in sorted(players, key=lambda x: bool(x.get("available")), reverse=True):
            pid = str(p.get("player_id") or "")
            name = str(p.get("display_name") or p.get("name") or "")
            if not pid or not name:
                continue
            for cand in (
                norm_key(name),
                re.sub(r"\s+(?:speaker|display|tv|group)$", "", norm_key(name)).strip(),
            ):
                if cand and len(cand) >= 3 and cand not in seen:
                    seen.add(cand)
                    out.append((cand, pid, f"on {name}"))
        out.sort(key=lambda row: len(row[0]), reverse=True)
        return out

    def _spoken_where(self, pid: str, players_by_id: dict[str, dict], cfg: HomeConfig) -> str:
        spec = cfg.room_by_ma_id(pid)
        if spec and spec.spoken:
            return spec.spoken
        p = players_by_id.get(pid) or {}
        name = p.get("display_name") or p.get("name") or pid
        return f"on {name}"

    def _pick_default_player(
        self,
        players_by_id: dict[str, dict],
        cfg: HomeConfig,
    ) -> tuple[str, str]:
        for name_or_id in cfg.default_players:
            rname = cfg.resolve_room_name(name_or_id)
            pid = cfg.rooms[rname].ma_player_id if rname else name_or_id
            p = players_by_id.get(pid)
            if p and p.get("available"):
                return pid, self._spoken_where(pid, players_by_id, cfg)
        # First available configured room speaker
        for spec in cfg.rooms.values():
            if spec.ma_player_id:
                p = players_by_id.get(spec.ma_player_id)
                if p and p.get("available"):
                    return spec.ma_player_id, spec.spoken or f"in the {spec.name}"
        # First available non-TV player
        for pid, p in players_by_id.items():
            name = str(p.get("display_name") or p.get("name") or "").lower()
            if p.get("available") and "tv" not in name:
                return pid, self._spoken_where(pid, players_by_id, cfg)
        raise MAError("No available Music Assistant speaker right now.")

    def _pick_active_player(
        self,
        queues_by_id: dict[str, dict],
        players_by_id: dict[str, dict],
        cfg: HomeConfig,
        *,
        accept_paused: bool = False,
    ) -> tuple[str, str] | None:
        music_ids = cfg.music_speaker_ma_ids()
        candidate_ids = list(music_ids) if music_ids else [
            pid for pid, p in players_by_id.items()
            if "tv" not in str(p.get("display_name") or p.get("name") or "").lower()
        ]
        for state in (("playing", "paused") if accept_paused else ("playing",)):
            for pid in candidate_ids:
                q = queues_by_id.get(pid) or {}
                if q.get("state") == state:
                    return pid, self._spoken_where(pid, players_by_id, cfg)
        return None

    def _extract_room(
        self,
        cleaned: str,
        room_maps: list[tuple[str, str, str]],
        players_by_id: dict[str, dict],
        queues_by_id: dict[str, dict],
        cfg: HomeConfig,
        *,
        prefer_active: bool = False,
        accept_paused: bool = False,
    ) -> tuple[str, str, str, bool]:
        """Return (player_id, spoken_where, remainder_text, explicit_room)."""
        pid: str | None = None
        where = ""
        explicit = False
        remainder = cleaned

        prep_matches = list(re.finditer(r"\b(?:on|in|to)\s+(?:the\s+)?", cleaned))
        if prep_matches:
            matched = False
            for pm in reversed(prep_matches):
                phrase = cleaned[pm.end():].strip()
                if phrase in _EVERYWHERE_WORDS:
                    return "__all__", "everywhere", cleaned[: pm.start()].strip(), True
                for key, cand_pid, cand_where in room_maps:
                    if key == phrase or key in phrase:
                        pid = cand_pid
                        where = cand_where
                        explicit = True
                        remainder = cleaned[: pm.start()]
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                tail = cleaned[prep_matches[-1].end():].strip()
                if tail not in _GENERIC_PLACES and not re.match(r"^\d+(?:\s*percent)?$", tail):
                    known = ", ".join(r for r in list(cfg.rooms.keys())[:8]) or ", ".join(k for k, _, _ in room_maps[:8])
                    raise MAError(f"Unknown room '{tail}'. Known rooms: {known}.")

        if pid is None:
            for ew in sorted(_EVERYWHERE_WORDS, key=len, reverse=True):
                if ew != "all" and re.search(rf"\b{re.escape(ew)}\b", cleaned):
                    rem = re.sub(rf"\b{re.escape(ew)}\b", " ", cleaned)
                    return "__all__", "everywhere", re.sub(r"\s+", " ", rem).strip(), True
            for key, cand_pid, cand_where in room_maps:
                if key in ("voice", "voice pe", "here", "local"):
                    continue
                if f" {key} " in f" {cleaned} ":
                    pid = cand_pid
                    where = cand_where
                    explicit = True
                    remainder = cleaned.replace(key, " ")
                    break

        if pid is None and prefer_active:
            active = self._pick_active_player(queues_by_id, players_by_id, cfg, accept_paused=accept_paused)
            if active:
                pid, where = active

        if pid is None:
            pid, where = self._pick_default_player(players_by_id, cfg)

        return pid, where, re.sub(r"\s+", " ", remainder).strip(), explicit

    @staticmethod
    def _parse_media_kind(text: str) -> tuple[str, str]:
        for word, kind in (
            ("album", "album"),
            ("song", "track"),
            ("track", "track"),
            ("playlist", "playlist"),
            ("artist", "artist"),
        ):
            m = re.search(rf"\b(?:the\s+)?{word}\s+(.+)$", text)
            if m:
                return kind, m.group(1).strip()
        return "auto", text

    # -- main entrypoint -----------------------------------------------------

    def command(self, raw_command: str) -> str:
        """Execute one natural-language music command via Music Assistant JSON-RPC."""
        if not self.configured:
            raise MAError("Music Assistant unavailable: MASS_SERVER_URL or MASS_ACCESS_TOKEN not configured.")
        cfg = self._cfg()
        corrected = cfg.apply_asr_corrections(raw_command.strip())
        cleaned = norm_key(corrected)
        if not cleaned:
            raise MAError("Empty music command.")

        players = self.call("players/all") or []
        queues = self.call("player_queues/all") or []
        players_by_id: dict[str, dict[str, Any]] = {
            str(p["player_id"]): dict(p) for p in players if isinstance(p, Mapping) and p.get("player_id")
        }
        queues_by_id: dict[str, dict[str, Any]] = {
            str(q["queue_id"]): dict(q) for q in queues if isinstance(q, Mapping) and q.get("queue_id")
        }
        room_maps = self._room_maps(players, cfg)

        # 1. Transfer queue: "transfer music from basement to kitchen" / "move music to kitchen"
        m_xfer = re.search(
            r"\b(?:transfer|move)\b(?:.*?\bfrom\s+(?:the\s+)?([a-z0-9 ]+?)|.*?)\s+to\s+(?:the\s+)?([a-z0-9 ]+)$",
            cleaned,
        )
        if m_xfer:
            src_phrase = (m_xfer.group(1) or "").strip()
            dst_phrase = m_xfer.group(2).strip()
            dst_pid = next((pid for k, pid, _ in room_maps if k == dst_phrase or k in dst_phrase), None)
            if not dst_pid:
                raise MAError(f"Unknown destination room '{dst_phrase}'.")
            if src_phrase:
                src_pid = next((pid for k, pid, _ in room_maps if k == src_phrase or k in src_phrase), None)
                if not src_pid:
                    raise MAError(f"Unknown source room '{src_phrase}'.")
            else:
                active = self._pick_active_player(queues_by_id, players_by_id, cfg, accept_paused=True)
                if not active:
                    raise MAError("Nothing is playing to transfer.")
                src_pid = active[0]
            if src_pid == dst_pid:
                return "Music is already on that speaker."
            src_q = queues_by_id.get(src_pid) or {}
            if not src_q.get("items") and not src_q.get("current_item"):
                raise MAError("Source queue is empty; nothing to transfer.")
            self.call("player_queues/transfer", source_queue_id=src_pid, target_queue_id=dst_pid)
            return f"Transferred music {self._spoken_where(dst_pid, players_by_id, cfg)}."

        # 2. Now playing / status
        if re.search(
            r"\b(?:whats|what is)\s+(?:currently\s+)?playing\b|\bnow\s+playing\b|\bwhat\s+(?:song|track)\s+is\s+(?:this|playing)\b",
            cleaned,
        ):
            pid, where, _, explicit = self._extract_room(
                cleaned, room_maps, players_by_id, queues_by_id, cfg, prefer_active=True, accept_paused=True
            )
            if not explicit:
                active = self._pick_active_player(queues_by_id, players_by_id, cfg, accept_paused=False)
                if not active:
                    paused = self._pick_active_player(queues_by_id, players_by_id, cfg, accept_paused=True)
                    if paused:
                        q = queues_by_id.get(paused[0]) or {}
                        t, a, _ = _describe_current_item(q.get("current_item"))
                        if t:
                            by = f" by {a}" if a else ""
                            return f"Nothing is playing right now ({t}{by} is paused {paused[1]})."
                    return "Nothing is playing right now."
                pid, where = active
            q = queues_by_id.get(pid) or {}
            p = players_by_id.get(pid) or {}
            t, a, _ = _describe_current_item(q.get("current_item"))
            st = q.get("state") or p.get("playback_state") or "idle"
            if st == "playing" and t:
                by = f" by {a}" if a else ""
                return f"Playing {t}{by} {where}."
            if st == "paused" and t:
                by = f" by {a}" if a else ""
                return f"Paused on {t}{by} {where}."
            return f"Nothing is playing {where}."

        # 3. Up next / queue listing
        if re.search(r"\b(?:(?:whats|what\s+is)\s+(?:up\s+)?next|show\s+(?:the\s+)?queue|what\s+is\s+in\s+the\s+queue)\b", cleaned):
            pid, where, _, _ = self._extract_room(
                cleaned, room_maps, players_by_id, queues_by_id, cfg, prefer_active=True, accept_paused=True
            )
            q = queues_by_id.get(pid) or {}
            cur_idx = int(q.get("current_index") or 0)
            items = self.call("player_queues/items", queue_id=pid, offset=cur_idx, limit=5) or []
            if not items:
                return f"Queue is empty {where}."
            names = []
            for it in items[:4]:
                t, a, _ = _describe_current_item(it)
                if t:
                    names.append(f"{t} by {a}" if a else t)
            return f"Up next {where}: " + "; ".join(names) + "."

        # 4. Favorite current track
        if re.search(r"\b(?:favorite|like|heart)\s+(?:this\s+)?(?:song|track|music)?\b|^favorite$", cleaned):
            pid, where, _, _ = self._extract_room(
                cleaned, room_maps, players_by_id, queues_by_id, cfg, prefer_active=True, accept_paused=True
            )
            q = queues_by_id.get(pid) or {}
            t, a, uri = _describe_current_item(q.get("current_item"))
            if not uri:
                raise MAError("Nothing is playing to favorite.")
            self.call("music/favorites/add_item", item=uri)
            by = f" by {a}" if a else ""
            return f"Favorited {t}{by}."

        # 5. Shuffle / Repeat toggles (when not a "shuffle <artist>" play command)
        m_shuf = re.match(r"^shuffle\s+(on|off)\b", cleaned)
        if m_shuf:
            enabled = m_shuf.group(1) == "on"
            pid, where, _, _ = self._extract_room(
                cleaned, room_maps, players_by_id, queues_by_id, cfg, prefer_active=True, accept_paused=True
            )
            self.call("player_queues/shuffle", queue_id=pid, shuffle_enabled=enabled)
            return f"Shuffle {'on' if enabled else 'off'} {where}."

        m_rep = re.match(r"^repeat\s+(off|all|one)\b", cleaned)
        if m_rep:
            mode = m_rep.group(1)
            pid, where, _, _ = self._extract_room(
                cleaned, room_maps, players_by_id, queues_by_id, cfg, prefer_active=True, accept_paused=True
            )
            self.call("player_queues/repeat", queue_id=pid, repeat_mode=mode)
            return f"Repeat set to {mode} {where}."

        # 6. Transport & volume commands
        is_transport = bool(
            re.match(
                r"^(?:stop|pause|resume|unpause|continue|next|skip|previous|prev|back|mute|unmute|volume|louder|quieter|softer|turn\s+(?:it\s+|the\s+volume\s+)?(?:up|down))\b",
                cleaned,
            )
            or re.search(r"\b(?:volume\s+(?:up|down|to|\d+)|set\s+.*\bvolume\b)", cleaned)
        )
        accept_paused = bool(re.match(r"^(?:resume|unpause|continue)\b", cleaned))
        pid, where, media, _ = self._extract_room(
            cleaned,
            room_maps,
            players_by_id,
            queues_by_id,
            cfg,
            prefer_active=is_transport,
            accept_paused=accept_paused,
        )

        # Stop everywhere
        if pid == "__all__":
            if re.match(r"^(?:stop|pause)\b", media):
                music_ids = cfg.music_speaker_ma_ids() or set(players_by_id.keys())
                stopped = 0
                for cand_pid in music_ids:
                    if (queues_by_id.get(cand_pid) or {}).get("state") == "playing":
                        self.call("player_queues/stop", queue_id=cand_pid)
                        self.call("players/cmd/stop", player_id=cand_pid)
                        stopped += 1
                return f"Stopped {stopped} speaker{'s' if stopped != 1 else ''}." if stopped else "Nothing was playing."
            raise MAError("Name a single room for that command.")

        player = players_by_id.get(pid)
        if player is None or not player.get("available"):
            raise MAError(f"The speaker {where} is not available right now.")

        if re.match(r"^(?:stop|pause)\b", media):
            if media.startswith("pause"):
                self.call("player_queues/pause", queue_id=pid)
                return f"Paused {where}."
            self.call("player_queues/stop", queue_id=pid)
            self.call("players/cmd/stop", player_id=pid)
            return "Stopped."

        if re.match(r"^(?:resume|unpause|continue)\b", media):
            self.call("player_queues/play", queue_id=pid)
            return f"Resumed {where}."

        if re.match(r"^(?:next|skip)\b", media):
            self.call("player_queues/next", queue_id=pid)
            q = self.call("player_queues/get", queue_id=pid) or {}
            t, a, _ = _describe_current_item(q.get("current_item"))
            if t:
                by = f" by {a}" if a else ""
                return f"Skipped to {t}{by}."
            return "Skipped track."

        if re.match(r"^(?:previous|prev|back)\b", media):
            self.call("player_queues/previous", queue_id=pid)
            return "Previous track."

        if re.match(r"^(?:unmute|mute)\b", media):
            muted = media.startswith("mute")
            self.call("players/cmd/volume_mute", player_id=pid, muted=muted)
            return f"{'Muted' if muted else 'Unmuted'} {where}."

        m_vol_pct = re.search(r"\bvolume\b.*?\b(\d{1,3})\b|\bset\b.*?\bvolume\s+(?:to\s+)?(\d{1,3})\b", media)
        if m_vol_pct:
            pct = max(0, min(100, int(m_vol_pct.group(1) or m_vol_pct.group(2))))
            self.call("players/cmd/volume_set", player_id=pid, volume_level=pct)
            return f"Volume set to {pct}% {where}."

        if re.search(r"\b(?:volume\s+up|turn\s+(?:it\s+|the\s+volume\s+)?up|louder)\b", media):
            cur = player.get("volume_level")
            new_vol = min(100, int(cur) + 10) if isinstance(cur, (int, float)) else 50
            self.call("players/cmd/volume_set", player_id=pid, volume_level=new_vol)
            return f"Volume up to {new_vol}% {where}."

        if re.search(r"\b(?:volume\s+down|turn\s+(?:it\s+|the\s+volume\s+)?down|quieter|softer)\b", media):
            cur = player.get("volume_level")
            new_vol = max(0, int(cur) - 10) if isinstance(cur, (int, float)) else 30
            self.call("players/cmd/volume_set", player_id=pid, volume_level=new_vol)
            return f"Volume down to {new_vol}% {where}."

        # 7. Play / Queue / Shuffle / Radio
        shuffle_req = bool(re.match(r"^shuffle\b", media))
        queue_add = bool(re.match(r"^(?:queue|add)\b", media))
        play_next = bool(queue_add and re.search(r"\bnext$", media))
        radio_mode = bool(re.search(r"\bradio$", media))

        q_text = re.sub(r"^(?:play|put on|start|shuffle|queue|add)\s+(?:some\s+)?", "", media).strip()
        q_text = re.sub(r"\s+(?:to\s+the\s+queue|next|radio|music)$", "", q_text).strip()
        if not q_text:
            raise MAError("Name an artist, album, track, or playlist to play.")

        kind, query = self._parse_media_kind(q_text)
        kinds = list(_SEARCH_TYPES) if kind == "auto" else [kind]
        search_res = self.call("music/search", search_query=query, media_types=kinds, limit=8) or {}
        lib_artists = None if " by " in query else self._library_artists()
        chosen_kind, item = pick_best_media(search_res, kind, query, lib_artists, cfg.playlist_owners)
        chosen_name = str(item.get("name") or query)
        artists = _item_artists(item)
        chosen_artist = ", ".join(artists) if artists else None
        uri = item.get("uri")
        if not uri:
            raise MAError(f"Matched {chosen_kind} '{chosen_name}' has no playable URI.")

        media_payload: Any = uri
        if chosen_kind == "artist" and not radio_mode and not queue_add:
            item_id = item.get("item_id")
            provider = item.get("provider")
            if item_id is not None and provider:
                try:
                    top_tracks = self.call(
                        "music/artists/top_tracks",
                        item_id=str(item_id),
                        provider_instance_id_or_domain=str(provider),
                    ) or []
                except MAError:
                    top_tracks = []
                allow_special = _wants_special_edition(query)
                filtered = [
                    dict(t)
                    for t in top_tracks
                    if isinstance(t, Mapping)
                    and t.get("uri")
                    and (allow_special or not _has_unwanted_edition(t))
                ]
                if filtered:
                    if shuffle_req:
                        random.shuffle(filtered)
                    media_payload = filtered[:25]

        option = ("next" if play_next else "add") if queue_add else "replace"
        self.call(
            "player_queues/play_media",
            queue_id=pid,
            media=media_payload,
            option=option,
            radio_mode=True if radio_mode else None,
        )
        if shuffle_req:
            self.call("player_queues/shuffle", queue_id=pid, shuffle_enabled=True)

        if queue_add:
            by = f" by {chosen_artist}" if chosen_artist and chosen_kind != "artist" else ""
            pos = "next in queue" if play_next else "to the queue"
            return f"Added {chosen_name}{by} {pos} {where}."

        q = self.call("player_queues/get", queue_id=pid) or {}
        started_title, started_artist, _ = _describe_current_item(q.get("current_item"))
        verb = "Shuffling" if shuffle_req else ("Starting" if radio_mode else "Playing")
        if radio_mode:
            tail = f", starting with {started_title}" if started_title else ""
            return f"Starting {chosen_name} radio {where}{tail}."
        if chosen_kind == "artist":
            tail = f", starting with {started_title}" if started_title else ""
            return f"{verb} {chosen_name} {where}{tail}."
        disp = started_title or chosen_name
        art = started_artist or chosen_artist
        by = f" by {art}" if art else ""
        return f"{verb} {disp}{by} {where}."
