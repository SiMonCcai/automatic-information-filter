"""DeepSeek peak/off-peak scheduling in the provider's billing timezone."""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def is_deepseek_idle_time(now: datetime | None = None) -> bool:
    """Return whether a new DeepSeek batch may start at *now*.

    DeepSeek peak windows are 09:00-12:00 and 14:00-18:00 Beijing time.
    A five-minute buffer prevents a newly-started batch from crossing into a
    peak window. Naive datetimes are interpreted as UTC, matching the server.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_time = current.astimezone(BEIJING).time().replace(tzinfo=None)
    morning_blocked = time(8, 55) <= local_time < time(12, 0)
    afternoon_blocked = time(13, 55) <= local_time < time(18, 0)
    return not (morning_blocked or afternoon_blocked)
