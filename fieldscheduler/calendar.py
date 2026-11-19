from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil, floor
from zoneinfo import ZoneInfo

from fieldscheduler.snapshot import PlanningSnapshot


def to_utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("INPUT_INVALID:NAIVE_DATETIME")
    return value.astimezone(timezone.utc)


def slot_ceil(value: datetime, horizon_start: datetime, slot_minutes: int) -> int:
    delta_seconds = (to_utc(value) - to_utc(horizon_start)).total_seconds()
    slot_seconds = slot_minutes * 60
    return ceil(delta_seconds / slot_seconds)


def slot_floor(value: datetime, horizon_start: datetime, slot_minutes: int) -> int:
    delta_seconds = (to_utc(value) - to_utc(horizon_start)).total_seconds()
    return floor(delta_seconds / (slot_minutes * 60))


def expand_utc_windows(
    windows: tuple[tuple[datetime, datetime], ...],
    horizon_start: datetime,
    horizon_slots: int,
    slot_minutes: int,
) -> tuple[tuple[int, int], ...]:
    expanded: list[tuple[int, int]] = []
    for start, end in windows:
        start_slot = max(0, slot_ceil(start, horizon_start, slot_minutes))
        end_slot = min(horizon_slots, slot_floor(end, horizon_start, slot_minutes))
        if start_slot < end_slot:
            expanded.append((start_slot, end_slot))
    return tuple(sorted(expanded))


def feasible_starts(
    windows: tuple[tuple[int, int], ...],
    duration_slots: int,
    earliest_start_slot: int,
    as_of_slot: int,
    horizon_slots: int,
) -> tuple[int, ...]:
    earliest = max(earliest_start_slot, as_of_slot, 0)
    starts = {
        start
        for window_start, window_end in windows
        for start in range( max(earliest, window_start), min(window_end - duration_slots, horizon_slots - duration_slots) + 1)
    }
    return tuple(sorted(starts))


def _resolve_local(value: datetime, zone: ZoneInfo, is_end: bool) -> datetime:
    candidates: list[datetime] = []
    for fold in (0, 1):
        localized = value.replace(tzinfo=zone, fold=fold)
        if localized.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == value:
            candidates.append(localized)
    if candidates:
        return (max if is_end else min)(candidates, key=lambda item: item.astimezone(timezone.utc))
    shifted = value
    for _ in range(180):
        shifted += timedelta(minutes=1)
        localized = shifted.replace(tzinfo=zone, fold=0)
        if localized.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == shifted:
            return localized
    raise ValueError("INPUT_INVALID:DST_LOCAL_TIME_UNRESOLVABLE")


def _subtract_interval(
    intervals: list[tuple[datetime, datetime]], blocked: tuple[datetime, datetime]
) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    block_start, block_end = blocked
    for start, end in intervals:
        if block_end <= start or block_start >= end:
            result.append((start, end))
            continue
        if start < block_start:
            result.append((start, block_start))
        if block_end < end:
            result.append((block_end, end))
    return result


def expand_resource_windows(snapshot: PlanningSnapshot) -> dict[str, tuple[tuple[datetime, datetime], ...]]:
    horizon_start = to_utc(snapshot.horizon.start_at)
    horizon_end = horizon_start + timedelta(hours=snapshot.horizon.hours)
    weekly_by_calendar: dict[object, list] = {}
    for shift in snapshot.weekly_availability:
        weekly_by_calendar.setdefault(shift.calendar_id, []).append(shift)
    exceptions_by_calendar: dict[object, list] = {}
    for exception in snapshot.availability_exceptions:
        exceptions_by_calendar.setdefault(exception.calendar_id, []).append(exception)
    result: dict[str, tuple[tuple[datetime, datetime], ...]] = {}
    for calendar in snapshot.resource_calendars:
        zone = ZoneInfo(calendar.timezone)
        local_start = horizon_start.astimezone(zone).date() - timedelta(days=1)
        local_end = horizon_end.astimezone(zone).date() + timedelta(days=1)
        intervals: list[tuple[datetime, datetime]] = []
        current = local_start
        while current <= local_end:
            for shift in weekly_by_calendar.get(calendar.id, []):
                if current.weekday() != shift.weekday:
                    continue
                if shift.valid_from and current < shift.valid_from or shift.valid_to and current > shift.valid_to:
                    continue
                local_begin = datetime.combine(current, shift.start)
                end_date = current + timedelta(days=1) if shift.end <= shift.start else current
                local_finish = datetime.combine(end_date, shift.end)
                start_utc = _resolve_local(local_begin, zone, False).astimezone(timezone.utc)
                end_utc = _resolve_local(local_finish, zone, True).astimezone(timezone.utc)
                start_utc, end_utc = max(start_utc, horizon_start), min(end_utc, horizon_end)
                if start_utc < end_utc:
                    intervals.append((start_utc, end_utc))
            current += timedelta(days=1)
        intervals.sort()
        calendar_exceptions = exceptions_by_calendar.get(calendar.id, [])
        for exception in (item for item in calendar_exceptions if item.exception_type == "AVAILABLE"):
            bounded = (max(horizon_start, to_utc(exception.start_at)), min(horizon_end, to_utc(exception.end_at)))
            if bounded[0] >= bounded[1]:
                continue
            intervals.append(bounded)
            intervals.sort()
        for exception in (item for item in calendar_exceptions if item.exception_type == "UNAVAILABLE"):
            bounded = (max(horizon_start, to_utc(exception.start_at)), min(horizon_end, to_utc(exception.end_at)))
            if bounded[0] < bounded[1]:
                intervals = _subtract_interval(intervals, bounded)
        for maintenance in snapshot.maintenance_events:
            if maintenance.resource_id == calendar.owner_id:
                bounded = (max(horizon_start, to_utc(maintenance.start_at)), min(horizon_end, to_utc(maintenance.end_at)))
                if bounded[0] < bounded[1]:
                    intervals = _subtract_interval(intervals, bounded)
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result[str(calendar.owner_id)] = tuple(merged)
    return result


def intersect_slot_windows(
    base: tuple[tuple[int, int], ...],
    additional: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (max(left_start, right_start), min(left_end, right_end))
        for left_start, left_end in base
        for right_start, right_end in additional
        if max(left_start, right_start) < min(left_end, right_end)
    )
