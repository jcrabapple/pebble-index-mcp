import datetime
import pytest

from pebble_index_mcp.calendar import CalendarClient, CalendarError


def test_calendar_list_formats_days_and_strips_glyphs(monkeypatch):
    c = CalendarClient(khal_bin="/bin/false")
    # Note: timed events ("16:00-17:00 GSA Meeting") and multi-day markers
    # ("↦ Kids weekend") have no leading space in khal output, while all-day
    # events (" Kids here") do.
    sample = (
        "DAY:Tomorrow, 2026-11-10\n"
        " Kids here ⟳\n"
        "16:00-17:00 GSA Meeting ⏰\n"
        "DAY:Thursday, 2026-11-12\n"
        "↦ Kids weekend ⟳\n"
    )
    calls = []

    def fake_run(args):
        calls.append(args)
        return sample

    monkeypatch.setattr(c, "_run", fake_run)
    out = c.lookup("what is on my calendar this week")
    assert calls[0][0] == "list"
    assert calls[0][-2:] == ["today", "7d"]
    assert out == "Tomorrow, 2026-11-10: Kids here, 4:00-5:00 PM GSA Meeting. Thursday, 2026-11-12: Kids weekend."


def test_calendar_today_empty(monkeypatch):
    c = CalendarClient(khal_bin="/bin/false")
    monkeypatch.setattr(c, "_run", lambda args: "")
    out = c.lookup("what do I have today")
    assert out == "Nothing on the calendar for today."


def test_calendar_weekday_routing(monkeypatch):
    c = CalendarClient(khal_bin="/bin/false")
    monkeypatch.setattr(c, "_today", lambda: datetime.date(2026, 11, 9))  # Monday
    calls = []
    monkeypatch.setattr(c, "_run", lambda args: calls.append(args) or "")
    out = c.lookup("am I free Thursday")
    assert calls[0][-2:] == ["2026-11-12", "1d"]
    assert out == "Nothing on the calendar for Thursday."


def test_calendar_next_same_weekday_and_next_week(monkeypatch):
    c = CalendarClient(khal_bin="/bin/false")
    monkeypatch.setattr(c, "_today", lambda: datetime.date(2026, 11, 12))  # Thursday
    calls = []
    monkeypatch.setattr(c, "_run", lambda args: calls.append(args) or "")
    c.lookup("what is on next Thursday")
    assert calls[0][-2:] == ["2026-11-19", "1d"]

    c.lookup("what do I have next week")
    assert calls[1][-2:] == ["2026-11-16", "7d"]


def test_calendar_explicit_date_and_relative_days(monkeypatch):
    c = CalendarClient(khal_bin="/bin/false")
    monkeypatch.setattr(c, "_today", lambda: datetime.date(2026, 11, 25))
    calls = []
    monkeypatch.setattr(c, "_run", lambda args: calls.append(args) or "")
    c.lookup("what is on my calendar December 5th")
    assert calls[0][-2:] == ["2026-12-05", "1d"]

    c.lookup("what is happening in 3 days")
    assert calls[1][-2:] == ["2026-11-28", "1d"]

    c.lookup("day after tomorrow")
    assert calls[2][-2:] == ["2026-11-27", "1d"]


def test_calendar_search_filters_past_and_sorts_chronologically(monkeypatch):
    c = CalendarClient(khal_bin="/bin/false")
    monkeypatch.setattr(c, "_today", lambda: datetime.date(2026, 11, 9))
    raw = (
        "2017-05-19 : Lucketts store vintage flea market\n"
        "2026-12-05 14:00→: Winter Flea Market\n"
        "2026-11-22 10:00→: DC Big Flea Market\n"
    )
    calls = []
    monkeypatch.setattr(c, "_run", lambda args: calls.append(args) or raw)
    out = c.lookup("when is the flea market")
    assert calls[0][0] == "search"
    assert calls[0][-1] == "flea market"
    assert out == "2026-11-22 10:00 AM: DC Big Flea Market; 2026-12-05 2:00 PM: Winter Flea Market."


def test_calendar_missing_binary_raises():
    c = CalendarClient(khal_bin="/nonexistent/khal-binary", auto_sync=False)
    with pytest.raises(CalendarError):
        c.lookup("today")


def test_calendar_syncs_when_mirror_stale(monkeypatch, tmp_path):
    import os
    import time

    ics = tmp_path / "ev.ics"
    ics.write_text("BEGIN:VCALENDAR\nEND:VCALENDAR\n")
    old_ts = time.time() - 3600  # 60 min old > 20 min threshold
    os.utime(ics, (old_ts, old_ts))

    ran = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_subproc(cmd, **kw):
        ran.append(cmd)
        return FakeProc()

    monkeypatch.setattr("pebble_index_mcp.calendar.subprocess.run", fake_subproc)
    c = CalendarClient(khal_bin="/bin/khal", store_dir=tmp_path, vdirsyncer_bin="/bin/vdirsyncer", stale_minutes=20)
    c.lookup("today")
    assert ran[0] == ["/bin/vdirsyncer", "sync"]
    assert ran[1][0] == "/bin/khal"
