from datetime import datetime

from xobop.reminder_time import parse_reminder_when

NOW = datetime(2026, 8, 4, 12, 0, 0)  # a Tuesday, noon


def test_relative_minutes():
    assert parse_reminder_when("in 30m", NOW) == datetime(2026, 8, 4, 12, 30, 0)


def test_relative_hours():
    assert parse_reminder_when("in 2h", NOW) == datetime(2026, 8, 4, 14, 0, 0)


def test_relative_days():
    assert parse_reminder_when("in 3d", NOW) == datetime(2026, 8, 7, 12, 0, 0)


def test_relative_composed():
    assert parse_reminder_when("in 1h30m", NOW) == datetime(2026, 8, 4, 13, 30, 0)


def test_at_24h_clock_later_today():
    assert parse_reminder_when("at 15:30", NOW) == datetime(2026, 8, 4, 15, 30, 0)


def test_at_24h_clock_already_passed_rolls_to_tomorrow():
    assert parse_reminder_when("at 09:00", NOW) == datetime(2026, 8, 5, 9, 0, 0)


def test_at_12h_clock_pm():
    assert parse_reminder_when("at 3pm", NOW) == datetime(2026, 8, 4, 15, 0, 0)


def test_at_12h_clock_am_rolls_to_tomorrow_when_midnight_already_passed():
    # NOW is noon, so today's midnight has already passed -> next occurrence is tomorrow.
    assert parse_reminder_when("at 12am", NOW) == datetime(2026, 8, 5, 0, 0, 0)


def test_today_at_explicit():
    assert parse_reminder_when("today at 08:00", NOW) == datetime(2026, 8, 4, 8, 0, 0)


def test_tomorrow_at():
    assert parse_reminder_when("tomorrow at 9am", NOW) == datetime(2026, 8, 5, 9, 0, 0)


def test_rejects_unrecognized_phrasing():
    assert parse_reminder_when("next monday morning", NOW) is None


def test_rejects_bare_number():
    assert parse_reminder_when("in 30", NOW) is None


def test_rejects_invalid_hour():
    assert parse_reminder_when("at 25:00", NOW) is None


def test_rejects_invalid_12h_hour():
    assert parse_reminder_when("at 13pm", NOW) is None


def test_rejects_empty():
    assert parse_reminder_when("", NOW) is None
