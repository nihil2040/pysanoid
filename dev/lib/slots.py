"""The time a snapshot type was last due, by brute force: the answer dev/sweep_dst.py checks pysanoid's
_last_preferred_time against, and the one the fault sweep and the trouble simulation judge a run's snapshots by.

  hourly/frequently  the most recent real instant <= now whose local wall clock reads the preferred minute
                     (both passes through a repeated hour count; a skipped hour has none)
  daily and longer   one per calendar period: the wall time's first occurrence (zoneinfo fold=0, so a
                     skipped wall time maps forward by the gap and a repeated one to its first pass),
                     the latest such time <= now, skipping periods that lack the day (the 31st, 29 February)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def perl_wday(d: date) -> int:
    return (d.weekday() + 1) % 7  # Sunday = 0, like localtime


def first_occurrence(tz: ZoneInfo, y: int, mo: int, d: int, h: int, mi: int) -> float | None:
    try:
        return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()
    except ValueError:  # a day the month (or year) lacks
        return None


def ideal(now: int, tz: ZoneInfo, cfg: dict[str, Any], t: str) -> float | None:
    """The brute-force answer (see the module docstring); None where pysanoid is expected to raise."""
    now_dt = datetime.fromtimestamp(now, tz)
    today = now_dt.date()
    if t in ("hourly", "frequently"):
        if t == "hourly":
            wanted = {int(cfg["hourly_min"])}
        else:
            size = abs(int(cfg["frequent_period"]))
            wanted = {k * size for k in range(60) if k * size < 60}
        m = now - now % 60
        for _ in range(60 * 30):  # real minutes, newest first
            if datetime.fromtimestamp(m, tz).minute in wanted:
                return float(m)
            m -= 60
        raise RuntimeError("no hourly/frequently candidate within 30 hours")
    if t == "monthly":
        mday = int(cfg["monthly_mday"])
        if first_occurrence(tz, today.year, today.month, mday, 0, 0) is None:
            return None  # the current month lacks the day: pysanoid raises, plan_snapshots reports it
        cands, y, mo = [], today.year, today.month
        for _ in range(14):
            cands.append(first_occurrence(tz, y, mo, mday, int(cfg["monthly_hour"]), int(cfg["monthly_min"])))
            y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
    elif t == "yearly":
        mon, mday = int(cfg["yearly_mon"]), int(cfg["yearly_mday"])
        if first_occurrence(tz, today.year, mon, mday, 0, 0) is None:
            return None  # the current year lacks the day (29 February)
        cands = [first_occurrence(tz, today.year - k, mon, mday, int(cfg["yearly_hour"]), int(cfg["yearly_min"]))
                 for k in range(10)]
    elif t == "daily":
        cands = [first_occurrence(tz, d.year, d.month, d.day, int(cfg["daily_hour"]), int(cfg["daily_min"]))
                 for d in (today - timedelta(days=k) for k in range(3))]
    else:  # weekly
        target = int(cfg["weekly_wday"]) % 7
        cands = [first_occurrence(tz, d.year, d.month, d.day, int(cfg["weekly_hour"]), int(cfg["weekly_min"]))
                 for d in (today - timedelta(days=k) for k in range(15)) if perl_wday(d) == target]
    return max(c for c in cands if c is not None and c <= now)
