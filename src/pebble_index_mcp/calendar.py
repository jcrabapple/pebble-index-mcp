"""Local Fastmail/CalDAV calendar lookup via the khal mirror."""
from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

_TIMEOUT = 10.0
_MAX_DAYS = 5
_MAX_SEARCH_HITS = 5
_DEFAULT_STALE_MIN = 20.0
_SYNC_TIMEOUT_S = 15.0

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_TIME_RANGE_RE = re.compile(r"\b(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\b")
_TIME_ARROW_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?:→|->)")


def _fmt_12h(hour: int, minute: int, include_meridiem: bool = True) -> tuple[str, str]:
    meridiem = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    t = f"{h12}:{minute:02d}"
    return (f"{t} {meridiem}" if include_meridiem else t), meridiem


def _convert_times_to_12h(text: str) -> str:
    def repl_range(m: re.Match) -> str:
        h1, m1, h2, m2 = (int(m.group(i)) for i in range(1, 5))
        if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
            return m.group(0)
        s1, mer1 = _fmt_12h(h1, m1, include_meridiem=False)
        s2, mer2 = _fmt_12h(h2, m2, include_meridiem=True)
        if mer1 == mer2:
            return f"{s1}-{s2}"
        return f"{s1} {mer1}-{s2}"

    def repl_arrow(m: re.Match) -> str:
        h1, m1 = int(m.group(1)), int(m.group(2))
        if not (0 <= h1 <= 23 and 0 <= m1 <= 59):
            return m.group(0)
        s1, _ = _fmt_12h(h1, m1, include_meridiem=True)
        return s1

    text = _TIME_RANGE_RE.sub(repl_range, text)
    text = _TIME_ARROW_RE.sub(repl_arrow, text)
    return text

_SCHEDULE_WORDS = frozenset(
    """what whats is are on my the our calendar schedule agenda today tomorrow
    this next week weekend upcoming free busy do i we have anything plans
    events appointments for""".split()
)

_SEARCH_FILLER = frozenset(
    """when what where is are was were my the our a an next on in at to for
    from calendar schedule agenda search find lookup look up do does did i we
    have got event appointment""".split()
)


class CalendarError(Exception):
    pass


def _default_khal_bin() -> str:
    env_bin = os.environ.get("KHAL_BIN", "").strip()
    if env_bin:
        return env_bin
    local_bin = Path.home() / ".local" / "bin" / "khal"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return shutil.which("khal") or "khal"


def _default_vdirsyncer_bin() -> str:
    env_bin = os.environ.get("VDIRSYNCER_BIN", "").strip()
    if env_bin:
        return env_bin
    local_bin = Path.home() / ".local" / "bin" / "vdirsyncer"
    if local_bin.is_file() and os.access(local_bin, os.X_OK):
        return str(local_bin)
    return shutil.which("vdirsyncer") or ""


class CalendarClient:
    def __init__(
        self,
        khal_bin: str = "",
        timeout: float = _TIMEOUT,
        store_dir: str | Path | None = None,
        vdirsyncer_bin: str = "",
        stale_minutes: float = _DEFAULT_STALE_MIN,
        auto_sync: bool = True,
    ):
        self.khal_bin = khal_bin or _default_khal_bin()
        self.timeout = timeout
        if store_dir is not None:
            self.store_dir: Path | None = Path(store_dir)
        else:
            env_store = os.environ.get("CALENDAR_STORE", "").strip()
            default_store = Path.home() / ".local" / "share" / "oscar" / "calendars"
            self.store_dir = Path(env_store).expanduser() if env_store else (default_store if default_store.is_dir() else None)
        self.vdirsyncer_bin = vdirsyncer_bin or _default_vdirsyncer_bin()
        self.stale_minutes = stale_minutes
        self.auto_sync = auto_sync and os.environ.get("CALENDAR_AUTO_SYNC", "1").strip() not in ("0", "false", "no")

    def _mirror_age_minutes(self) -> float:
        if not self.store_dir or not self.store_dir.is_dir():
            return 0.0
        newest = 0.0
        try:
            for path in self.store_dir.rglob("*.ics"):
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            return 0.0
        if not newest:
            return float("inf")
        return (time.time() - newest) / 60.0

    def _sync_if_stale(self) -> None:
        if not self.auto_sync or not self.vdirsyncer_bin or not self.store_dir:
            return
        if self._mirror_age_minutes() < self.stale_minutes:
            return
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        try:
            subprocess.run(
                [self.vdirsyncer_bin, "sync"],
                capture_output=True,
                timeout=_SYNC_TIMEOUT_S,
                env=env,
            )
        except (subprocess.SubprocessError, OSError):
            pass  # cached events are better than failing the turn

    def _run(self, args: list[str]) -> str:
        self._sync_if_stale()
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        try:
            proc = subprocess.run(
                [self.khal_bin, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except FileNotFoundError as e:
            raise CalendarError("Calendar unavailable: khal is not installed.") from e
        except subprocess.TimeoutExpired as e:
            raise CalendarError("Calendar lookup timed out.") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:160]
            raise CalendarError(f"Calendar error: {err or f'exit {proc.returncode}'}")
        return proc.stdout

    @staticmethod
    def _today() -> datetime.date:
        return datetime.datetime.now().astimezone().date()

    @staticmethod
    def _clean_event_line(line: str) -> str:
        # Strip khal's recurring/alarm/range glyphs and format 24h times to 12h
        cleaned = re.sub(r"[⟳⏰↦↔⇥]", "", line)
        cleaned = _convert_times_to_12h(cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    _DAY_HDR_RE = re.compile(r"^(?:DAY:)?([A-Za-z]+,\s+\d{4}-\d{2}-\d{2}):?$")

    @classmethod
    def _format_list_output(cls, raw: str, empty_label: str) -> str:
        days: list[tuple[str, list[str]]] = []
        current_day: str | None = None
        current_events: list[str] = []

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m_day = cls._DAY_HDR_RE.match(stripped)
            if m_day:
                if current_day is not None and current_events:
                    days.append((current_day, current_events))
                current_day = m_day.group(1)
                current_events = []
            else:
                ev = cls._clean_event_line(stripped)
                if ev and ev not in current_events:
                    current_events.append(ev)

        if current_day is not None and current_events:
            days.append((current_day, current_events))

        if not days:
            return f"Nothing on the calendar for {empty_label}."

        parts = []
        for day_hdr, events in days[:_MAX_DAYS]:
            parts.append(f"{day_hdr}: {', '.join(events[:5])}")
        return ". ".join(parts) + "."

    def _list_range(self, start: str, span: str, label: str) -> str:
        raw = self._run(
            [
                "list",
                "--day-format",
                "DAY:{name}, {date}",
                "--format",
                "{start-end-time-style} {title}",
                start,
                span,
            ]
        )
        return self._format_list_output(raw, label)

    def _search(self, terms: list[str]) -> str:
        query_str = " ".join(terms)
        raw = self._run(
            [
                "search",
                "--format",
                "{start-date} {start-end-time-style}: {title}",
                query_str,
            ]
        )
        today_iso = self._today().isoformat()
        upcoming: list[str] = []
        past: list[str] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            cleaned = self._clean_event_line(line).replace(" : ", ": ")
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            date_prefix = cleaned[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date_prefix) and date_prefix < today_iso:
                past.append(cleaned)
            else:
                upcoming.append(cleaned)
        upcoming.sort(key=lambda s: s[:16])
        past.sort(key=lambda s: s[:16])
        hits = upcoming or past[-_MAX_SEARCH_HITS:]
        if not hits:
            return f"No calendar events matching '{query_str}'."
        return "; ".join(hits[:_MAX_SEARCH_HITS]) + "."

    @classmethod
    def _parse_explicit_date(cls, text: str, today: datetime.date) -> datetime.date | None:
        m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m_iso:
            try:
                return datetime.date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
            except ValueError:
                return None
        month_pat = "|".join(_MONTHS.keys())
        m_md = re.search(rf"\b({month_pat})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{{4}}))?\b", text)
        if m_md:
            month = _MONTHS[m_md.group(1)]
            day = int(m_md.group(2))
            year = int(m_md.group(3)) if m_md.group(3) else today.year
            try:
                candidate = datetime.date(year, month, day)
            except ValueError:
                return None
            if not m_md.group(3) and candidate < today:
                try:
                    candidate = datetime.date(year + 1, month, day)
                except ValueError:
                    pass
            return candidate
        return None

    def lookup(self, query: str = "") -> str:
        """Answer a schedule or event search query from the local khal mirror."""
        low = query.lower().strip()
        words = re.sub(r"[^a-z0-9 ]+", " ", low).split()
        word_set = set(words)
        today = self._today()

        if "day after tomorrow" in low:
            target = today + datetime.timedelta(days=2)
            return self._list_range(target.isoformat(), "1d", target.strftime("%A, %b %-d"))

        m_in_days = re.search(r"\bin\s+(\d+)\s+days?\b", low)
        if m_in_days:
            n = int(m_in_days.group(1))
            target = today + datetime.timedelta(days=n)
            return self._list_range(target.isoformat(), "1d", target.strftime("%A, %b %-d"))

        explicit = self._parse_explicit_date(low, today)
        if explicit is not None:
            return self._list_range(explicit.isoformat(), "1d", explicit.strftime("%b %-d"))

        if "tomorrow" in word_set:
            return self._list_range("tomorrow", "1d", "tomorrow")

        if "next week" in low:
            days_until_mon = (7 - today.weekday()) % 7 or 7
            start = today + datetime.timedelta(days=days_until_mon)
            return self._list_range(start.isoformat(), "7d", "next week")

        if "weekend" in word_set:
            days_until_sat = (5 - today.weekday()) % 7
            if today.weekday() == 6:  # Sunday
                sat = today
                return self._list_range(sat.isoformat(), "1d", "this weekend")
            sat = today + datetime.timedelta(days=days_until_sat)
            return self._list_range(sat.isoformat(), "2d", "this weekend")

        for day_name, wd in _WEEKDAYS.items():
            if day_name in word_set:
                delta = (wd - today.weekday()) % 7
                if delta == 0 and f"next {day_name}" in low:
                    delta = 7
                target = today + datetime.timedelta(days=delta)
                label = f"next {day_name.capitalize()}" if delta == 7 else day_name.capitalize()
                return self._list_range(target.isoformat(), "1d", label)

        if "today" in word_set or "tonight" in word_set:
            return self._list_range("today", "1d", "today")

        # Check if there are non-schedule search terms (e.g. "flea market", "dentist")
        search_terms = [w for w in words if w not in _SEARCH_FILLER and w not in _SCHEDULE_WORDS]
        if search_terms:
            return self._search(search_terms)

        return self._list_range("today", "7d", "the next 7 days")
