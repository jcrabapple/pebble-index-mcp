"""Overseerr client for plex_media (Plex availability checks and download requests).

Talks to the operator's Overseerr instance (OVERSEERR_URL + OVERSEERR_API_KEY).
Design rules for safe voice use:
- Multi-word search queries are percent-encoded with %20 (urllib.parse.quote),
  never '+' form encoding, which Overseerr rejects with 400.
- Never re-requests media that is already available or processing on Plex.
- When a movie and TV show share the exact same title and the user did not
  specify movie vs TV (or a season), asks to clarify rather than downloading
  the wrong title.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

import httpx

_TIMEOUT = 15.0
_MAX_HITS = 3
_MAX_RECENT = 5

_STATUS_LABELS = {
    1: "not on Plex",
    2: "pending approval",
    3: "downloading",
    4: "partially on Plex",
    5: "available on Plex",
    6: "not on Plex",
}

_REQUEST_STATUS_LABELS = {
    1: "pending",
    2: "approved",
    3: "declined",
    4: "failed",
    5: "completed",
}

_MEDIA_STATUS_ALREADY = {2, 3, 5}


class OverseerrError(Exception):
    pass


def _norm_title(text: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _parse_query_hints(
    query: str,
    media_type: str = "",
    season: int | None = None,
) -> tuple[str, str, int | None, str]:
    """Extract (clean_title, media_type, season, year) from spoken query + explicit args."""
    q = (query or "").strip()
    mt = (media_type or "").strip().lower()
    if mt in ("film", "movies"):
        mt = "movie"
    elif mt in ("show", "series", "tv show", "television"):
        mt = "tv"
    elif mt not in ("movie", "tv"):
        mt = ""

    # Strip leading conversational request/check wrappers
    q = re.sub(
        r"^(?:please\s+)?(?:download|request|add|get|grab|put|check|search(?:\s+for)?|find)\s+",
        "",
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(r"\s+(?:on|to|for|in)\s+(?:my\s+|the\s+)?(?:plex|overseerr)\s*[?.!]*$", "", q, flags=re.IGNORECASE)

    # Extract explicit season: "season 2 of Severance" or "Severance season 2"
    if season is None:
        m_pre = re.match(r"^(?:the\s+)?season\s+(\d{1,2})\s+of\s+(.+)$", q, flags=re.IGNORECASE)
        if m_pre:
            season = int(m_pre.group(1))
            q = m_pre.group(2).strip()
        else:
            m_post = re.search(r"\bseason\s+(\d{1,2})\b", q, flags=re.IGNORECASE)
            if m_post:
                season = int(m_post.group(1))
                q = (q[: m_post.start()] + " " + q[m_post.end():]).strip()

    if season is not None and not mt:
        mt = "tv"

    # Infer movie/tv hint from query if not explicit
    if not mt:
        if re.search(r"\b(?:the\s+)?(?:movie|film)\b", q, flags=re.IGNORECASE):
            mt = "movie"
        elif re.search(r"\b(?:the\s+)?(?:tv\s+show|tv\s+series|show|series)\b", q, flags=re.IGNORECASE):
            mt = "tv"

    q = re.sub(r"\b(?:the\s+)?(?:tv\s+show|tv\s+series|movie|film|show|series)\b", " ", q, flags=re.IGNORECASE)

    # Optional 4-digit year (only strip if there are other title words so "1917" stays intact)
    year = ""
    m_year = re.search(r"\b((?:19|20)\d{2})\b", q)
    if m_year:
        remainder = (q[: m_year.start()] + " " + q[m_year.end():]).strip(" ()-,")
        if remainder:
            year = m_year.group(1)
            q = remainder

    q = re.sub(r"\s+", " ", q).strip(" .,?!\"'")
    return q, mt, season, year


class OverseerrClient:
    def __init__(self, url: str = "", api_key: str = "", timeout: float = _TIMEOUT):
        self.url = url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def _require_config(self) -> None:
        if not self.url or not self.api_key:
            raise OverseerrError("Plex requests unavailable: OVERSEERR_URL or OVERSEERR_API_KEY not configured.")

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "User-Agent": "pebble-index-mcp/1.0",
        }

    def _get_raw(self, path: str) -> tuple[int, Any]:
        try:
            r = self._client.get(
                f"{self.url}{path}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise OverseerrError(f"Overseerr timeout: {e}") from e
        except httpx.TransportError as e:
            raise OverseerrError(f"Overseerr transport error: {e}") from e
        return r.status_code, _safe_json(r)

    def _post_raw(self, path: str, payload: dict) -> tuple[int, Any]:
        try:
            r = self._client.post(
                f"{self.url}{path}",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as e:
            raise OverseerrError(f"Overseerr timeout: {e}") from e
        except httpx.TransportError as e:
            raise OverseerrError(f"Overseerr transport error: {e}") from e
        return r.status_code, _safe_json(r)

    def _get(self, path: str) -> Any:
        code, body = self._get_raw(path)
        if code != 200:
            msg = body.get("message", "") if isinstance(body, dict) else str(body)[:120]
            raise OverseerrError(f"Overseerr HTTP {code}: {msg}".strip(": "))
        return body

    # -- search helpers ------------------------------------------------------

    def _search_media(self, title: str, media_type: str = "", year: str = "") -> list[dict]:
        # Overseerr requires %20-encoded spaces; '+' form encoding returns 400.
        encoded = urllib.parse.quote(title, safe="")
        data = self._get(f"/api/v1/search?query={encoded}")
        raw = data.get("results", []) if isinstance(data, dict) else []
        items = [r for r in raw if r.get("mediaType") in ("movie", "tv")]
        if media_type:
            typed = [r for r in items if r.get("mediaType") == media_type]
            if typed:
                items = typed
        if year:
            by_year = [r for r in items if _item_year(r) == year]
            if by_year:
                items = by_year
        target_norm = _norm_title(title)
        items.sort(
            key=lambda r: (
                _norm_title(_item_title(r)) == target_norm,
                target_norm in _norm_title(_item_title(r)),
                float(r.get("popularity") or 0.0),
            ),
            reverse=True,
        )
        return items

    # -- public actions ------------------------------------------------------

    def status(self, query: str, media_type: str = "") -> str:
        """Check whether a movie or TV show is available/requested on Plex."""
        self._require_config()
        title, mt, _, year = _parse_query_hints(query, media_type=media_type)
        if not title:
            raise OverseerrError("Name a movie or TV show to check.")
        items = self._search_media(title, media_type=mt, year=year)
        if not items:
            return f"No movies or TV shows found matching '{title}'."
        lines = []
        for item in items[:_MAX_HITS]:
            mi = item.get("mediaInfo") or {}
            raw_st = mi.get("status")
            st = _STATUS_LABELS.get(int(raw_st), "not on Plex") if isinstance(raw_st, int) else "not on Plex"
            lines.append(f"{_format_item(item)}: {st}")
        return "; ".join(lines) + "."

    def request_media(
        self,
        query: str,
        media_type: str = "",
        season: int | None = None,
    ) -> str:
        """Search Overseerr and request a movie or TV show for Plex."""
        self._require_config()
        title, mt, req_season, year = _parse_query_hints(query, media_type=media_type, season=season)
        if not title:
            raise OverseerrError("Name a movie or TV show to request.")
        items = self._search_media(title, media_type=mt, year=year)
        if not items:
            return f"Could not find '{title}' in Overseerr."

        # If both a movie and TV show have the exact same title and caller did
        # not specify movie vs TV (or a season), ask to clarify instead of guessing.
        target_norm = _norm_title(title)
        if not mt and not year and len(items) > 1:
            exact = [r for r in items if _norm_title(_item_title(r)) == target_norm]
            exact_types = {r.get("mediaType") for r in exact}
            if len(exact_types) > 1:
                opts = " and ".join(_format_item(r) for r in exact[:2])
                return f"Found {opts}. Say movie or TV show to request the right one."

        chosen = items[0]
        chosen_type = chosen.get("mediaType")
        tmdb_id = int(chosen["id"])
        label = _format_item(chosen, include_type=False)
        mi = chosen.get("mediaInfo") or {}
        cur_status = mi.get("status")

        if chosen_type == "movie":
            if cur_status == 5:
                return f"{label} is already available on Plex."
            if cur_status in (2, 3):
                return f"{label} is already requested ({_STATUS_LABELS[cur_status]})."
            return self._submit_request(
                {"mediaType": "movie", "mediaId": tmdb_id},
                success_msg=f"Requested {label} for Plex.",
                label=label,
            )

        # TV show: inspect seasons + TVDB ID so we never re-request existing seasons
        details = self._get(f"/api/v1/tv/{tmdb_id}")
        ext = details.get("externalIds") or {}
        tvdb_id = int(ext.get("tvdbId") or tmdb_id)
        all_seasons = [
            s for s in (details.get("seasons") or [])
            if isinstance(s, dict) and (s.get("seasonNumber") or 0) > 0
        ]
        aired_seasons = [
            int(s["seasonNumber"]) for s in all_seasons
            if s.get("episodeCount") is None or int(s.get("episodeCount") or 0) > 0
        ]
        valid_seasons = aired_seasons or [int(s["seasonNumber"]) for s in all_seasons]
        if not valid_seasons:
            return f"No requestable seasons found for {label}."

        det_mi = details.get("mediaInfo") or mi or {}
        season_status = {
            int(s["seasonNumber"]): s.get("status")
            for s in (det_mi.get("seasons") or [])
            if isinstance(s, dict) and s.get("seasonNumber") is not None
        }

        if req_season is not None:
            all_nums = {int(s["seasonNumber"]) for s in all_seasons}
            if req_season not in all_nums:
                return f"{label} does not have season {req_season} (available: {min(valid_seasons)}-{max(valid_seasons)})."
            st = season_status.get(req_season)
            if st == 5:
                return f"{label} season {req_season} is already on Plex."
            if st in (2, 3):
                return f"{label} season {req_season} is already requested ({_STATUS_LABELS[st]})."
            target_seasons = [req_season]
            season_desc = f"season {req_season}"
        else:
            if det_mi.get("status") == 5:
                return f"{label} is already available on Plex."
            missing = [n for n in valid_seasons if season_status.get(n) not in _MEDIA_STATUS_ALREADY]
            if not missing:
                return f"All seasons of {label} are already on Plex or requested."
            target_seasons = missing
            season_desc = f"season {missing[0]}" if len(missing) == 1 else f"{len(missing)} seasons"

        return self._submit_request(
            {
                "mediaType": "tv",
                "mediaId": tmdb_id,
                "tvdbId": tvdb_id,
                "seasons": target_seasons,
            },
            success_msg=f"Requested {label} ({season_desc}) for Plex.",
            label=label,
        )

    def _submit_request(self, payload: dict, *, success_msg: str, label: str) -> str:
        code, body = self._post_raw("/api/v1/request", payload)
        if code in (200, 201):
            return success_msg
        msg = body.get("message", "") if isinstance(body, dict) else str(body)[:120]
        if code in (400, 409) and "already" in msg.lower():
            return f"{label} is already requested."
        raise OverseerrError(f"Could not request {label}: HTTP {code} {msg}".strip())

    def recent_requests(self, limit: int = _MAX_RECENT) -> str:
        """List recent Plex media requests with resolved titles."""
        self._require_config()
        take = max(1, min(int(limit), 10))
        data = self._get(f"/api/v1/request?take={take}&sort=added")
        rows = data.get("results", []) if isinstance(data, dict) else []
        if not rows:
            return "No recent Plex requests."
        lines = []
        for req in rows[:take]:
            mtype = req.get("type") or (req.get("media") or {}).get("mediaType") or "movie"
            media = req.get("media") or {}
            tmdb_id = media.get("tmdbId")
            title = f"TMDB {tmdb_id}"
            if tmdb_id:
                try:
                    det = self._get(f"/api/v1/{mtype}/{tmdb_id}")
                    title = det.get("title") or det.get("name") or title
                except OverseerrError:
                    pass
            m_status = media.get("status")
            r_status = req.get("status")
            st_label = (
                _STATUS_LABELS.get(m_status)
                if m_status in (2, 3, 4, 5)
                else _REQUEST_STATUS_LABELS.get(r_status, "requested")
            )
            lines.append(f"{title} ({st_label})")
        return "Recent Plex requests: " + "; ".join(lines) + "."


def _item_title(item: dict) -> str:
    return str(item.get("title") or item.get("name") or "?").strip()


def _item_year(item: dict) -> str:
    raw = str(item.get("releaseDate") or item.get("firstAirDate") or "")
    return raw[:4] if len(raw) >= 4 else ""


def _format_item(item: dict, *, include_type: bool = True) -> str:
    title = _item_title(item)
    year = _item_year(item)
    kind = "TV" if item.get("mediaType") == "tv" else "movie"
    bits = [b for b in (year, kind if include_type else "") if b]
    return f"{title} ({', '.join(bits)})" if bits else title


def _safe_json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:200]
