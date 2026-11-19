from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import UUID


class SnapshotValidationError(ValueError):
    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__(";".join(issues))


@dataclass(frozen=True)
class Horizon:
    start_at: datetime
    as_of: datetime
    hours: int
    slot_minutes: int
    timezone: str

    @property
    def slots(self) -> int:
        return self.hours * 60 // self.slot_minutes


@dataclass(frozen=True)
class ResourceBundle:
    worker_ids: tuple[UUID, ...]
    machine_ids: tuple[UUID, ...]
    equipment_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class SolverOperation:
    id: UUID
    field_id: UUID
    area_ha: Decimal
    mandatory: bool
    duration_minutes: int
    earliest_start_slot: int
    bundle: ResourceBundle


@dataclass(frozen=True)
class Dependency:
    predecessor_id: UUID
    successor_id: UUID
    min_lag_minutes: int
    max_lag_minutes: int | None = None


@dataclass(frozen=True)
class ResourceCalendar:
    id: UUID
    timezone: str
    worker_id: UUID | None
    machine_id: UUID | None
    equipment_id: UUID | None

    @property
    def owner_id(self) -> UUID:
        owner = self.worker_id or self.machine_id or self.equipment_id
        assert owner is not None
        return owner


@dataclass(frozen=True)
class WeeklyAvailability:
    calendar_id: UUID
    weekday: int
    start: time
    end: time
    valid_from: date | None
    valid_to: date | None


@dataclass(frozen=True)
class AvailabilityException:
    calendar_id: UUID
    start_at: datetime
    end_at: datetime
    exception_type: str


@dataclass(frozen=True)
class MaintenanceEvent:
    resource_id: UUID
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class PlanningSnapshot:
    horizon: Horizon
    workers: tuple[UUID, ...]
    machines: tuple[UUID, ...]
    equipment: tuple[UUID, ...]
    field_ids: tuple[UUID, ...]
    operations: tuple[SolverOperation, ...]
    dependencies: tuple[Dependency, ...]
    allowed_work_windows: tuple[tuple[datetime, datetime], ...]
    resource_calendars: tuple[ResourceCalendar, ...] = ()
    weekly_availability: tuple[WeeklyAvailability, ...] = ()
    availability_exceptions: tuple[AvailabilityException, ...] = ()
    maintenance_events: tuple[MaintenanceEvent, ...] = ()


def snapshot_from_fixture(raw: dict) -> PlanningSnapshot:
    issues: list[str] = []

    def parse_uuid(value: object, path: str) -> UUID:
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            issues.append(f"INPUT_INVALID:{path}:UUID")
            return UUID(int=0)

    def parse_datetime(value: object, path: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError
            return parsed
        except (ValueError, TypeError):
            issues.append(f"INPUT_INVALID:{path}:DATETIME")
            return datetime.fromtimestamp(0).astimezone()

    if raw.get("schema_version") != "field-scheduler-solver-fixture/v1":
        issues.append("INPUT_INVALID:schema_version:UNSUPPORTED")
    try:
        source_horizon = raw["horizon"]
        slot_minutes = int(source_horizon["slot_minutes"])
        hours = int(source_horizon["horizon_hours"])
        if slot_minutes not in (15, 30, 60) or hours <= 0 or (hours * 60) % slot_minutes:
            issues.append("INPUT_INVALID:horizon:SLOT_CONFIGURATION")
        horizon = Horizon(
            parse_datetime(source_horizon["start_at"], "horizon.start_at"),
            parse_datetime(source_horizon["as_of"], "horizon.as_of"),
            hours,
            slot_minutes,
            str(source_horizon["timezone"]),
        )
        try:
            ZoneInfo(horizon.timezone)
        except ZoneInfoNotFoundError:
            issues.append("INPUT_INVALID:horizon.timezone:UNKNOWN_IANA_ZONE")
        horizon_end = horizon.start_at.timestamp() + hours * 3600
        if not horizon.start_at <= horizon.as_of or horizon.as_of.timestamp() > horizon_end:
            issues.append("INPUT_INVALID:horizon.as_of:OUTSIDE_HORIZON")
    except (KeyError, TypeError, ValueError):
        issues.append("INPUT_INVALID:horizon:REQUIRED")
        horizon = Horizon(datetime.fromtimestamp(0).astimezone(), datetime.fromtimestamp(0).astimezone(), 24, 60, "UTC")

    resources = raw.get("resources", {})
    worker_ids = tuple(parse_uuid(item.get("id"), "resources.workers.id") for item in resources.get("workers", []))
    machine_ids = tuple(parse_uuid(item.get("id"), "resources.machines.id") for item in resources.get("machines", []))
    equipment_ids = tuple(parse_uuid(item.get("id"), "resources.equipment.id") for item in resources.get("equipment", []))
    field_ids = tuple(parse_uuid(item.get("field_id"), "locations.field_id") for item in raw.get("locations", []))
    for label, ids in (("worker", worker_ids), ("machine", machine_ids), ("equipment", equipment_ids), ("field", field_ids)):
        if len(ids) != len(set(ids)):
            issues.append(f"INPUT_INVALID:{label}:DUPLICATE_ID")

    operations: list[SolverOperation] = []
    operation_ids: set[UUID] = set()
    for index, item in enumerate(raw.get("operations", [])):
        path = f"operations[{index}]"
        try:
            op_id = parse_uuid(item["id"], f"{path}.id")
            field_id = parse_uuid(item["field_id"], f"{path}.field_id")
            bundle_data = item["resource_bundle"]
            bundle = ResourceBundle(
                tuple(parse_uuid(value, f"{path}.worker_ids") for value in bundle_data["worker_ids"]),
                tuple(parse_uuid(value, f"{path}.machine_ids") for value in bundle_data["machine_ids"]),
                tuple(parse_uuid(value, f"{path}.equipment_ids") for value in bundle_data["equipment_ids"]),
            )
            area = Decimal(str(item["area_ha"]))
            duration = int(item["duration_minutes"])
            earliest = int(item.get("earliest_start_slot", 0))
            if area <= 0 or duration <= 0 or earliest < 0:
                issues.append(f"INPUT_INVALID:{path}:NON_POSITIVE_OR_NEGATIVE_VALUE")
            if op_id in operation_ids:
                issues.append(f"INPUT_INVALID:{path}.id:DUPLICATE_ID")
            operation_ids.add(op_id)
            if field_id not in field_ids:
                issues.append(f"INPUT_INVALID:{path}.field_id:UNKNOWN_FIELD")
            for group, known, label in ((bundle.worker_ids, worker_ids, "WORKER"), (bundle.machine_ids, machine_ids, "MACHINE"), (bundle.equipment_ids, equipment_ids, "EQUIPMENT")):
                if len(group) != len(set(group)):
                    issues.append(f"INPUT_INVALID:{path}:DUPLICATE_{label}")
                if not set(group).issubset(known):
                    issues.append(f"INPUT_INVALID:{path}:UNKNOWN_{label}")
            if not bundle.worker_ids or not bundle.machine_ids:
                issues.append(f"INPUT_INVALID:{path}:INCOMPLETE_RESOURCE_BUNDLE")
            operations.append(SolverOperation(op_id, field_id, area, bool(item.get("mandatory", True)), duration, earliest, bundle))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            issues.append(f"INPUT_INVALID:{path}:REQUIRED_OR_INVALID_VALUE")

    dependencies: list[Dependency] = []
    adjacency: dict[UUID, list[UUID]] = {operation_id: [] for operation_id in operation_ids}
    indegree: dict[UUID, int] = dict.fromkeys(operation_ids, 0)
    for index, item in enumerate(raw.get("dependencies", [])):
        try:
            predecessor = parse_uuid(item["predecessor_id"], f"dependencies[{index}].predecessor_id")
            successor = parse_uuid(item["successor_id"], f"dependencies[{index}].successor_id")
            lag = int(item["min_lag_minutes"])
            maximum_lag = int(item["max_lag_minutes"]) if item.get("max_lag_minutes") is not None else None
            if predecessor not in operation_ids or successor not in operation_ids:
                issues.append(f"INPUT_INVALID:dependencies[{index}]:UNKNOWN_OPERATION")
            elif lag < 0 or maximum_lag is not None and maximum_lag < 0:
                issues.append(f"INPUT_INVALID:dependencies[{index}]:NEGATIVE_LAG")
            elif maximum_lag is not None and maximum_lag < lag:
                issues.append(f"INPUT_INVALID:dependencies[{index}]:MAX_LAG_BELOW_MIN_LAG")
            else:
                dependencies.append(Dependency(predecessor, successor, lag, maximum_lag))
                adjacency[predecessor].append(successor)
                indegree[successor] += 1
        except (KeyError, TypeError, ValueError):
            issues.append(f"INPUT_INVALID:dependencies[{index}]:REQUIRED_OR_INVALID_VALUE")
    ready = [operation_id for operation_id, count in indegree.items() if count == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for successor in adjacency[current]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
    if visited != len(operation_ids):
        issues.append("DEPENDENCY_CYCLE")

    windows: list[tuple[datetime, datetime]] = []
    for index, pair in enumerate(raw.get("allowed_work_windows_utc", [])):
        try:
            start = parse_datetime(pair[0], f"allowed_work_windows_utc[{index}].start")
            end = parse_datetime(pair[1], f"allowed_work_windows_utc[{index}].end")
            if end <= start:
                issues.append(f"INPUT_INVALID:allowed_work_windows_utc[{index}]:INVALID_INTERVAL")
            windows.append((start, end))
        except (IndexError, TypeError):
            issues.append(f"INPUT_INVALID:allowed_work_windows_utc[{index}]:INVALID_INTERVAL")

    calendars: list[ResourceCalendar] = []
    calendar_ids: set[UUID] = set()
    known_resources_by_owner = (set(worker_ids), set(machine_ids), set(equipment_ids))
    for index, item in enumerate(raw.get("resource_calendars", [])):
        path = f"resource_calendars[{index}]"
        calendar_id = parse_uuid(item.get("id"), f"{path}.id")
        owners = [item.get("owner_worker_id"), item.get("owner_machine_id"), item.get("owner_equipment_id")]
        if sum(value is not None for value in owners) != 1:
            issues.append(f"INPUT_INVALID:{path}:OWNER_CARDINALITY")
        owner_ids = [parse_uuid(value, f"{path}.owner") if value is not None else None for value in owners]
        owner = next((value for value in owner_ids if value is not None), None)
        if any(owner_ids[position] is not None and owner_ids[position] not in known_resources_by_owner[position] for position in range(3)):
            issues.append(f"INPUT_INVALID:{path}:UNKNOWN_OWNER")
        if calendar_id in calendar_ids:
            issues.append(f"INPUT_INVALID:{path}.id:DUPLICATE_ID")
        calendar_ids.add(calendar_id)
        zone = str(item.get("timezone", ""))
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            issues.append(f"INPUT_INVALID:{path}.timezone:UNKNOWN_IANA_ZONE")
        calendars.append(ResourceCalendar(calendar_id, zone, owner_ids[0], owner_ids[1], owner_ids[2]))
    calendar_by_id = {calendar.id: calendar for calendar in calendars}

    weekly: list[WeeklyAvailability] = []
    for index, item in enumerate(raw.get("weekly_availability", [])):
        path = f"weekly_availability[{index}]"
        try:
            calendar_id = parse_uuid(item["resource_calendar_id"], f"{path}.resource_calendar_id")
            weekday = int(item["weekday"])
            start_time = time.fromisoformat(item["local_start_time"])
            end_time = time.fromisoformat(item["local_end_time"])
            valid_from = date.fromisoformat(item["valid_from"]) if item.get("valid_from") else None
            valid_to = date.fromisoformat(item["valid_to"]) if item.get("valid_to") else None
            if calendar_id not in calendar_by_id:
                issues.append(f"INPUT_INVALID:{path}:UNKNOWN_CALENDAR")
            if not 0 <= weekday <= 6 or start_time == end_time:
                issues.append(f"INPUT_INVALID:{path}:INVALID_SHIFT")
            if valid_from and valid_to and valid_to < valid_from:
                issues.append(f"INPUT_INVALID:{path}:INVALID_VALIDITY")
            weekly.append(WeeklyAvailability(calendar_id, weekday, start_time, end_time, valid_from, valid_to))
        except (KeyError, TypeError, ValueError):
            issues.append(f"INPUT_INVALID:{path}:REQUIRED_OR_INVALID_VALUE")

    exceptions: list[AvailabilityException] = []
    for index, item in enumerate(raw.get("availability_exceptions", [])):
        path = f"availability_exceptions[{index}]"
        try:
            calendar_id = parse_uuid(item["resource_calendar_id"], f"{path}.resource_calendar_id")
            start_at = parse_datetime(item["start_at"], f"{path}.start_at")
            end_at = parse_datetime(item["end_at"], f"{path}.end_at")
            exception_type = str(item["exception_type"])
            if calendar_id not in calendar_by_id:
                issues.append(f"INPUT_INVALID:{path}:UNKNOWN_CALENDAR")
            if end_at <= start_at or exception_type not in {"AVAILABLE", "UNAVAILABLE"}:
                issues.append(f"INPUT_INVALID:{path}:INVALID_EXCEPTION")
            exceptions.append(AvailabilityException(calendar_id, start_at, end_at, exception_type))
        except (KeyError, TypeError, ValueError):
            issues.append(f"INPUT_INVALID:{path}:REQUIRED_OR_INVALID_VALUE")

    maintenance: list[MaintenanceEvent] = []
    for index, item in enumerate(raw.get("maintenance_events", [])):
        path = f"maintenance_events[{index}]"
        try:
            machine_value, equipment_value = item.get("machine_id"), item.get("equipment_id")
            if (machine_value is None) == (equipment_value is None):
                issues.append(f"INPUT_INVALID:{path}:RESOURCE_CARDINALITY")
            resource_id = parse_uuid(machine_value if machine_value is not None else equipment_value, f"{path}.resource_id")
            if resource_id not in set(machine_ids) | set(equipment_ids):
                issues.append(f"INPUT_INVALID:{path}:UNKNOWN_RESOURCE")
            start_at = parse_datetime(item["starts_at"], f"{path}.starts_at")
            end_at = parse_datetime(item["ends_at"], f"{path}.ends_at")
            if end_at <= start_at:
                issues.append(f"INPUT_INVALID:{path}:INVALID_INTERVAL")
            maintenance.append(MaintenanceEvent(resource_id, start_at, end_at))
        except (KeyError, TypeError, ValueError):
            issues.append(f"INPUT_INVALID:{path}:REQUIRED_OR_INVALID_VALUE")

    if issues:
        raise SnapshotValidationError(tuple(issues))
    return PlanningSnapshot(horizon, worker_ids, machine_ids, equipment_ids, field_ids, tuple(operations), tuple(dependencies), tuple(windows), tuple(calendars), tuple(weekly), tuple(exceptions), tuple(maintenance))
