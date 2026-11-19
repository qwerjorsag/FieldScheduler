from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fieldscheduler.bundles import BundleCandidate
from fieldscheduler.calendar import (
    _resolve_local,
    _subtract_interval,
    expand_utc_windows,
    feasible_starts,
    intersect_slot_windows,
    slot_ceil,
    slot_floor,
    to_utc,
)


@dataclass(frozen=True)
class SeedCalendarExpansion:
    windows_by_resource: tuple[tuple[UUID, tuple[tuple[datetime, datetime], ...]], ...]
    diagnostics: tuple[str, ...]

    def windows(self) -> dict[UUID, tuple[tuple[datetime, datetime], ...]]:
        return dict(self.windows_by_resource)

    def slot_windows(self, horizon_start: datetime, horizon_slots: int, slot_minutes: int) -> dict[UUID, tuple[tuple[int, int], ...]]:
        return {
            resource_id: expand_utc_windows(windows, horizon_start, horizon_slots, slot_minutes)
            for resource_id, windows in self.windows_by_resource
        }


def expand_seed_resource_windows(
    entities: dict[str, Any],
    farm_id: UUID,
    horizon_start: datetime,
    horizon_hours: int,
) -> SeedCalendarExpansion:
    if horizon_start.utcoffset() is None or horizon_hours <= 0:
        return SeedCalendarExpansion((), ("INPUT_INVALID:CALENDAR_HORIZON",))
    start_utc = horizon_start.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(hours=horizon_hours)
    farm_key = str(farm_id)
    diagnostics: set[str] = set()
    calendars_by_owner: dict[UUID, dict[str, Any]] = {}
    calendar_by_id: dict[str, dict[str, Any]] = {}
    for row in entities.get("resource_calendars", []):
        if row.get("farm_id") != farm_key:
            continue
        owner_fields = [row.get("owner_worker_id"), row.get("owner_machine_id"), row.get("owner_equipment_id")]
        try:
            owners = [UUID(value) for value in owner_fields if value]
        except (ValueError, TypeError):
            diagnostics.add(f"INPUT_INVALID:RESOURCE_CALENDAR_OWNER:{row.get('id')}")
            continue
        if len(owners) != 1:
            diagnostics.add(f"INPUT_INVALID:RESOURCE_CALENDAR_OWNER:{row.get('id')}")
            continue
        owner_id = owners[0]
        if owner_id in calendars_by_owner:
            diagnostics.add(f"INPUT_INVALID:DUPLICATE_RESOURCE_CALENDAR:{owner_id}")
            continue
        try:
            ZoneInfo(row["timezone"])
            UUID(row["id"])
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            diagnostics.add(f"INPUT_INVALID:RESOURCE_CALENDAR:{row.get('id')}")
            continue
        calendars_by_owner[owner_id] = row
        calendar_by_id[row["id"]] = row

    for resource_group in ("workers", "machines", "equipment"):
        for resource in entities.get(resource_group, []):
            if resource.get("farm_id") == farm_key and resource.get("active", True):
                try:
                    resource_id = UUID(resource["id"])
                except (KeyError, ValueError):
                    diagnostics.add(f"INPUT_INVALID:RESOURCE_ID:{resource_group}")
                    continue
                if resource_id not in calendars_by_owner:
                    diagnostics.add(f"RESOURCE_CALENDAR_MISSING:{resource_id}")

    weekly_by_calendar: dict[str, list[dict[str, Any]]] = {}
    for row in entities.get("weekly_availability", []):
        if row.get("farm_id") != farm_key:
            continue
        if row.get("resource_calendar_id") not in calendar_by_id:
            diagnostics.add(f"INPUT_INVALID:WEEKLY_CALENDAR_REFERENCE:{row.get('id')}")
            continue
        weekly_by_calendar.setdefault(row["resource_calendar_id"], []).append(row)
    exceptions_by_calendar: dict[str, list[dict[str, Any]]] = {}
    for row in entities.get("availability_exceptions", []):
        if row.get("farm_id") != farm_key:
            continue
        if row.get("resource_calendar_id") not in calendar_by_id:
            diagnostics.add(f"INPUT_INVALID:AVAILABILITY_CALENDAR_REFERENCE:{row.get('id')}")
            continue
        exceptions_by_calendar.setdefault(row["resource_calendar_id"], []).append(row)
        if row.get("exception_type") not in {"AVAILABLE", "UNAVAILABLE"}:
            diagnostics.add(f"INPUT_INVALID:AVAILABILITY_EXCEPTION_TYPE:{row.get('id')}")

    maintenance_by_resource: dict[UUID, list[tuple[datetime, datetime]]] = {}
    for row in entities.get("maintenance_events", []):
        if row.get("farm_id") != farm_key:
            continue
        resource_values = [row.get("machine_id"), row.get("equipment_id")]
        try:
            resources = [UUID(value) for value in resource_values if value]
        except (ValueError, TypeError):
            diagnostics.add(f"INPUT_INVALID:MAINTENANCE_RESOURCE:{row.get('id')}")
            continue
        if len(resources) != 1:
            diagnostics.add(f"INPUT_INVALID:MAINTENANCE_RESOURCE:{row.get('id')}")
            continue
        try:
            blocked = (
                max(start_utc, to_utc(datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")))),
                min(end_utc, to_utc(datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")))),
            )
            if blocked[0] < blocked[1]:
                maintenance_by_resource.setdefault(resources[0], []).append(blocked)
        except (KeyError, ValueError):
            diagnostics.add(f"INPUT_INVALID:MAINTENANCE_INTERVAL:{row.get('id')}")

    for row in entities.get("field_blocks", []):
        if row.get("farm_id") != farm_key:
            continue
        try:
            block_start = to_utc(datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")))
            block_end = to_utc(datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")))
            UUID(row["field_id"])
            if block_start >= block_end:
                raise ValueError
        except (KeyError, ValueError):
            diagnostics.add(f"INPUT_INVALID:FIELD_BLOCK:{row.get('id')}")

    result: dict[UUID, tuple[tuple[datetime, datetime], ...]] = {}
    for owner_id, calendar in calendars_by_owner.items():
        zone = ZoneInfo(calendar["timezone"])
        local_start = start_utc.astimezone(zone).date() - timedelta(days=1)
        local_end = end_utc.astimezone(zone).date() + timedelta(days=1)
        raw_windows: list[tuple[datetime, datetime]] = []
        current_day = local_start
        while current_day <= local_end:
            for shift in weekly_by_calendar.get(calendar["id"], []):
                try:
                    weekday = int(shift["weekday"])
                    local_start_time = time.fromisoformat(shift["local_start_time"])
                    local_end_time = time.fromisoformat(shift["local_end_time"])
                    valid_from = date.fromisoformat(shift["valid_from"]) if shift.get("valid_from") else None
                    valid_to = date.fromisoformat(shift["valid_to"]) if shift.get("valid_to") else None
                    if not 0 <= weekday <= 6 or local_start_time == local_end_time:
                        raise ValueError
                except (KeyError, ValueError):
                    diagnostics.add(f"INPUT_INVALID:WEEKLY_SHIFT:{shift.get('id')}")
                    continue
                if current_day.weekday() != weekday or valid_from and current_day < valid_from or valid_to and current_day > valid_to:
                    continue
                end_day = current_day + timedelta(days=1) if local_end_time <= local_start_time else current_day
                try:
                    shift_start = _resolve_local(datetime.combine(current_day, local_start_time), zone, False).astimezone(timezone.utc)
                    shift_end = _resolve_local(datetime.combine(end_day, local_end_time), zone, True).astimezone(timezone.utc)
                except ValueError as error:
                    diagnostics.add(f"INPUT_INVALID:WEEKLY_SHIFT_DST:{shift.get('id')}:{error}")
                    continue
                shift_start, shift_end = max(start_utc, shift_start), min(end_utc, shift_end)
                if shift_start < shift_end:
                    raw_windows.append((shift_start, shift_end))
            current_day += timedelta(days=1)
        raw_windows.sort()
        calendar_exceptions = exceptions_by_calendar.get(calendar["id"], [])
        for exception in (row for row in calendar_exceptions if row.get("exception_type") == "AVAILABLE"):
            try:
                interval = (
                    max(start_utc, to_utc(datetime.fromisoformat(exception["start_at"].replace("Z", "+00:00")))),
                    min(end_utc, to_utc(datetime.fromisoformat(exception["end_at"].replace("Z", "+00:00")))),
                )
            except (KeyError, ValueError):
                diagnostics.add(f"INPUT_INVALID:AVAILABILITY_EXCEPTION:{exception.get('id')}")
                continue
            if interval[0] >= interval[1]:
                continue
            raw_windows.append(interval)
            raw_windows.sort()
        for exception in (row for row in calendar_exceptions if row.get("exception_type") == "UNAVAILABLE"):
            try:
                interval = (
                    max(start_utc, to_utc(datetime.fromisoformat(exception["start_at"].replace("Z", "+00:00")))),
                    min(end_utc, to_utc(datetime.fromisoformat(exception["end_at"].replace("Z", "+00:00")))),
                )
            except (KeyError, ValueError):
                diagnostics.add(f"INPUT_INVALID:AVAILABILITY_EXCEPTION:{exception.get('id')}")
                continue
            if interval[0] < interval[1]:
                raw_windows = _subtract_interval(raw_windows, interval)
        for interval in maintenance_by_resource.get(owner_id, []):
            raw_windows = _subtract_interval(raw_windows, interval)
        merged: list[tuple[datetime, datetime]] = []
        for window_start, window_end in sorted(raw_windows):
            if merged and window_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], window_end))
            else:
                merged.append((window_start, window_end))
        result[owner_id] = tuple(merged)
    return SeedCalendarExpansion(
        tuple(sorted(result.items(), key=lambda item: str(item[0]))),
        tuple(sorted(diagnostics)),
    )


def rest_guarantee_diagnostics(
    windows: tuple[tuple[datetime, datetime], ...],
    min_rest_minutes: int,
    worker_id: UUID,
) -> tuple[str, ...]:
    if min_rest_minutes < 0:
        return (f"INPUT_INVALID:MIN_REST:{worker_id}",)
    ordered = sorted((start.astimezone(timezone.utc), end.astimezone(timezone.utc)) for start, end in windows)
    for previous, following in zip(ordered, ordered[1:]):
        rest_minutes = (following[0] - previous[1]).total_seconds() / 60
        if rest_minutes < min_rest_minutes:
            return (f"REST_NOT_GUARANTEED_BY_CALENDAR:{worker_id}",)
    return ()


@dataclass(frozen=True)
class SeedFeasibleStartProvider:
    entities: dict[str, Any]
    farm_id: UUID
    horizon_start: datetime
    horizon_hours: int
    slot_minutes: int
    as_of_slot: int
    windows_by_resource: dict[UUID, tuple[tuple[int, int], ...]]
    field_by_operation: dict[UUID, UUID]

    def __post_init__(self) -> None:
        if self.horizon_start.utcoffset() is None or self.horizon_hours <= 0 or self.slot_minutes not in (15, 30, 60):
            raise ValueError("INPUT_INVALID:FEASIBLE_START_TIMEBASE")

    def __call__(self, bundle: BundleCandidate) -> tuple[int, ...]:
        horizon_slots = self.horizon_hours * 60 // self.slot_minutes
        if bundle.operation_id not in self.field_by_operation:
            return ()
        operation = next((row for row in self.entities.get("field_operations", []) if row.get("id") == str(bundle.operation_id) and row.get("farm_id") == str(self.farm_id)), None)
        if operation is None:
            return ()
        resource_ids = (*bundle.worker_ids, *bundle.machine_ids, *bundle.equipment_ids)
        common = ((0, horizon_slots),)
        for resource_id in resource_ids:
            resource_windows = self.windows_by_resource.get(resource_id)
            if not resource_windows:
                return ()
            common = intersect_slot_windows(common, resource_windows)
            if not common:
                return ()

        field_id = self.field_by_operation[bundle.operation_id]
        blocks: list[tuple[int, int]] = []
        horizon_end = self.horizon_start.astimezone(timezone.utc) + timedelta(hours=self.horizon_hours)
        for row in self.entities.get("field_blocks", []):
            if row.get("farm_id") != str(self.farm_id) or row.get("field_id") != str(field_id):
                continue
            try:
                start_at = to_utc(datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00")))
                end_at = to_utc(datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00")))
            except (KeyError, ValueError):
                continue
            start_slot = max(0, slot_floor(start_at, self.horizon_start, self.slot_minutes))
            end_slot = min(horizon_slots, slot_ceil(end_at, self.horizon_start, self.slot_minutes))
            if start_slot < end_slot:
                blocks.append((start_slot, end_slot))
        for blocked_start, blocked_end in blocks:
            common = _subtract_slot_window(common, (blocked_start, blocked_end))

        earliest = 0
        latest_finish = horizon_slots
        try:
            if operation.get("earliest_start_at"):
                earliest = slot_ceil(to_utc(datetime.fromisoformat(operation["earliest_start_at"].replace("Z", "+00:00"))), self.horizon_start, self.slot_minutes)
            if operation.get("latest_finish_at"):
                latest_finish = slot_floor(to_utc(datetime.fromisoformat(operation["latest_finish_at"].replace("Z", "+00:00"))), self.horizon_start, self.slot_minutes)
        except (ValueError, TypeError):
            return ()
        common = tuple((start, min(end, latest_finish)) for start, end in common if start < min(end, latest_finish))
        return feasible_starts(common, bundle.duration_slots, earliest, self.as_of_slot, horizon_slots)


def _subtract_slot_window(
    windows: tuple[tuple[int, int], ...],
    blocked: tuple[int, int],
) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    block_start, block_end = blocked
    for start, end in windows:
        if block_end <= start or block_start >= end:
            result.append((start, end))
            continue
        if start < block_start:
            result.append((start, block_start))
        if block_end < end:
            result.append((block_end, end))
    return tuple(result)
