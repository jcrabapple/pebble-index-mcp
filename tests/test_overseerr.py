import pytest

from pebble_index_mcp.overseerr import OverseerrClient, OverseerrError


def _client():
    return OverseerrClient(url="https://overseerr.test", api_key="api-key")


def _movie(tmdb_id=693134, title="Dune: Part Two", year="2024", status=None, pop=80.0):
    d = {
        "id": tmdb_id,
        "mediaType": "movie",
        "title": title,
        "releaseDate": f"{year}-03-01",
        "popularity": pop,
    }
    if status is not None:
        d["mediaInfo"] = {"status": status}
    return d


def _tv(tmdb_id=95396, name="Severance", year="2022", status=None, pop=90.0):
    d = {
        "id": tmdb_id,
        "mediaType": "tv",
        "name": name,
        "firstAirDate": f"{year}-02-18",
        "popularity": pop,
    }
    if status is not None:
        d["mediaInfo"] = {"status": status}
    return d


def test_missing_config_raises():
    c = OverseerrClient(url="", api_key="")
    with pytest.raises(OverseerrError, match="OVERSEERR_URL"):
        c.status("Severance")


def test_search_uses_percent20_encoding(monkeypatch):
    c = _client()
    paths = []

    def fake_get(path):
        paths.append(path)
        return 200, {"results": [_movie(title="Dune: Part Two", status=5)]}

    monkeypatch.setattr(c, "_get_raw", fake_get)
    out = c.status("Dune Part Two")
    assert paths[0] == "/api/v1/search?query=Dune%20Part%20Two"
    assert "Dune: Part Two (2024, movie): available on Plex." == out


def test_request_movie_success(monkeypatch):
    c = _client()
    posts = []
    monkeypatch.setattr(c, "_get_raw", lambda path: (200, {"results": [_movie(title="Sinners", year="2025")]}))
    monkeypatch.setattr(c, "_post_raw", lambda path, payload: posts.append((path, payload)) or (201, {"id": 10}))
    out = c.request_media("download the movie Sinners to Plex")
    assert posts == [("/api/v1/request", {"mediaType": "movie", "mediaId": 693134})]
    assert out == "Requested Sinners (2025) for Plex."


def test_request_movie_already_available_does_not_post(monkeypatch):
    c = _client()
    posts = []
    monkeypatch.setattr(c, "_get_raw", lambda path: (200, {"results": [_movie(status=5)]}))
    monkeypatch.setattr(c, "_post_raw", lambda path, payload: posts.append((path, payload)) or (201, {}))
    out = c.request_media("Dune: Part Two")
    assert posts == []
    assert "already available on Plex" in out


def test_request_disambiguates_same_title_movie_and_tv(monkeypatch):
    c = _client()
    posts = []
    monkeypatch.setattr(
        c,
        "_get_raw",
        lambda path: (200, {"results": [_tv(name="Severance", year="2022"), _movie(tmdb_id=5072, title="Severance", year="2006")]}),
    )
    monkeypatch.setattr(c, "_post_raw", lambda path, payload: posts.append((path, payload)) or (201, {}))
    out = c.request_media("Severance")
    assert posts == []
    assert "movie or TV show" in out


def test_request_tv_requests_only_missing_aired_seasons(monkeypatch):
    c = _client()
    posts = []

    def fake_get(path):
        if path.startswith("/api/v1/search"):
            return 200, {"results": [_tv(tmdb_id=95396, name="Severance", year="2022", status=4)]}
        if path == "/api/v1/tv/95396":
            return 200, {
                "name": "Severance",
                "externalIds": {"tvdbId": 371980},
                "seasons": [
                    {"seasonNumber": 0, "episodeCount": 1},
                    {"seasonNumber": 1, "episodeCount": 9},
                    {"seasonNumber": 2, "episodeCount": 10},
                    {"seasonNumber": 3, "episodeCount": 0},  # unaired stub
                ],
                "mediaInfo": {
                    "status": 4,
                    "seasons": [
                        {"seasonNumber": 1, "status": 5},
                        {"seasonNumber": 2, "status": 1},
                    ],
                },
            }
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(c, "_get_raw", fake_get)
    monkeypatch.setattr(c, "_post_raw", lambda path, payload: posts.append((path, payload)) or (201, {}))
    out = c.request_media("request the TV show Severance")
    assert posts == [
        (
            "/api/v1/request",
            {"mediaType": "tv", "mediaId": 95396, "tvdbId": 371980, "seasons": [2]},
        )
    ]
    assert out == "Requested Severance (2022) (season 2) for Plex."


def test_request_tv_specific_season_already_available(monkeypatch):
    c = _client()
    posts = []

    def fake_get(path):
        if path.startswith("/api/v1/search"):
            return 200, {"results": [_tv(tmdb_id=95396, name="Severance", status=4)]}
        return 200, {
            "name": "Severance",
            "externalIds": {"tvdbId": 371980},
            "seasons": [{"seasonNumber": 1, "episodeCount": 9}, {"seasonNumber": 2, "episodeCount": 10}],
            "mediaInfo": {"status": 4, "seasons": [{"seasonNumber": 1, "status": 5}]},
        }

    monkeypatch.setattr(c, "_get_raw", fake_get)
    monkeypatch.setattr(c, "_post_raw", lambda path, payload: posts.append((path, payload)) or (201, {}))
    out = c.request_media("season 1 of Severance")
    assert posts == []
    assert "season 1 is already on Plex" in out


def test_recent_requests_resolves_titles(monkeypatch):
    c = _client()

    def fake_get(path):
        if path.startswith("/api/v1/request"):
            return 200, {
                "results": [
                    {"id": 1, "type": "movie", "status": 2, "media": {"tmdbId": 10, "status": 3}},
                    {"id": 2, "type": "tv", "status": 5, "media": {"tmdbId": 20, "status": 5}},
                ]
            }
        if path == "/api/v1/movie/10":
            return 200, {"title": "Sinners"}
        if path == "/api/v1/tv/20":
            return 200, {"name": "Severance"}
        raise AssertionError(path)

    monkeypatch.setattr(c, "_get_raw", fake_get)
    out = c.recent_requests()
    assert out == "Recent Plex requests: Sinners (downloading); Severance (available on Plex)."
