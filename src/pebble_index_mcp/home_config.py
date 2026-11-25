"""Shared home-voice configuration loader (rooms, aliases, intercom, routines, ASR fixes).

Reads a single JSON config file (default ``~/.config/home-voice/home.json``,
overridable via ``HOME_VOICE_CONFIG``) and caches by ``(path, mtime)`` so edits
apply live without restarting services. Missing config files resolve cleanly to
empty defaults so the server stays generic and runnable with zero setup.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pebble_index_mcp")

DEFAULT_HOME_CONFIG_PATH = Path.home() / ".config" / "home-voice" / "home.json"


def norm_key(text: str) -> str:
    """Lowercase, strip apostrophes/punctuation, collapse whitespace."""
    s = (text or "").lower().replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class RoomSpec:
    name: str
    ha_entity: str = ""
    ma_player_id: str = ""
    spoken: str = ""


@dataclass(frozen=True)
class HomeConfig:
    rooms: dict[str, RoomSpec] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # normalized alias -> canonical room name
    default_players: tuple[str, ...] = ()
    intercom_tts_entity: str = ""
    intercom_voice: str = ""
    intercom_everyone_exclude: frozenset[str] = frozenset()
    common_light_groups: tuple[str, ...] = ()
    all_light_groups: tuple[str, ...] = ()
    asr_corrections: dict[str, str] = field(default_factory=dict)
    playlist_owners: frozenset[str] = frozenset({"music assistant"})

    def resolve_room_name(self, raw: str) -> str | None:
        """Resolve a spoken room or person alias to a canonical room name."""
        key = norm_key(raw)
        if not key:
            return None
        for rname in self.rooms:
            if norm_key(rname) == key:
                return rname
        mapped = self.aliases.get(key)
        if mapped and mapped in self.rooms:
            return mapped
        return None

    def all_room_keys_longest_first(self) -> list[tuple[str, str]]:
        """Return (normalized_key, canonical_room_name) sorted longest key first."""
        seen: dict[str, str] = {}
        for rname in self.rooms:
            nk = norm_key(rname)
            if nk:
                seen[nk] = rname
        for alias_k, target in self.aliases.items():
            if target in self.rooms and alias_k not in seen:
                seen[alias_k] = target
        pairs = list(seen.items())
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        return pairs

    def everyone_rooms(self) -> list[RoomSpec]:
        """Rooms targeted by 'everyone' intercom broadcasts (excluding configured skips)."""
        excluded = {norm_key(x) for x in self.intercom_everyone_exclude}
        out: list[RoomSpec] = []
        seen_entities: set[str] = set()
        for rname, spec in self.rooms.items():
            if norm_key(rname) in excluded or not spec.ha_entity:
                continue
            if spec.ha_entity in seen_entities:
                continue
            seen_entities.add(spec.ha_entity)
            out.append(spec)
        return out

    def room_by_ma_id(self, player_id: str) -> RoomSpec | None:
        for spec in self.rooms.values():
            if spec.ma_player_id == player_id:
                return spec
        return None

    def music_speaker_ma_ids(self) -> frozenset[str]:
        return frozenset(s.ma_player_id for s in self.rooms.values() if s.ma_player_id)

    def apply_asr_corrections(self, text: str) -> str:
        """Replace known STT homophones (e.g. 'bill murray' -> 'Bilmuri')."""
        out = text
        for wrong, right in self.asr_corrections.items():
            if wrong:
                out = re.sub(rf"\b{re.escape(wrong)}\b", right, out, flags=re.IGNORECASE)
        return out


def parse_home_config(data: dict[str, Any]) -> HomeConfig:
    rooms: dict[str, RoomSpec] = {}
    for raw_name, spec in (data.get("rooms") or {}).items():
        if not isinstance(spec, dict):
            continue
        name = str(raw_name).strip().lower()
        if not name:
            continue
        rooms[name] = RoomSpec(
            name=name,
            ha_entity=str(spec.get("ha_entity") or "").strip(),
            ma_player_id=str(spec.get("ma_player_id") or "").strip(),
            spoken=str(spec.get("spoken") or f"in the {name}").strip(),
        )

    aliases: dict[str, str] = {}
    for raw_alias, raw_target in (data.get("aliases") or {}).items():
        nk = norm_key(str(raw_alias))
        target = str(raw_target).strip().lower()
        if nk and target in rooms:
            aliases[nk] = target

    default_players = tuple(
        str(p).strip() for p in (data.get("default_players") or []) if str(p).strip()
    )
    intercom = data.get("intercom") or {}
    routines = data.get("routines") or {}
    asr = {
        str(k).strip().lower(): str(v).strip()
        for k, v in (data.get("asr_corrections") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    owners = frozenset(
        norm_key(str(o)) for o in (data.get("playlist_owners") or ["music assistant"]) if str(o).strip()
    )

    return HomeConfig(
        rooms=rooms,
        aliases=aliases,
        default_players=default_players,
        intercom_tts_entity=str(intercom.get("tts_entity") or "").strip(),
        intercom_voice=str(intercom.get("voice") or "").strip(),
        intercom_everyone_exclude=frozenset(
            str(x).strip().lower() for x in (intercom.get("everyone_exclude") or []) if str(x).strip()
        ),
        common_light_groups=tuple(
            str(x).strip() for x in (routines.get("common_light_groups") or []) if str(x).strip()
        ),
        all_light_groups=tuple(
            str(x).strip() for x in (routines.get("all_light_groups") or []) if str(x).strip()
        ),
        asr_corrections=asr,
        playlist_owners=owners or frozenset({"music assistant"}),
    )


class HomeConfigLoader:
    """mtime-cached loader for the shared home.json config."""

    def __init__(self, path: str | Path | None = None):
        self._explicit_path = Path(path) if path else None
        self._cache: tuple[str, float, HomeConfig] | None = None

    def _target_path(self) -> Path | None:
        if self._explicit_path is not None:
            return self._explicit_path
        env_p = os.environ.get("HOME_VOICE_CONFIG", "").strip()
        if env_p:
            return Path(env_p).expanduser()
        if DEFAULT_HOME_CONFIG_PATH.is_file():
            return DEFAULT_HOME_CONFIG_PATH
        return None

    def get(self) -> HomeConfig:
        p = self._target_path()
        if p is None:
            return HomeConfig()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return HomeConfig()
        key = str(p)
        if self._cache and self._cache[0] == key and self._cache[1] == mtime:
            return self._cache[2]
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            cfg = parse_home_config(raw) if isinstance(raw, dict) else HomeConfig()
        except (OSError, ValueError) as e:
            logger.warning("Failed to load home config %s: %s", p, e)
            return HomeConfig()
        self._cache = (key, mtime, cfg)
        return cfg
