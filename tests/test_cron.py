import datetime

import pytest

from cron import CronError, CronSchedule

MON_0900 = datetime.datetime(2026, 11, 9, 9, 0)   # a Monday
TUE_0900 = datetime.datetime(2026, 11, 10, 9, 0)


def test_default_weekly_refresh():
    s = CronSchedule("0 9 * * mon")
    assert s.matches(MON_0900)
    assert not s.matches(MON_0900.replace(minute=1))
    assert not s.matches(TUE_0900)


def test_nightly():
    s = CronSchedule("30 3 * * *")
    assert s.matches(datetime.datetime(2026, 11, 12, 3, 30))
    assert not s.matches(datetime.datetime(2026, 11, 12, 4, 30))


@pytest.mark.parametrize("dow", ["0", "7", "sun", "SUN"])
def test_sunday_aliases(dow):
    assert CronSchedule(f"0 9 * * {dow}").matches(datetime.datetime(2026, 11, 8, 9, 0))


def test_steps_ranges_lists():
    s = CronSchedule("*/15 8-10 * * mon-fri")
    assert s.matches(MON_0900.replace(minute=45))
    assert not s.matches(MON_0900.replace(minute=10))
    assert not s.matches(datetime.datetime(2026, 11, 8, 9, 0))  # Sunday
    assert CronSchedule("5,35 * * * *").matches(MON_0900.replace(minute=35))


def test_dom_and_dow_are_ored_like_cron():
    s = CronSchedule("0 9 1 * mon")
    assert s.matches(MON_0900)                                # Monday, not the 1st
    assert s.matches(datetime.datetime(2026, 12, 1, 9, 0))    # the 1st, a Tuesday
    assert not s.matches(TUE_0900)


@pytest.mark.parametrize("expr", ["", "0 9 * *", "61 9 * * *", "0 9 * * funday", "0 9-3 * * *", "*/0 * * * *"])
def test_invalid(expr):
    with pytest.raises(CronError):
        CronSchedule(expr)


def test_scheduler_skips_disabled_and_invalid_entries(monkeypatch):
    import app

    monkeypatch.setitem(app.CONFIG, "NIGHTLY_REINDEX_CRON", "30 3 * * *")
    monkeypatch.setitem(app.CONFIG, "WEEKLY_REFRESH_CRON", "off")
    monkeypatch.setitem(app.CONFIG, "WEEKLY_DISCOVERY_CRON", "every monday")
    names = [name for name, _schedule, _fn in app._load_schedule()]
    assert names == ["library re-index"]
