"""Pure-python helpers to turn Agur daily index readings into Home Assistant statistics.

No Home Assistant imports here so that the logic can be unit-tested offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class DailyReading:
    date: datetime  # tz-aware, midnight local time
    index_l: float | None  # meter reading in liters, None when missing
    volume_l: float | None  # consumption of the day in liters as computed by the provider


@dataclass
class StatRow:
    start: datetime
    state_m3: float | None  # meter reading in m³
    sum_m3: float  # cumulative consumption in m³ since the first imported reading


def latest_reset_start(readings: list[DailyReading]) -> int:
    """Return the position (in chronological order) of the first reading of the current meter.

    A reset is detected when the index drops compared to the previous known index
    (meter replacement). Readings without index are skipped for the detection.
    """
    prev: float | None = None
    start = 0
    for pos, reading in enumerate(readings):
        if reading.index_l is None:
            continue
        if prev is not None and reading.index_l < prev:
            start = pos
        prev = reading.index_l
    return start


def daily_volume_l(previous: DailyReading | None, current: DailyReading) -> float | None:
    """Volume consumed on `current` day, preferring the provider's figure."""
    if current.volume_l is not None:
        return float(current.volume_l)
    if previous is not None and previous.index_l is not None and current.index_l is not None:
        diff = current.index_l - previous.index_l
        return diff if diff >= 0 else None
    return None


def build_statistics(
    readings: list[DailyReading],
    anchor_start: datetime | None = None,
    anchor_sum_m3: float = 0.0,
    since: datetime | None = None,
) -> list[StatRow]:
    """Build hourly statistic rows from daily readings.

    - `readings` can be in any order; they are sorted chronologically.
    - Only readings from the latest meter reset onwards are used.
    - When `anchor_start` is given, only readings strictly after it are returned and the
      cumulative sum continues from `anchor_sum_m3` (the sum stored for the anchor row).
    - `since` optionally restricts the output to readings at or after that date; the sum
      is still accumulated from the reset point so values stay consistent.
    """
    ordered = sorted(readings, key=lambda r: r.date)
    ordered = ordered[latest_reset_start(ordered):]

    rows: list[StatRow] = []
    running = 0.0
    previous: DailyReading | None = None
    for reading in ordered:
        volume = daily_volume_l(previous, reading)
        if reading.index_l is not None:
            previous = reading
        if volume is None:
            # Nothing measurable that day (e.g. meter swap day): no row, sum unchanged.
            continue
        if anchor_start is not None:
            if reading.date <= anchor_start:
                continue
            running += volume / 1000
            total = anchor_sum_m3 + running
        else:
            running += volume / 1000
            total = running
        if since is not None and reading.date < since:
            continue
        rows.append(
            StatRow(
                start=reading.date.replace(minute=0, second=0, microsecond=0),
                state_m3=reading.index_l / 1000 if reading.index_l is not None else None,
                sum_m3=round(total, 6),
            )
        )
    return rows
