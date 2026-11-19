from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from math import ceil, floor
from pathlib import Path
from typing import Any

from ortools.sat.python import cp_model

from fieldscheduler.calendar import (
    expand_resource_windows,
    expand_utc_windows,
    feasible_starts,
    intersect_slot_windows,
    slot_ceil,
)
from fieldscheduler.snapshot import SnapshotValidationError, snapshot_from_fixture
from fieldscheduler.farm_policy import FarmPolicy


@dataclass(frozen=True)
class Assignment:
    operation_id: str
    field_id: str
    start_slot: int
    end_slot: int
    worker_ids: tuple[str, ...]
    machine_ids: tuple[str, ...]
    equipment_ids: tuple[str, ...]


@dataclass(frozen=True)
class EngineResult:
    solver_status: str
    validated: bool
    assignments: tuple[Assignment, ...]
    diagnostics: tuple[str, ...]


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _slot(value: str, horizon_start: datetime, slot_minutes: int) -> int:
    seconds = (_parse_instant(value) - horizon_start).total_seconds()
    if seconds % (slot_minutes * 60):
        raise ValueError("SLOT_ALIGNMENT_INVALID")
    return int(seconds // (slot_minutes * 60))


def load_solver_fixture(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fixture_file:
        fixture = json.load(fixture_file)
    if fixture.get("schema_version") != "field-scheduler-solver-fixture/v1":
        raise ValueError("Unsupported solver fixture schema")
    return fixture


def solve_fixture(fixture: dict[str, Any], max_seconds: float = 10) -> EngineResult:
    try:
        snapshot = snapshot_from_fixture(fixture)
    except SnapshotValidationError as error:
        return EngineResult("INPUT_INVALID", False, (), error.issues)
    try:
        policy = FarmPolicy(fixture)
    except (KeyError, ValueError, TypeError) as error:
        return EngineResult("INPUT_INVALID", False, (), (f"FARM_POLICY:{error}",))
    slot_minutes = snapshot.horizon.slot_minutes
    horizon_start = snapshot.horizon.start_at
    horizon_slots = snapshot.horizon.slots
    model = cp_model.CpModel()
    operations = snapshot.operations
    windows = expand_utc_windows(snapshot.allowed_work_windows, horizon_start, horizon_slots, slot_minutes)
    as_of_slot = slot_ceil(snapshot.horizon.as_of, horizon_start, slot_minutes)
    resource_windows = expand_resource_windows(snapshot)

    choices: dict[str, tuple[Any, Any, Any, int]] = {}
    starts_by_op = {}
    intervals_by_resource: dict[str, list[Any]] = {}
    intervals_by_field: dict[str, list[Any]] = {}
    for operation in operations:
        op_id = str(operation.id)
        duration_minutes = operation.duration_minutes
        duration_slots = (duration_minutes + slot_minutes - 1) // slot_minutes
        earliest = operation.earliest_start_slot
        bundle = operation.bundle
        operation_windows = windows
        resource_ids = (*bundle.worker_ids, *bundle.machine_ids, *bundle.equipment_ids)
        for resource_id in resource_ids:
            resource_key = str(resource_id)
            if snapshot.resource_calendars and resource_key not in resource_windows:
                return EngineResult("INPUT_INVALID", False, (), (f"RESOURCE_CALENDAR_MISSING:{resource_key}",))
            if resource_key in resource_windows:
                available = expand_utc_windows(resource_windows[resource_key], horizon_start, horizon_slots, slot_minutes)
                operation_windows = intersect_slot_windows(operation_windows, available)
        starts = feasible_starts(operation_windows, duration_slots, earliest, as_of_slot, horizon_slots)
        starts = tuple(s for s in starts if policy.allowed(op_id, s, duration_slots))
        starts_by_op[op_id] = starts
        if not starts and operation.mandatory:
            return EngineResult("INFEASIBLE", False, (), (f"NO_FEASIBLE_START:{op_id}",))
        scheduled = model.new_bool_var(f"scheduled_{op_id}")
        start_var = model.new_int_var_from_domain(cp_model.Domain.from_values(starts) if starts else cp_model.Domain.from_values([0]), f"start_{op_id}")
        end_var = model.new_int_var(0, horizon_slots, f"end_{op_id}")
        interval = model.new_optional_interval_var(start_var, duration_slots, end_var, scheduled, f"interval_{op_id}")
        if operation.mandatory:
            model.add(scheduled == 1)
        else:
            model.add(scheduled == 0) if not starts else None
        choices[op_id] = (scheduled, start_var, end_var, duration_slots)
        for resource_id in (*bundle.worker_ids, *bundle.machine_ids, *bundle.equipment_ids):
            intervals_by_resource.setdefault(str(resource_id), []).append(interval)
        intervals_by_field.setdefault(str(operation.field_id), []).append(interval)

    for resource_intervals in intervals_by_resource.values():
        model.add_no_overlap(resource_intervals)
    for field_intervals in intervals_by_field.values():
        model.add_no_overlap(field_intervals)

    for dependency in snapshot.dependencies:
        predecessor = choices[str(dependency.predecessor_id)]
        successor = choices[str(dependency.successor_id)]
        lag_slots = (dependency.min_lag_minutes + slot_minutes - 1) // slot_minutes
        model.add(successor[0] <= predecessor[0])
        model.add(successor[1] >= predecessor[2] + lag_slots).only_enforce_if(successor[0])
        if dependency.max_lag_minutes is not None:
            maximum_lag_slots = dependency.max_lag_minutes // slot_minutes
            model.add(successor[1] <= predecessor[2] + maximum_lag_slots).only_enforce_if(successor[0])

    switch_cost = policy.constrain(model, choices, starts_by_op, horizon_slots)
    # Scheduling optional work takes precedence over start times and switching cost.
    weight = len(choices) * (horizon_slots * slot_minutes + 100000) + 1
    model.minimize(sum(choice[1] * slot_minutes for choice in choices.values()) + switch_cost
                   + weight * sum(1 - choice[0] for choice in choices.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_seconds
    status = solver.solve(model)
    status_name = solver.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return EngineResult(status_name, False, (), ("SOLVER_NO_SOLUTION",))

    result: list[Assignment] = []
    operation_by_id = {str(operation.id): operation for operation in operations}
    for op_id, (scheduled, start_var, end_var, _) in choices.items():
        if not solver.boolean_value(scheduled):
            continue
        operation = operation_by_id[op_id]
        bundle = operation.bundle
        result.append(Assignment(op_id, str(operation.field_id), solver.value(start_var), solver.value(end_var), tuple(str(value) for value in bundle.worker_ids), tuple(str(value) for value in bundle.machine_ids), tuple(str(value) for value in bundle.equipment_ids)))
    issues = validate_fixture_schedule(fixture, result)
    issues.append("NOT_IMPLEMENTED:TRAVEL_CERTIFICATE")
    return EngineResult(status_name, not issues, tuple(result), tuple(issues))


def validate_fixture_schedule(fixture: dict[str, Any], assignments: list[Assignment] | tuple[Assignment, ...]) -> list[str]:
    issues: list[str] = []
    operations = {operation["id"]: operation for operation in fixture["operations"]}
    by_id = {assignment.operation_id: assignment for assignment in assignments}
    try:
        snapshot = snapshot_from_fixture(fixture)
        resource_windows = expand_resource_windows(snapshot)
    except SnapshotValidationError:
        return ["INPUT_INVALID"]
    horizon_start = _parse_instant(fixture["horizon"]["start_at"])
    slot_minutes = fixture["horizon"]["slot_minutes"]
    slot_seconds = slot_minutes * 60
    windows = [
        (max(0, ceil((_parse_instant(start) - horizon_start).total_seconds() / slot_seconds)),
         min(fixture["horizon"]["horizon_hours"] * 60 // slot_minutes, floor((_parse_instant(end) - horizon_start).total_seconds() / slot_seconds)))
        for start, end in fixture["allowed_work_windows_utc"]
    ]
    as_of_slot = ceil((_parse_instant(fixture["horizon"]["as_of"]) - horizon_start).total_seconds() / slot_seconds)
    for operation in operations.values():
        assignment = by_id.get(operation["id"])
        if assignment is None:
            if operation.get("mandatory", True):
                issues.append(f"MANDATORY_OPERATION_MISSING:{operation['id']}")
            continue
        if assignment.end_slot - assignment.start_slot != (operation["duration_minutes"] + slot_minutes - 1) // slot_minutes:
            issues.append(f"DURATION_MISMATCH:{operation['id']}")
        if assignment.start_slot < as_of_slot:
            issues.append(f"WORK_BEFORE_AS_OF:{operation['id']}")
        if not any(start <= assignment.start_slot and assignment.end_slot <= end for start, end in windows):
            issues.append(f"OUTSIDE_WORK_WINDOW:{operation['id']}")
        bundle_ids = (*assignment.worker_ids, *assignment.machine_ids, *assignment.equipment_ids)
        for resource_id in bundle_ids:
            if snapshot.resource_calendars and resource_id not in resource_windows:
                issues.append(f"RESOURCE_CALENDAR_MISSING:{resource_id}")
                continue
            if resource_id in resource_windows:
                resource_slots = [
                    (max(0, ceil((start - horizon_start).total_seconds() / slot_seconds)),
                     min(fixture["horizon"]["horizon_hours"] * 60 // slot_minutes, floor((end - horizon_start).total_seconds() / slot_seconds)))
                    for start, end in resource_windows[resource_id]
                ]
                if not any(start <= assignment.start_slot and assignment.end_slot <= end for start, end in resource_slots):
                    issues.append(f"RESOURCE_OUTSIDE_AVAILABILITY:{resource_id}:{operation['id']}")
    resource_use: dict[str, list[Assignment]] = {}
    field_use: dict[str, list[Assignment]] = {}
    for assignment in assignments:
        for resource_id in (*assignment.worker_ids, *assignment.machine_ids, *assignment.equipment_ids):
            resource_use.setdefault(resource_id, []).append(assignment)
        field_use.setdefault(assignment.field_id, []).append(assignment)
    for label, groups in (("RESOURCE_OVERLAP", resource_use), ("FIELD_OVERLAP", field_use)):
        for entity_id, items in groups.items():
            ordered = sorted(items, key=lambda item: item.start_slot)
            for left, right in zip(ordered, ordered[1:]):
                if left.end_slot > right.start_slot:
                    issues.append(f"{label}:{entity_id}:{left.operation_id}:{right.operation_id}")
    for dependency in fixture.get("dependencies", []):
        predecessor = by_id.get(dependency["predecessor_id"])
        successor = by_id.get(dependency["successor_id"])
        if successor and predecessor is None:
            issues.append(f"DEPENDENCY_VIOLATION:{dependency['predecessor_id']}:{dependency['successor_id']}")
        elif successor and predecessor:
            actual_lag_minutes = (successor.start_slot - predecessor.end_slot) * slot_minutes
            if actual_lag_minutes < dependency["min_lag_minutes"]:
                issues.append(f"DEPENDENCY_VIOLATION:{dependency['predecessor_id']}:{dependency['successor_id']}")
            if dependency.get("max_lag_minutes") is not None and actual_lag_minutes > dependency["max_lag_minutes"]:
                issues.append(f"MAX_LAG_VIOLATION:{dependency['predecessor_id']}:{dependency['successor_id']}")
    try:
        issues.extend(FarmPolicy(fixture).validate(assignments))
    except (KeyError, ValueError, TypeError) as error:
        issues.append(f"FARM_POLICY:{error}")
    return issues
