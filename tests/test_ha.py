import pytest

from pebble_index_mcp.homeassistant import (
    HAError,
    HAClient,
    parse_command,
    parse_music_rooms_env,
)


def _state(entity_id, friendly, state="off", domain=None):
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": friendly},
    }


# Mirrors the real registry shapes: room group is light.<room>_<room>,
# individual fixtures are light.<room>_<room>_lamp_N, and there can be
# duplicate friendly names where one group is unavailable.
def _states():
    return [
        _state("light.living_room_living_room", "Living Room", "on"),
        _state("light.living_room_living_room_lamp_1", "Living room lamp 1", "on"),
        _state("light.living_room_living_room_lamp_2", "Living room lamp 2", "on"),
        _state("light.game_room_game_room", "Game room"),
        _state("light.game_room_game_room_lamp_1", "Game room lamp 1"),
        _state("light.hallway_hallway", "Hallway", "on"),
        _state("light.bedroom_bedroom", "Bedroom", "unavailable"),
        _state("light.bedroom_bedroom_2", "Bedroom"),
        _state("light.storage_closet_storage_closet", "Storage Closet"),
        _state("light.alex_s_lamp", "Alex's lamp"),
        _state("cover.office_blinds", "Office Blinds", "closed"),
        _state("media_player.living_room_tv_2", "Living Room TV", "paused"),
        _state("switch.home_assistant_voice_mute", "Home Assistant Voice Mute"),
    ]


def _client(**kwargs):
    return HAClient(url="http://ha.test:8123", token="ha-token", **kwargs)


# --- parse_command ---------------------------------------------------------

def test_parse_simple_on():
    assert parse_command("living room lights on") == ("on", ["living", "room", "light"])


def test_parse_strip_filler_and_action_position():
    action, target = parse_command("Turn off the Kitchen Fan please")
    assert action == "off"
    assert target == ["kitchen", "fan"]


def test_parse_last_action_word_wins():
    action, target = parse_command("turn the closet light off")
    assert action == "off"
    assert target == ["closet", "light"]


def test_parse_closet_does_not_trigger_close():
    # "closet" must not match the \bclose\b action regex
    action, target = parse_command("storage closet light on")
    assert action == "on"
    assert target == ["storage", "closet", "light"]


def test_parse_status_question():
    action, target = parse_command("is the living room tv on")
    assert action == "status"
    assert target == ["living", "room", "tv"]


def test_parse_status_keyword():
    assert parse_command("bedroom status")[0] == "status"


def test_parse_toggle():
    assert parse_command("toggle the hallway")[0] == "toggle"


def test_parse_missing_action_raises():
    with pytest.raises(HAError):
        parse_command("do something with the lights")


def test_parse_empty_raises():
    with pytest.raises(HAError):
        parse_command("   ")


# --- resolution ------------------------------------------------------------

def _resolve(client, tokens):
    return client.resolve(_states(), tokens)


def test_resolve_prefers_room_group():
    entity = _resolve(_client(), ["living", "room", "light"])
    assert entity["entity_id"] == "light.living_room_living_room"


def test_resolve_specific_lamp_beats_group():
    entity = _resolve(_client(), ["living", "room", "lamp", "2"])
    assert entity["entity_id"] == "light.living_room_living_room_lamp_2"


def test_resolve_skips_unavailable_duplicate():
    entity = _resolve(_client(), ["bedroom", "light"])
    assert entity["entity_id"] == "light.bedroom_bedroom_2"


def test_parse_possessive_splits_apostrophe():
    # "alex's" must tokenize as alex/s, not a dead "alex's" token that
    # singularize() mangles into "alex'"
    action, target = parse_command("turn on Alex's lamp")
    assert action == "on"
    assert target == ["alex", "lamp"]


def test_resolve_possessive_entity():
    entity = _resolve(_client(), ["alex", "lamp"])
    assert entity["entity_id"] == "light.alex_s_lamp"


def test_control_possessive_end_to_end(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("turn on Alex's lamp")
    assert fake.posts[0][1] == {"entity_id": "light.alex_s_lamp"}
    assert "now on" in out


def test_control_cover_open_and_close(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    # Already closed -> short-circuits instead of toggling open
    out_closed = c.control("close office blinds")
    assert fake.posts == []
    assert "already closed" in out_closed
    # Open calls open_cover, not toggle
    out_open = c.control("open office blinds")
    assert fake.posts[0][0] == "/api/services/cover/open_cover"
    assert "now open" in out_open
    # Close calls close_cover
    out_close = c.control("close office blinds")
    assert fake.posts[1][0] == "/api/services/cover/close_cover"
    assert "now closed" in out_close


def test_resolve_media_player():
    entity = _resolve(_client(), ["living", "room", "tv"])
    assert entity["entity_id"] == "media_player.living_room_tv_2"


def test_resolve_bare_lights_is_ambiguous():
    with pytest.raises(HAError) as exc:
        _resolve(_client(), ["light"])
    assert "Living Room" in str(exc.value)
    assert "Game room" in str(exc.value)


def test_resolve_no_match_suggests_partials():
    with pytest.raises(HAError) as exc:
        _resolve(_client(), ["garage", "door"])
    assert "garage" in str(exc.value)


# --- control flow ----------------------------------------------------------

class FakeHA:
    def __init__(self, states, apply=True):
        self.states = states
        self.posts = []
        self.apply = apply
        self.play_queue = []
        self.fail_first_plays = 0

    def get(self, url, headers=None, timeout=None):
        if url.endswith("/api/states") or "/api/states/" in url:
            entity_id = url.rsplit("/api/states/", 1)[1] if "/api/states/" in url else None
            if entity_id:
                for s in self.states:
                    if s["entity_id"] == entity_id:
                        return 200, s
                return 404, {"message": "not found"}
            return 200, self.states
        return 404, {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        if not self.apply:
            return 200, []
        service = url.rsplit("/", 2)[1:]
        if len(service) == 2 and json and "entity_id" in json:
            domain, svc = service
            eid = json["entity_id"]
            if svc == "play_media":
                if self.fail_first_plays > 0:
                    self.fail_first_plays -= 1
                    return 500, "Media not found"
                attrs = self.play_queue.pop(0) if self.play_queue else {}
                for s in self.states:
                    if s["entity_id"] == eid:
                        s["state"] = "playing"
                        s["attributes"].update(attrs)
                return 200, []
            new_state = {
                "turn_on": "on",
                "turn_off": "off",
                "toggle": "on",
                "open_cover": "open",
                "close_cover": "closed",
            }.get(svc)
            for s in self.states:
                if s["entity_id"] == eid and new_state:
                    s["state"] = new_state
        return 200, []


def test_control_turn_on_calls_service_and_verifies(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("game room lights on")
    assert fake.posts[0][0] == "/api/services/light/turn_on"
    assert fake.posts[0][1] == {"entity_id": "light.game_room_game_room"}
    assert "now on" in out


def test_control_already_in_state_short_circuits(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("living room lights on")
    assert fake.posts == []
    assert "already on" in out


def test_control_no_state_change_reports_honestly(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    fake.apply = False
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("game room lights on")
    assert fake.posts, "service call should still have been attempted"
    assert "did not respond" in out


def _sat(entity_id, state="idle"):
    return {"entity_id": entity_id, "state": state, "attributes": {"friendly_name": entity_id}}


def test_announce_targets_only_live_satellites(monkeypatch):
    c = _client()
    states = [
        _sat("assist_satellite.one", "idle"),
        _sat("assist_satellite.two", "unavailable"),
    ]
    posts = []

    def fake_get(path):
        return states

    def fake_post(path, payload):
        posts.append((path, payload))
        return []

    monkeypatch.setattr(c, "_get", fake_get)
    monkeypatch.setattr(c, "_post", fake_post)
    out = c.announce("dinner is ready")
    assert posts[0][0] == "/api/services/assist_satellite/announce"
    assert posts[0][1]["entity_id"] == ["assist_satellite.one"]
    assert posts[0][1]["message"] == "dinner is ready"
    assert posts[0][1]["preannounce"] is True
    assert "1 speaker" in out


def test_announce_no_live_satellite(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c, "_get",
        lambda path: [_sat("assist_satellite.dead", "unavailable")],
    )
    with pytest.raises(HAError):
        c.announce("hello")


def test_announce_empty_message(monkeypatch):
    with pytest.raises(HAError):
        _client().announce("   ")


def test_announce_missing_token():
    c = HAClient(url="http://ha.test:8123", token="")
    with pytest.raises(HAError, match="HASS_TOKEN"):
        c.announce("hello")


# --- music (Music Assistant via HA) -----------------------------------------

def _mu_state(entity_id, state="idle", **attrs):
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def _music_states():
    return [
        _mu_state("media_player.home_assistant_voice_test", friendly_name="Voice PE"),
        _mu_state("media_player.basement_speaker_1_2", friendly_name="Basement speaker"),
        _mu_state("media_player.living_room_speaker_2", friendly_name="Living Room"),
        _mu_state("media_player.kitchen_display_2", friendly_name="Kitchen display"),
        _mu_state("media_player.taylor_s_room_speaker_2", friendly_name="Taylor's Room Speaker"),
        _mu_state("media_player.emily_s_room_speaker_2", friendly_name="Emily's Room speaker"),
        _mu_state("media_player.master_bedroom_speaker_2", "unavailable", friendly_name="Master Bedroom speaker"),
    ]


def test_music_play_default_player_and_verify(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    fake.play_queue = [{"media_title": "Circle With Me", "media_artist": "Spiritbox"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play Spiritbox")
    url, payload = fake.posts[0]
    assert url == "/api/services/media_player/play_media"
    assert payload["entity_id"] == "media_player.home_assistant_voice_test"
    assert payload["media_content_type"] == "artist"
    assert payload["media_content_id"] == "spiritbox"
    assert "Circle With Me" in out and "Spiritbox" in out


def test_music_default_player_falls_back_when_first_unavailable(monkeypatch):
    states = _music_states()
    states[0]["state"] = "unavailable"
    c = _client(
        default_players="media_player.home_assistant_voice_test,media_player.living_room_speaker_2"
    )
    fake = FakeHA(states)
    fake.play_queue = [{"media_title": "Granite", "media_artist": "Sleep Token"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play Sleep Token")
    assert fake.posts[0][1]["entity_id"] == "media_player.living_room_speaker_2"
    assert "Granite" in out


def test_music_room_routing_and_possessive(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    fake.play_queue = [
        {"media_title": "Casanova Fluid Hair"},
        {"media_title": "Casanova Fluid Hair"},
        {"media_title": "Casanova Fluid Hair"},
    ]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    c.music("play Bilmuri in the basement")
    assert fake.posts[0][1]["entity_id"] == "media_player.basement_speaker_1_2"
    c.music("play Bilmuri in Taylor's room")
    assert fake.posts[1][1]["entity_id"] == "media_player.taylor_s_room_speaker_2"
    # Regression: possessive ending in y ("Emily's room" -> "emilys room") must match
    c.music("play Bilmuri in Emily's room")
    assert fake.posts[2][1]["entity_id"] == "media_player.emily_s_room_speaker_2"


def test_music_configured_room_alias(monkeypatch):
    c = _client(music_rooms=parse_music_rooms_env("gym=media_player.basement_speaker_1_2"))
    fake = FakeHA(_music_states())
    fake.play_queue = [{"media_title": "Holy Roller", "media_artist": "Spiritbox"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play Spiritbox in the gym")
    assert fake.posts[0][1]["entity_id"] == "media_player.basement_speaker_1_2"
    assert "Holy Roller" in out


def test_music_missing_token():
    c = HAClient(url="http://ha.test:8123", token="")
    with pytest.raises(HAError, match="HASS_TOKEN"):
        c.music("play Spiritbox")


def test_music_album_and_track_types(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    fake.play_queue = [{"media_title": "Blue Rev"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    c.music("play the album Blue Rev in the kitchen")
    assert fake.posts[0][1]["media_content_type"] == "album"
    fake.play_queue = [{"media_title": "Granite", "media_artist": "Sleep Token"}]
    c.music("play the song Granite")
    assert fake.posts[-1][1]["media_content_type"] == "track"


def test_music_falls_back_to_track_when_artist_empty(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    # First attempt (artist) yields no title; second (track) succeeds.
    fake.play_queue = [{}, {"media_title": "Granite", "media_artist": "Sleep Token"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play Granite")
    types = [p[1]["media_content_type"] for p in fake.posts]
    assert types == ["artist", "track"]
    assert "Granite" in out


def test_music_stop(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("stop the music in the basement")
    assert fake.posts[0][0] == "/api/services/media_player/media_stop"
    assert fake.posts[0][1]["entity_id"] == "media_player.basement_speaker_1_2"
    assert "topped" in out


def test_music_unavailable_player(monkeypatch):
    c = _client(music_rooms={"bedroom": "media_player.master_bedroom_speaker_2"})
    fake = FakeHA(_music_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    with pytest.raises(HAError):
        c.music("play Spiritbox in the bedroom")


def test_music_unknown_room(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    with pytest.raises(HAError) as exc:
        c.music("play Spiritbox on the porch")
    assert "basement" in str(exc.value)


def test_music_500_triggers_fallback(monkeypatch):
    # MA returns HTTP 500 when a search resolves to nothing (STT-mangled
    # names); the tool must try the next interpretation, not abort.
    c = _client()
    fake = FakeHA(_music_states())
    fake.fail_first_plays = 1
    fake.play_queue = [{"media_title": "Jaded", "media_artist": "Spiritbox"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play jaded")
    types = [p[1]["media_content_type"] for p in fake.posts if p[0].endswith("play_media")]
    assert types == ["artist", "track"]
    assert "Jaded" in out


def test_music_all_misses_report_honestly(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    fake.fail_first_plays = 99
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play qzxvk wjmnv")
    assert "could not find" in out


def test_music_by_phrase_splits_variants(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    # First interpretation ("jaded by spiritbox") misses; the split variant
    # ("spiritbox jaded") must be what lands.
    fake.fail_first_plays = 1
    fake.play_queue = [{}, {"media_title": "Jaded", "media_artist": "Spiritbox"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.music("play the song jaded by spiritbox")
    ids = [p[1]["media_content_id"] for p in fake.posts if p[0].endswith("play_media")]
    assert "jaded by spiritbox" in ids and "spiritbox jaded" in ids
    assert "Jaded" in out


def test_control_status_never_posts(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("is the living room tv on")
    assert fake.posts == []
    assert "paused" in out


def test_control_off(monkeypatch):
    c = _client()
    states = _states()
    fake = FakeHA(states)
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("turn off the hallway lights")
    assert fake.posts[0][0] == "/api/services/light/turn_off"
    assert "off" in out


def test_control_http_error(monkeypatch):
    c = _client()

    def boom(*a, **k):
        raise HAError("HTTP 401: invalid token")

    monkeypatch.setattr(c, "_get_raw", boom)
    with pytest.raises(HAError):
        c.control("living room lights on")


# --- shopping_list ---------------------------------------------------------

def test_shopping_list_read_items(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_post",
        lambda path, payload: {
            "service_response": {
                "todo.shopping_list": {
                    "items": [{"summary": "Milk"}, {"summary": "Oat milk"}]
                }
            }
        },
    )
    out = c.shopping_list("what is on my shopping list")
    assert out == "Shopping list (2 items): Milk, Oat milk."


def test_shopping_list_read_empty(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_post",
        lambda path, payload: {"service_response": {"todo.shopping_list": {"items": []}}},
    )
    assert c.shopping_list("shopping list") == "Your shopping list is empty."


def test_shopping_list_add_strips_tail(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "_post", lambda path, payload: calls.append((path, payload)) or [])
    out = c.shopping_list("add some oat milk to the shopping list")
    assert calls[0] == (
        "/api/services/todo/add_item",
        {"entity_id": "todo.shopping_list", "item": "oat milk"},
    )
    assert out == "Added oat milk to the shopping list."


def test_shopping_list_check_off_case_insensitive(monkeypatch):
    c = _client()
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        if "get_items" in path:
            return {
                "service_response": {
                    "todo.shopping_list": {"items": [{"summary": "Milk"}, {"summary": "Bread"}]}
                }
            }
        return []

    monkeypatch.setattr(c, "_post", fake_post)
    out = c.shopping_list("check off milk from the shopping list")
    assert calls[1] == (
        "/api/services/todo/remove_item",
        {"entity_id": "todo.shopping_list", "item": "Milk"},
    )
    assert out == "Checked off Milk."


def test_shopping_list_check_off_missing(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_post",
        lambda path, payload: {
            "service_response": {"todo.shopping_list": {"items": [{"summary": "Milk"}]}}
        },
    )
    out = c.shopping_list("check off eggs")
    assert "not on the shopping list" in out


def test_shopping_list_check_off_does_not_delete_shorter_item(monkeypatch):
    c = _client()
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        if "get_items" in path:
            return {"service_response": {"todo.shopping_list": {"items": [{"summary": "Milk"}]}}}
        return []

    monkeypatch.setattr(c, "_post", fake_post)
    out = c.shopping_list("check off almond milk")
    assert "not on the shopping list" in out
    assert len(calls) == 1  # never called remove_item


def test_shopping_list_add_multiple_and_need_phrasing(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "_post", lambda path, payload: calls.append((path, payload)) or [])
    out = c.shopping_list("we need milk and eggs")
    assert [p["item"] for _, p in calls] == ["milk", "eggs"]
    assert out == "Added milk, eggs to the shopping list."


def test_shopping_list_keeps_compound_grocery_item(monkeypatch):
    c = _client()
    calls = []
    monkeypatch.setattr(c, "_post", lambda path, payload: calls.append((path, payload)) or [])
    out = c.shopping_list("add mac and cheese to the shopping list")
    assert [p["item"] for _, p in calls] == ["mac and cheese"]
    assert out == "Added mac and cheese to the shopping list."


# --- weather ---------------------------------------------------------------

def test_weather_current_and_forecast(monkeypatch):
    c = _client()

    def fake_get(path):
        assert path == "/api/states/weather.home"
        return {
            "state": "sunny",
            "attributes": {
                "temperature": 44,
                "temperature_unit": "°F",
                "wind_speed": 3.4,
                "wind_speed_unit": "mph",
            },
        }

    def fake_post(path, payload):
        return {
            "service_response": {
                "weather.home": {
                    "forecast": [
                        {
                            "datetime": "2026-11-10T00:00:00+00:00",
                            "condition": "cloudy",
                            "temperature": 67,
                            "templow": 40,
                            "precipitation": 0.0,
                        },
                        {
                            "datetime": "2026-11-11T00:00:00+00:00",
                            "condition": "rainy",
                            "temperature": 61,
                            "templow": 46,
                            "precipitation": 0.25,
                        },
                    ]
                }
            }
        }

    monkeypatch.setattr(c, "_get", fake_get)
    monkeypatch.setattr(c, "_post", fake_post)
    out = c.weather("what is the weather")
    assert "Currently 44°F and sunny." in out
    assert "Today: cloudy, high 67°F, low 40°F." in out
    assert "Tomorrow: rainy, high 61°F, low 46°F, 0.25 in rain." in out


def test_weather_uses_reported_precip_unit(monkeypatch):
    c = _client()
    monkeypatch.setattr(
        c,
        "_get",
        lambda path: {
            "state": "rainy",
            "attributes": {
                "temperature": 12,
                "temperature_unit": "°C",
                "precipitation_unit": "mm",
            },
        },
    )
    monkeypatch.setattr(
        c,
        "_post",
        lambda path, payload: {
            "service_response": {
                "weather.home": {
                    "forecast": [
                        {
                            "datetime": "2026-11-10T00:00:00+00:00",
                            "condition": "rainy",
                            "temperature": 14,
                            "templow": 8,
                            "precipitation": 4.2,
                        }
                    ]
                }
            }
        },
    )
    out = c.weather("weather")
    assert "4.2 mm rain" in out


def test_weather_sunrise_sunset(monkeypatch):
    c = _client()

    def fake_get(path):
        if "rising" in path:
            return {"state": "2026-11-10T11:48:20+00:00"}
        return {"state": "2026-11-10T22:03:56+00:00"}

    monkeypatch.setattr(c, "_get", fake_get)
    out = c.weather("when is sunset")
    assert "Next sunrise is" in out and "next sunset is" in out


# --- Pass 2: scenes, brightness, all-lights, music transport, hourly weather -

def test_control_scenes_brightness_and_all_lights(monkeypatch):
    c = _client()
    states = _states() + [
        {
            "entity_id": "light.basement_basement",
            "state": "on",
            "attributes": {"friendly_name": "Basement", "brightness": 204},
        },
        {
            "entity_id": "scene.basement_basement_relax",
            "state": "unknown",
            "attributes": {"friendly_name": "Basement Relax"},
        },
        {
            "entity_id": "scene.living_room_living_room_dimmed",
            "state": "unknown",
            "attributes": {"friendly_name": "Living Room Dimmed"},
        },
    ]
    fake = FakeHA(states)
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)

    # Scene activation (explicit cue and natural phrasing)
    out_scene = c.control("activate basement relax")
    assert out_scene == "Activated Basement Relax."
    assert fake.posts[0] == (
        "/api/services/scene/turn_on",
        {"entity_id": "scene.basement_basement_relax"},
    )

    out_scene2 = c.control("set living room to dimmed")
    assert out_scene2 == "Activated Living Room Dimmed."
    assert fake.posts[1] == (
        "/api/services/scene/turn_on",
        {"entity_id": "scene.living_room_living_room_dimmed"},
    )

    # Explicit brightness percentage
    out_dim_pct = c.control("dim living room to 30%")
    assert out_dim_pct == "Living Room set to 30%."
    assert fake.posts[2] == (
        "/api/services/light/turn_on",
        {"entity_id": "light.living_room_living_room", "brightness_pct": 30},
    )

    # Relative dim from 80% (204/255) -> 50%
    out_dim_rel = c.control("dim the basement")
    assert out_dim_rel == "Basement set to 50%."
    assert fake.posts[3] == (
        "/api/services/light/turn_on",
        {"entity_id": "light.basement_basement", "brightness_pct": 50},
    )

    # Status reports brightness when available
    out_status = c.control("is the basement light on")
    assert out_status == "Basement: on at 80%."

    # All lights off
    out_all_off = c.control("turn off all lights")
    assert out_all_off == "Turned off all lights."
    assert fake.posts[4][0] == "/api/services/light/turn_off"
    assert "light.basement_basement" in fake.posts[4][1]["entity_id"]


def test_music_transport_volume_and_now_playing(monkeypatch):
    c = _client()
    states = [
        _mu_state("media_player.home_assistant_voice_test", state="idle", friendly_name="Voice PE"),
        _mu_state(
            "media_player.basement_speaker_1_2",
            state="playing",
            friendly_name="Basement speaker",
            media_title="The Summoning",
            media_artist="Sleep Token",
            volume_level=0.40,
        ),
        _mu_state("media_player.kitchen_display_2", state="idle", friendly_name="Kitchen display", volume_level=0.50),
    ]
    fake = FakeHA(states)
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)

    # Now playing finds actively playing speaker without room name
    assert c.music("what's playing") == "Playing The Summoning by Sleep Token on Basement speaker."

    # Roomless skip targets actively playing speaker
    out_skip = c.music("skip this song")
    assert fake.posts[0] == (
        "/api/services/media_player/media_next_track",
        {"entity_id": "media_player.basement_speaker_1_2"},
    )
    assert "The Summoning" in out_skip

    # Volume up uses active speaker (+10% from 40% -> 50%)
    out_vol_up = c.music("turn it up")
    assert fake.posts[1] == (
        "/api/services/media_player/volume_set",
        {"entity_id": "media_player.basement_speaker_1_2", "volume_level": 0.5},
    )
    assert "50%" in out_vol_up

    # Explicit volume in specific room
    out_vol_set = c.music("set volume to 35 in the kitchen")
    assert fake.posts[2] == (
        "/api/services/media_player/volume_set",
        {"entity_id": "media_player.kitchen_display_2", "volume_level": 0.35},
    )
    assert "35%" in out_vol_set

    # Resume and mute
    out_mute = c.music("mute the kitchen")
    assert fake.posts[3] == (
        "/api/services/media_player/volume_mute",
        {"entity_id": "media_player.kitchen_display_2", "is_volume_muted": True},
    )
    assert "Muted" in out_mute

    out_resume = c.music("resume in the kitchen")
    assert fake.posts[4] == (
        "/api/services/media_player/media_play",
        {"entity_id": "media_player.kitchen_display_2"},
    )
    assert "Resumed" in out_resume


def test_weather_feels_like_gusts_and_hourly(monkeypatch):
    import datetime

    c = _client()
    monkeypatch.setattr(
        c,
        "_get",
        lambda path: {
            "state": "cloudy",
            "attributes": {
                "temperature": 41,
                "temperature_unit": "°F",
                "precipitation_unit": "in",
                "wind_speed": 7.5,
                "wind_speed_unit": "mph",
            },
        },
    )

    now = datetime.datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    target_5pm = now.replace(hour=17)
    if target_5pm < now:
        target_5pm += datetime.timedelta(days=1)

    def fake_post(path, payload):
        if payload.get("type") == "hourly":
            return {
                "service_response": {
                    "weather.home": {
                        "forecast": [
                            {
                                "datetime": now.isoformat(),
                                "condition": "cloudy",
                                "temperature": 41,
                                "apparent_temperature": 34,
                                "wind_gust_speed": 15,
                                "precipitation": 0.0,
                            },
                            {
                                "datetime": target_5pm.isoformat(),
                                "condition": "rainy",
                                "temperature": 39,
                                "apparent_temperature": 31,
                                "wind_gust_speed": 28,
                                "precipitation": 0.15,
                            },
                        ]
                    }
                }
            }
        return {
            "service_response": {
                "weather.home": {
                    "forecast": [
                        {
                            "datetime": "2026-11-25T00:00:00+00:00",
                            "condition": "cloudy",
                            "temperature": 41,
                            "apparent_temperature": 34,
                            "templow": 25,
                            "wind_gust_speed": 18,
                            "precipitation": 0.0,
                        },
                        {
                            "datetime": "2026-11-26T00:00:00+00:00",
                            "condition": "pouring",
                            "temperature": 52,
                            "templow": 39,
                            "wind_gust_speed": 50,
                            "precipitation": 3.97,
                        },
                    ]
                }
            }
        }

    monkeypatch.setattr(c, "_post", fake_post)

    # Daily includes feels-like (34 vs 41) and gusts (50 mph)
    daily_out = c.weather("what is the weather")
    assert "feels like 34°F" in daily_out
    assert "gusts to 50 mph" in daily_out

    # Specific hour query uses hourly forecast
    hour_out = c.weather("weather at 5pm")
    assert "5 PM" in hour_out and "39°F" in hour_out and "0.15 in rain" in hour_out

    # Umbrella / rain query scans hourly window
    rain_out = c.weather("do I need an umbrella tonight")
    assert "rainy starting around" in rain_out and "gusts to 28 mph" in rain_out
