import pytest

from pebble_index_mcp.homeassistant import HAError, HAClient, parse_command


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
        _state("light.jason_s_lamp", "Jason's lamp"),
        _state("media_player.living_room_tv_2", "Living Room TV", "paused"),
        _state("switch.home_assistant_voice_09f284_mute", "Home Assistant Voice Mute"),
    ]


def _client():
    return HAClient(url="http://ha.test:8123", token="ha-token")


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
    # "jason's" must tokenize as jason/s, not a dead "jason's" token that
    # singularize() mangles into "jason'"
    action, target = parse_command("turn on Jason's lamp")
    assert action == "on"
    assert target == ["jason", "lamp"]


def test_resolve_possessive_entity():
    entity = _resolve(_client(), ["jason", "lamp"])
    assert entity["entity_id"] == "light.jason_s_lamp"


def test_control_possessive_end_to_end(monkeypatch):
    c = _client()
    fake = FakeHA(_states())
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    out = c.control("turn on Jason's lamp")
    assert fake.posts[0][1] == {"entity_id": "light.jason_s_lamp"}
    assert "now on" in out


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


# --- music (Music Assistant via HA) -----------------------------------------

def _mu_state(entity_id, state="idle", **attrs):
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def _music_states():
    return [
        _mu_state("media_player.home_assistant_voice_09f284"),
        _mu_state("media_player.basement_speaker_1_2"),
        _mu_state("media_player.clarity_s_vinyl_speaker_2"),
        _mu_state("media_player.kitchen_display_2"),
        _mu_state("media_player.sydney_s_room_speaker_2"),
        _mu_state("media_player.master_bedroom_speaker_2", "unavailable"),
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
    assert payload["entity_id"] == "media_player.home_assistant_voice_09f284"
    assert payload["media_content_type"] == "artist"
    assert payload["media_content_id"] == "spiritbox"
    assert "Circle With Me" in out and "Spiritbox" in out


def test_music_room_routing_and_possessive(monkeypatch):
    c = _client()
    fake = FakeHA(_music_states())
    fake.play_queue = [{"media_title": "Casanova Fluid Hair"}]
    monkeypatch.setattr(c, "_get_raw", fake.get)
    monkeypatch.setattr(c, "_post_raw", fake.post)
    c.music("play Bilmuri in the basement")
    assert fake.posts[0][1]["entity_id"] == "media_player.basement_speaker_1_2"
    c.music("play Bilmuri in Sydney's room")
    assert fake.posts[-1][1]["entity_id"] == "media_player.sydney_s_room_speaker_2"


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
    c = _client()
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
