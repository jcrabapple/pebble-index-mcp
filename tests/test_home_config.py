import json
import os
import time

from pebble_index_mcp.home_config import HomeConfigLoader, parse_home_config


def test_parse_home_config_rooms_aliases_and_asr():
    cfg = parse_home_config(
        {
            "rooms": {
                "basement": {"ha_entity": "media_player.basement_2", "ma_player_id": "pid-base", "spoken": "in the basement"},
                "guest room": {"ha_entity": "media_player.guest_2", "ma_player_id": "pid-guest"},
                "office": {"ha_entity": "media_player.office_2", "ma_player_id": "pid-office"},
            },
            "aliases": {"downstairs": "basement", "alex": "office"},
            "default_players": ["office", "basement"],
            "intercom": {"tts_entity": "tts.harper", "voice": "en-GB-Ollie", "everyone_exclude": ["guest room"]},
            "asr_corrections": {"bill murray": "Bilmuri"},
        }
    )
    assert cfg.resolve_room_name("downstairs") == "basement"
    assert cfg.resolve_room_name("Alex") == "office"
    assert cfg.resolve_room_name("unknown") is None
    assert [r.name for r in cfg.everyone_rooms()] == ["basement", "office"]
    assert cfg.apply_asr_corrections("play bill murray in the basement") == "play Bilmuri in the basement"


def test_loader_reloads_on_mtime_change(tmp_path):
    p = tmp_path / "home.json"
    p.write_text(json.dumps({"rooms": {"kitchen": {"ha_entity": "media_player.k_2", "ma_player_id": "p1"}}}))
    loader = HomeConfigLoader(p)
    assert "kitchen" in loader.get().rooms

    # Update with newer mtime
    p.write_text(json.dumps({"rooms": {"foyer": {"ha_entity": "media_player.f_2", "ma_player_id": "p2"}}}))
    future = time.time() + 10
    os.utime(p, (future, future))
    cfg2 = loader.get()
    assert "foyer" in cfg2.rooms and "kitchen" not in cfg2.rooms


def test_loader_missing_file_returns_empty_defaults(tmp_path):
    loader = HomeConfigLoader(tmp_path / "does-not-exist.json")
    cfg = loader.get()
    assert cfg.rooms == {}
    assert cfg.aliases == {}
