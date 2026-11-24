"""Time- and state-aware polling schedule.

Pure scheduling logic with no Home Assistant dependencies: given the local
time and what the vehicles are doing, decide how long to wait before the next
poll. The coordinator owns the state (which car is charging or in use) and
applies the result to its update interval.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
import logging
import re
from typing import Any

from .const import (
    CONF_CHARGING_SCAN_INTERVAL,
    CONF_DAY_SCAN_INTERVAL,
    CONF_DAY_START,
    CONF_DYNAMIC_POLLING,
    CONF_IN_USE_SCAN_INTERVAL,
    CONF_IN_USE_WINDOWS,
    CONF_NIGHT_SCAN_INTERVAL,
    CONF_NIGHT_START,
    DEFAULT_CHARGING_SCAN_INTERVAL,
    DEFAULT_DAY_SCAN_INTERVAL,
    DEFAULT_DAY_START,
    DEFAULT_DYNAMIC_POLLING,
    DEFAULT_IN_USE_SCAN_INTERVAL,
    DEFAULT_IN_USE_WINDOWS,
    DEFAULT_NIGHT_SCAN_INTERVAL,
    DEFAULT_NIGHT_START,
    MAX_DYNAMIC_SCAN_INTERVAL,
)
from .volvooncall_base import MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

POLLING_MODE_FIXED = "fixed"
POLLING_MODE_IN_USE = "in_use"
POLLING_MODE_CHARGING = "charging"
POLLING_MODE_DAY = "day"
POLLING_MODE_NIGHT = "night"

# When several modes apply at once the shortest interval wins; this order
# only breaks ties, so the reported mode is the most specific one.
_MODE_PRIORITY = {
    POLLING_MODE_IN_USE: 0,
    POLLING_MODE_CHARGING: 1,
    POLLING_MODE_DAY: 2,
    POLLING_MODE_NIGHT: 2,
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
# People type these on Chinese keyboards; accept the full-width forms.
_NORMALIZE = str.maketrans(
    {
        "：": ":",
        "，": ",",
        "；": ",",
        ";": ",",
        "、": ",",
        "\n": ",",
        "－": "-",
        "–": "-",
        "—": "-",
        "~": "-",
        "～": "-",
        "至": "-",
    }
)

TimeWindow = tuple[dt_time, dt_time]


def parse_time_of_day(value: Any) -> dt_time:
    """Parse ``HH:MM`` (seconds optional) into a time, or raise ValueError."""
    if isinstance(value, dt_time):
        return value.replace(microsecond=0, tzinfo=None)
    text = str(value).translate(_NORMALIZE).strip()
    match = _TIME_RE.match(text)
    if match is None:
        raise ValueError(f"invalid time of day: {value!r}")
    hour, minute, second = (int(part or 0) for part in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"invalid time of day: {value!r}")
    return dt_time(hour, minute, second)


def parse_time_windows(value: Any) -> tuple[TimeWindow, ...]:
    """Parse ``07:00-09:00,17:00-19:30`` into (start, end) pairs.

    An empty value means "no windows". A window whose end is earlier than
    its start wraps past midnight.
    """
    text = str(value or "").translate(_NORMALIZE)
    windows: list[TimeWindow] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        start_text, separator, end_text = chunk.partition("-")
        if not separator:
            raise ValueError(f"invalid time window: {chunk!r}")
        start = parse_time_of_day(start_text)
        end = parse_time_of_day(end_text)
        if start == end:
            raise ValueError(f"empty time window: {chunk!r}")
        windows.append((start, end))
    return tuple(windows)


def format_time_of_day(value: dt_time) -> str:
    return value.strftime("%H:%M")


def format_time_windows(windows: tuple[TimeWindow, ...]) -> str:
    """Return the canonical text form stored in the config entry."""
    return ",".join(
        f"{format_time_of_day(start)}-{format_time_of_day(end)}"
        for start, end in windows
    )


def _in_range(moment: dt_time, start: dt_time, end: dt_time) -> bool:
    """Return whether moment is in [start, end), wrapping past midnight."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def _clamp_interval(value: Any, default: int) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_SCAN_INTERVAL, min(seconds, MAX_DYNAMIC_SCAN_INTERVAL))


@dataclass(frozen=True)
class PollingSchedule:
    """Per-mode poll intervals plus the clock times that select them."""

    in_use_interval: int = DEFAULT_IN_USE_SCAN_INTERVAL
    charging_interval: int = DEFAULT_CHARGING_SCAN_INTERVAL
    day_interval: int = DEFAULT_DAY_SCAN_INTERVAL
    night_interval: int = DEFAULT_NIGHT_SCAN_INTERVAL
    day_start: dt_time = parse_time_of_day(DEFAULT_DAY_START)
    night_start: dt_time = parse_time_of_day(DEFAULT_NIGHT_START)
    in_use_windows: tuple[TimeWindow, ...] = parse_time_windows(
        DEFAULT_IN_USE_WINDOWS
    )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> PollingSchedule | None:
        """Build the schedule from entry data/options; None when disabled.

        The options flow validates everything it stores, so a bad value here
        means a hand-edited entry. Fall back to the default for that field
        rather than failing setup over a polling preference.
        """
        if not config.get(CONF_DYNAMIC_POLLING, DEFAULT_DYNAMIC_POLLING):
            return None

        def parsed(key, default, parser):
            raw = config.get(key, default)
            try:
                return parser(raw)
            except ValueError:
                _LOGGER.warning(
                    "Ignoring invalid %s setting; using the default", key
                )
                return parser(default)

        return cls(
            in_use_interval=_clamp_interval(
                config.get(CONF_IN_USE_SCAN_INTERVAL),
                DEFAULT_IN_USE_SCAN_INTERVAL,
            ),
            charging_interval=_clamp_interval(
                config.get(CONF_CHARGING_SCAN_INTERVAL),
                DEFAULT_CHARGING_SCAN_INTERVAL,
            ),
            day_interval=_clamp_interval(
                config.get(CONF_DAY_SCAN_INTERVAL), DEFAULT_DAY_SCAN_INTERVAL
            ),
            night_interval=_clamp_interval(
                config.get(CONF_NIGHT_SCAN_INTERVAL),
                DEFAULT_NIGHT_SCAN_INTERVAL,
            ),
            day_start=parsed(CONF_DAY_START, DEFAULT_DAY_START, parse_time_of_day),
            night_start=parsed(
                CONF_NIGHT_START, DEFAULT_NIGHT_START, parse_time_of_day
            ),
            in_use_windows=parsed(
                CONF_IN_USE_WINDOWS, DEFAULT_IN_USE_WINDOWS, parse_time_windows
            ),
        )

    @property
    def fastest_interval(self) -> int:
        return min(
            self.in_use_interval,
            self.charging_interval,
            self.day_interval,
            self.night_interval,
        )

    def is_day(self, moment: dt_time) -> bool:
        return _in_range(moment, self.day_start, self.night_start)

    def in_use_window(self, moment: dt_time) -> bool:
        return any(
            _in_range(moment, start, end) for start, end in self.in_use_windows
        )

    def select(
        self,
        now: datetime,
        *,
        in_use: bool = False,
        charging: bool = False,
    ) -> tuple[str, int]:
        """Return (mode, interval seconds) for this moment and vehicle state.

        ``in_use`` and ``charging`` are observed vehicle state; the configured
        in-use windows and the day/night split come from the clock. Every
        applicable mode is a candidate and the shortest interval wins, so a
        car charging overnight is polled at the charging interval while a
        long charging interval can never slow down a commute window.
        """
        moment = now.time().replace(tzinfo=None)
        candidates = [
            (self.day_interval, POLLING_MODE_DAY)
            if self.is_day(moment)
            else (self.night_interval, POLLING_MODE_NIGHT)
        ]
        if in_use or self.in_use_window(moment):
            candidates.append((self.in_use_interval, POLLING_MODE_IN_USE))
        if charging:
            candidates.append((self.charging_interval, POLLING_MODE_CHARGING))
        interval, mode = min(
            candidates, key=lambda item: (item[0], _MODE_PRIORITY[item[1]])
        )
        return mode, interval

    def seconds_until_faster(self, now: datetime, interval: float) -> float | None:
        """Return seconds to the next clock boundary that needs a faster poll.

        Without this a 30-minute night poll at 06:55 would sleep straight
        through the first 25 minutes of a 07:00 commute window. Only
        boundaries that fall inside the coming interval *and* switch to a
        shorter one are reported.
        """
        boundaries = {self.day_start, self.night_start}
        boundaries.update(start for start, _end in self.in_use_windows)
        soonest = None
        for boundary in boundaries:
            candidate = now.replace(
                hour=boundary.hour,
                minute=boundary.minute,
                second=boundary.second,
                microsecond=0,
            )
            if candidate <= now:
                candidate += timedelta(days=1)
            delay = (candidate - now).total_seconds()
            if delay >= interval:
                continue
            _mode, boundary_interval = self.select(candidate)
            if boundary_interval < interval and (
                soonest is None or delay < soonest
            ):
                soonest = delay
        return soonest
