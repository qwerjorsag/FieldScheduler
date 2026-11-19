from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from typing import Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from fieldscheduler.optimizer import SelectedOption
from fieldscheduler.travel_certificate import TravelLegCertificate


@dataclass(frozen=True)
class WorkerWorkLimit:
    timezone: str
    daily_minutes: int
    weekly_minutes: int
    min_rest_minutes: int = 0


@dataclass(frozen=True)
class _WorkInterval:
    starts_at: datetime
    ends_at: datetime
    source: str


def _next_midnight(day: date, zone: ZoneInfo) -> datetime:
    candidate = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    # Midnight transitions are rare but valid in IANA zones; move forward to
    # the first existing local instant if this midnight falls in a DST gap.
    for _ in range(180):
        roundtrip = candidate.astimezone(timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return roundtrip.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    raise ValueError("INPUT_INVALID:DST_LOCAL_MIDNIGHT")


def _period_segments(interval: _WorkInterval, zone: ZoneInfo) -> tuple[tuple[date, float], ...]:
    start = interval.starts_at.astimezone(timezone.utc)
    end = interval.ends_at.astimezone(timezone.utc)
    if start >= end:
        raise ValueError("INPUT_INVALID:WORK_INTERVAL")
    result: list[tuple[date, float]] = []
    cursor = start
    while cursor < end:
        local_day = cursor.astimezone(zone).date()
        boundary = _next_midnight(local_day, zone)
        segment_end = min(end, boundary)
        if segment_end <= cursor:
            raise ValueError("INPUT_INVALID:LOCAL_DAY_BOUNDARY")
        result.append((local_day, (segment_end - cursor).total_seconds() / 60))
        cursor = segment_end
    return tuple(result)


def validate_worker_work_limits(
    assignments: Sequence[SelectedOption],
    travel_legs: Sequence[TravelLegCertificate],
    duration_minutes_by_bundle: dict[UUID, int],
    limits_by_worker: dict[UUID, WorkerWorkLimit],
    horizon_start_at: datetime,
    slot_minutes: int,
    working_windows_by_worker: dict[UUID, tuple[tuple[datetime, datetime], ...]] | None = None,
) -> tuple[str, ...]:
    """Check selected work and certified travel against local daily/weekly limits."""
    if horizon_start_at.utcoffset() is None or slot_minutes not in (15, 30, 60):
        return ("INPUT_INVALID:WORK_LIMIT_TIMEBASE",)
    intervals: dict[UUID, list[_WorkInterval]] = {}
    issues: list[str] = []
    for assignment in assignments:
        duration = duration_minutes_by_bundle.get(assignment.bundle_id)
        if duration is None or duration <= 0:
            issues.append(f"WORK_DURATION_UNKNOWN:{assignment.bundle_id}")
            continue
        start = horizon_start_at.astimezone(timezone.utc) + timedelta(minutes=assignment.start_slot * slot_minutes)
        end = start + timedelta(minutes=duration)
        for worker_id in assignment.worker_ids:
            intervals.setdefault(worker_id, []).append(_WorkInterval(start, end, f"WORK:{assignment.operation_id}"))
    for leg in travel_legs:
        intervals.setdefault(leg.driver_worker_id, []).append(_WorkInterval(leg.departure_at, leg.arrival_at, f"TRAVEL:{leg.towing_machine_id}"))

    for worker_id, worker_intervals in intervals.items():
        limit = limits_by_worker.get(worker_id)
        if limit is None:
            issues.append(f"WORK_LIMITS_MISSING:{worker_id}")
            continue
        if limit.daily_minutes < 0 or limit.weekly_minutes < 0 or limit.min_rest_minutes < 0:
            issues.append(f"INPUT_INVALID:WORK_LIMITS:{worker_id}")
            continue
        try:
            zone = ZoneInfo(limit.timezone)
        except Exception:
            issues.append(f"INPUT_INVALID:WORK_LIMIT_TIMEZONE:{worker_id}")
            continue
        daily: dict[date, float] = {}
        weekly: dict[tuple[int, int], float] = {}
        ordered = sorted(worker_intervals, key=lambda value: (value.starts_at, value.ends_at))
        for interval in ordered:
            if interval.starts_at.utcoffset() is None or interval.ends_at.utcoffset() is None:
                issues.append(f"INPUT_INVALID:WORK_INTERVAL:{worker_id}")
                continue
            for local_day, minutes in _period_segments(interval, zone):
                daily[local_day] = daily.get(local_day, 0) + minutes
                iso = local_day.isocalendar()
                key = (iso.year, iso.week)
                weekly[key] = weekly.get(key, 0) + minutes
        for local_day, minutes in daily.items():
            if ceil(minutes) > limit.daily_minutes:
                issues.append(f"DAILY_WORK_LIMIT_EXCEEDED:{worker_id}:{local_day.isoformat()}:{ceil(minutes)}")
        for (year, week), minutes in weekly.items():
            if ceil(minutes) > limit.weekly_minutes:
                issues.append(f"WEEKLY_WORK_LIMIT_EXCEEDED:{worker_id}:{year}-W{week:02d}:{ceil(minutes)}")

        if limit.min_rest_minutes:
            shift_windows = (working_windows_by_worker or {}).get(worker_id)
            if shift_windows is None:
                issues.append(f"REST_NOT_GUARANTEED_BY_CALENDAR:{worker_id}")
                continue
            active_shifts: list[tuple[datetime, datetime]] = []
            for window_start, window_end in shift_windows:
                if window_start.utcoffset() is None or window_end.utcoffset() is None:
                    issues.append(f"INPUT_INVALID:SHIFT_WINDOW:{worker_id}")
                    continue
                ws, we = window_start.astimezone(timezone.utc), window_end.astimezone(timezone.utc)
                if any(interval.starts_at < we and ws < interval.ends_at for interval in ordered):
                    active_shifts.append((ws, we))
            active_shifts.sort()
            for previous, following in zip(active_shifts, active_shifts[1:]):
                rest = (following[0] - previous[1]).total_seconds() / 60
                if rest < limit.min_rest_minutes:
                    issues.append(f"MIN_REST_VIOLATION:{worker_id}:{ceil(rest)}")
    return tuple(dict.fromkeys(issues))
