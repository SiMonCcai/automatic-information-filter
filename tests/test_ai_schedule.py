from datetime import datetime
from zoneinfo import ZoneInfo

from pipeline.ai_schedule import is_deepseek_idle_time

BEIJING = ZoneInfo("Asia/Shanghai")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, tzinfo=BEIJING)


def test_peak_boundaries_are_blocked_with_five_minute_start_buffer():
    assert is_deepseek_idle_time(at(8, 54)) is True
    assert is_deepseek_idle_time(at(8, 55)) is False
    assert is_deepseek_idle_time(at(9, 0)) is False
    assert is_deepseek_idle_time(at(11, 59)) is False
    assert is_deepseek_idle_time(at(12, 0)) is True


def test_afternoon_peak_boundaries_are_blocked_with_five_minute_start_buffer():
    assert is_deepseek_idle_time(at(13, 54)) is True
    assert is_deepseek_idle_time(at(13, 55)) is False
    assert is_deepseek_idle_time(at(14, 0)) is False
    assert is_deepseek_idle_time(at(17, 59)) is False
    assert is_deepseek_idle_time(at(18, 0)) is True


def test_naive_or_utc_datetimes_are_converted_to_beijing_time():
    # Storage/server timestamps are UTC; 01:00 UTC is 09:00 Beijing.
    assert is_deepseek_idle_time(datetime(2026, 8, 17, 1, 0, tzinfo=ZoneInfo("UTC"))) is False
    assert is_deepseek_idle_time(datetime(2026, 8, 17, 4, 0)) is True
