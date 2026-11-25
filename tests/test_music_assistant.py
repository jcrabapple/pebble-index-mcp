import pytest

from pebble_index_mcp.home_config import parse_home_config
from pebble_index_mcp.music_assistant import MAError, MusicAssistantClient, pick_best_media


def _home_cfg():
    return parse_home_config(
        {
            "rooms": {
                "voice": {"ha_entity": "media_player.voice", "ma_player_id": "pid-voice", "spoken": "on the Voice PE"},
                "basement": {"ha_entity": "media_player.base_2", "ma_player_id": "pid-base", "spoken": "in the basement"},
                "kitchen": {"ha_entity": "media_player.kit_2", "ma_player_id": "pid-kit", "spoken": "in the kitchen"},
            },
            "aliases": {"downstairs": "basement", "voice pe": "voice"},
            "default_players": ["voice", "basement"],
            "asr_corrections": {"bill murray": "Bilmuri"},
        }
    )


def _client():
    return MusicAssistantClient(url="http://ma.test:8095", token="tok", home_config=_home_cfg())


def _players(voice_avail=True):
    return [
        {"player_id": "pid-voice", "name": "Voice PE", "available": voice_avail, "volume_level": 45},
        {"player_id": "pid-base", "name": "Basement speaker", "available": True, "volume_level": 40},
        {"player_id": "pid-kit", "name": "Kitchen display", "available": True, "volume_level": 50},
        {"player_id": "pid-tv", "name": "Living Room TV", "available": True, "volume_level": 25},
    ]


def test_ranking_penalizes_karaoke_and_prefers_real_track():
    res = {
        "tracks": [
            {"name": "Granite (Karaoke Version)", "uri": "deezer://track/1", "artists": [{"name": "Karaoke Kings"}]},
            {"name": "Granite", "uri": "library://track/2", "artists": [{"name": "Sleep Token"}]},
        ]
    }
    kind, item = pick_best_media(res, "track", "Granite")
    assert kind == "track"
    assert item["uri"] == "library://track/2"


def test_play_artist_applies_asr_fix_filters_top_tracks_and_falls_back_default(monkeypatch):
    c = _client()
    calls = []

    def fake_call(command, **args):
        calls.append((command, args))
        if command == "players/all":
            return _players(voice_avail=False)  # Voice PE offline -> falls back to basement
        if command == "player_queues/all":
            return []
        if command == "music/artists/library_items":
            return []
        if command == "music/search":
            return {
                "artists": [{"name": "Bilmuri", "item_id": "42", "provider": "deezer", "uri": "deezer://artist/42"}],
                "tracks": [],
                "albums": [],
                "playlists": [],
            }
        if command == "music/artists/top_tracks":
            return [
                {"name": "EMPTYHANDED", "uri": "deezer://track/100"},
                {"name": "EMPTYHANDED (Karaoke)", "uri": "deezer://track/101"},
            ]
        if command == "player_queues/play_media":
            return None
        if command == "player_queues/get":
            return {"current_item": {"media_item": {"name": "EMPTYHANDED", "artists": [{"name": "Bilmuri"}]}}}
        return None

    monkeypatch.setattr(c, "call", fake_call)
    out = c.command("play Bill Murray")
    assert any(cmd == "music/search" and a["search_query"] == "bilmuri" for cmd, a in calls)
    play_call = next(a for cmd, a in calls if cmd == "player_queues/play_media")
    assert play_call["queue_id"] == "pid-base"
    assert len(play_call["media"]) == 1  # karaoke filtered out
    assert "Playing Bilmuri in the basement, starting with EMPTYHANDED." == out


def test_status_transport_favorite_queue_and_transfer(monkeypatch):
    c = _client()
    calls = []

    def fake_call(command, **args):
        calls.append((command, args))
        if command == "players/all":
            return _players()
        if command == "player_queues/all":
            return [
                # TV is playing, basement is playing -> TV ignored, basement picked
                {"queue_id": "pid-tv", "state": "playing", "items": 1, "current_item": {"name": "Netflix"}},
                {
                    "queue_id": "pid-base",
                    "state": "playing",
                    "items": 3,
                    "current_index": 0,
                    "current_item": {
                        "media_item": {
                            "name": "The Summoning",
                            "uri": "library://track/7",
                            "artists": [{"name": "Sleep Token"}],
                        }
                    },
                },
            ]
        if command == "player_queues/items":
            return [
                {"media_item": {"name": "The Summoning", "artists": [{"name": "Sleep Token"}]}},
                {"media_item": {"name": "Granite", "artists": [{"name": "Sleep Token"}]}},
            ]
        return None

    monkeypatch.setattr(c, "call", fake_call)

    assert c.command("what's playing") == "Playing The Summoning by Sleep Token in the basement."
    assert "Granite" in c.command("what's up next")
    assert "Granite" in c.command("what is up next in the basement")
    assert c.command("favorite this song") == "Favorited The Summoning by Sleep Token."
    assert ("music/favorites/add_item", {"item": "library://track/7"}) in calls

    assert c.command("transfer music to kitchen") == "Transferred music in the kitchen."
    assert ("player_queues/transfer", {"source_queue_id": "pid-base", "target_queue_id": "pid-kit"}) in calls

    assert c.command("stop everywhere") == "Stopped 1 speaker."
    assert ("player_queues/stop", {"queue_id": "pid-base"}) in calls


def test_transfer_empty_source_refused(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "call",
        lambda cmd, **a: _players() if cmd == "players/all" else [{"queue_id": "pid-base", "state": "idle", "items": 0}],
    )
    with pytest.raises(MAError, match="empty"):
        c.command("transfer from basement to kitchen")
