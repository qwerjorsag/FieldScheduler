from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence
from uuid import UUID


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    issues: tuple[str, ...]


def validate_selected_options(
    operations: Sequence[object],
    dependencies: Sequence[object],
    assignments: Sequence[object],
    slot_minutes: int,
    horizon_slots: int,
) -> ValidationReport:
    issues: list[str] = []
    operation_by_id = {operation.operation_id: operation for operation in operations}
    assigned_by_id: dict[UUID, list[object]] = {}
    for assignment in assignments:
        assigned_by_id.setdefault(assignment.operation_id, []).append(assignment)

    for operation_id, operation in operation_by_id.items():
        selected = assigned_by_id.get(operation_id, [])
        if len(selected) > 1:
            issues.append(f"MULTIPLE_BUNDLES_SELECTED:{operation_id}")
        if operation.mandatory and len(selected) != 1:
            issues.append(f"MANDATORY_OPERATION_NOT_SCHEDULED:{operation_id}")
        if not selected:
            continue
        assignment = selected[0]
        matching = [
            option
            for option in operation.candidates
            if option.bundle.id == assignment.bundle_id
        ]
        if len(matching) != 1:
            issues.append(f"UNKNOWN_OR_AMBIGUOUS_BUNDLE:{operation_id}:{assignment.bundle_id}")
            continue
        bundle = matching[0].bundle
        if bundle.operation_id != operation_id:
            issues.append(f"BUNDLE_OPERATION_MISMATCH:{operation_id}")
        expected_sources = (bundle.worker_ids, bundle.machine_ids, bundle.equipment_ids)
        actual_sources = (assignment.worker_ids, assignment.machine_ids, assignment.equipment_ids)
        if expected_sources != actual_sources:
            issues.append(f"BUNDLE_RESOURCE_MISMATCH:{operation_id}")
        if assignment.area_ha != str(bundle.area_ha):
            issues.append(f"AREA_MISMATCH:{operation_id}")
        if assignment.estimated_cost_cents != bundle.estimated_cost_cents:
            issues.append(f"COST_MISMATCH:{operation_id}")
        duration = assignment.end_slot - assignment.start_slot
        if duration != bundle.duration_slots:
            issues.append(f"DURATION_MISMATCH:{operation_id}")
        if bundle.duration_minutes <= 0 or bundle.duration_slots <= 0 or bundle.area_ha <= 0:
            issues.append(f"BUNDLE_INVALID_DOMAIN_VALUE:{operation_id}")
        if assignment.start_slot not in matching[0].feasible_start_slots:
            issues.append(f"START_NOT_FEASIBLE_FOR_BUNDLE:{operation_id}")
        if assignment.start_slot < 0 or assignment.end_slot > horizon_slots or assignment.end_slot <= assignment.start_slot:
            issues.append(f"INTERVAL_OUTSIDE_HORIZON:{operation_id}")
        if assignment.field_id != operation.field_id:
            issues.append(f"FIELD_MISMATCH:{operation_id}")

    for assignment_id in set(assigned_by_id) - set(operation_by_id):
        issues.append(f"UNKNOWN_OPERATION_ASSIGNMENT:{assignment_id}")

    resource_intervals: dict[UUID, list[object]] = {}
    field_intervals: dict[UUID, list[object]] = {}
    for assignment in assignments:
        for resource_id in (*assignment.worker_ids, *assignment.machine_ids, *assignment.equipment_ids):
            resource_intervals.setdefault(resource_id, []).append(assignment)
        field_intervals.setdefault(assignment.field_id, []).append(assignment)
    for resource_id, intervals in resource_intervals.items():
        ordered = sorted(intervals, key=lambda item: (item.start_slot, item.end_slot))
        for earlier, later in zip(ordered, ordered[1:]):
            if earlier.end_slot > later.start_slot:
                issues.append(f"RESOURCE_OVERLAP:{resource_id}:{earlier.operation_id}:{later.operation_id}")
    for field_id, intervals in field_intervals.items():
        ordered = sorted(intervals, key=lambda item: (item.start_slot, item.end_slot))
        for earlier, later in zip(ordered, ordered[1:]):
            if earlier.end_slot > later.start_slot:
                issues.append(f"FIELD_OVERLAP:{field_id}:{earlier.operation_id}:{later.operation_id}")

    assignment_for = {operation_id: items[0] for operation_id, items in assigned_by_id.items() if len(items) == 1}
    for dependency in dependencies:
        predecessor = assignment_for.get(dependency.predecessor_id)
        successor = assignment_for.get(dependency.successor_id)
        if successor is None:
            continue
        if predecessor is None:
            issues.append(f"PREDECESSOR_NOT_SCHEDULED:{dependency.predecessor_id}:{dependency.successor_id}")
            continue
        actual_lag_minutes = (successor.start_slot - predecessor.end_slot) * slot_minutes
        if actual_lag_minutes < dependency.min_lag_minutes:
            issues.append(f"DEPENDENCY_VIOLATION:{dependency.predecessor_id}:{dependency.successor_id}")
        if dependency.max_lag_minutes is not None and actual_lag_minutes > dependency.max_lag_minutes:
            issues.append(f"MAX_LAG_VIOLATION:{dependency.predecessor_id}:{dependency.successor_id}")
    return ValidationReport(not issues, tuple(issues))


def validate_work_unit_schedule(
    work_units: Sequence[object],
    dependencies: Sequence[object],
    assignments: Sequence[object],
    operation_area_totals: dict[UUID, object],
    slot_minutes: int,
    horizon_slots: int,
    dispositions: Sequence[object] = (),
    area_balances: Sequence[object] = (),
) -> ValidationReport:
    issues: list[str] = []
    unit_by_id = {item.unit.id: item for item in work_units}
    assignment_by_unit: dict[UUID, list[object]] = {}
    for assignment in assignments:
        if assignment.work_unit_id is None:
            issues.append(f"WORK_UNIT_ID_MISSING:{assignment.operation_id}")
            continue
        assignment_by_unit.setdefault(assignment.work_unit_id, []).append(assignment)
    state_by_unit = {item.work_unit_id: item.state for item in dispositions}
    if set(state_by_unit) != set(unit_by_id):
        issues.append("WORK_UNIT_DISPOSITION_SET_MISMATCH")
    for unit_id, item in unit_by_id.items():
        selected = assignment_by_unit.get(unit_id, [])
        state = state_by_unit.get(unit_id)
        if state not in {"SCHEDULED", "DEFERRED", "BEYOND_HORIZON"}:
            issues.append(f"WORK_UNIT_DISPOSITION_INVALID:{unit_id}")
            continue
        if state == "SCHEDULED" and len(selected) != 1:
            issues.append(f"WORK_UNIT_ASSIGNMENT_COUNT:{unit_id}:{len(selected)}")
            continue
        if state != "SCHEDULED" and selected:
            issues.append(f"UNSCHEDULED_UNIT_HAS_ASSIGNMENT:{unit_id}")
        if item.mandatory and state != "SCHEDULED":
            issues.append(f"MANDATORY_UNIT_NOT_SCHEDULED:{unit_id}")
        if state == "DEFERRED" and not item.allow_deferred:
            issues.append(f"DEFERRED_UNIT_NOT_ALLOWED:{unit_id}")
        if state == "BEYOND_HORIZON" and not item.allow_beyond_horizon:
            issues.append(f"BEYOND_HORIZON_NOT_ALLOWED:{unit_id}")
        if state != "SCHEDULED":
            continue
        assignment = selected[0]
        matches = [option for option in item.candidates if option.bundle.id == assignment.bundle_id]
        if len(matches) != 1:
            issues.append(f"UNKNOWN_WORK_UNIT_BUNDLE:{unit_id}")
            continue
        bundle = matches[0].bundle
        if assignment.operation_id != item.unit.operation_id or bundle.operation_id != item.unit.operation_id:
            issues.append(f"WORK_UNIT_OPERATION_MISMATCH:{unit_id}")
        if assignment.area_ha != str(item.unit.area_ha) or bundle.area_ha != item.unit.area_ha:
            issues.append(f"WORK_UNIT_AREA_MISMATCH:{unit_id}")
        if assignment.end_slot - assignment.start_slot != bundle.duration_slots:
            issues.append(f"WORK_UNIT_DURATION_MISMATCH:{unit_id}")
        if assignment.start_slot not in matches[0].feasible_start_slots:
            issues.append(f"WORK_UNIT_START_INVALID:{unit_id}")
        if assignment.start_slot < 0 or assignment.end_slot > horizon_slots:
            issues.append(f"WORK_UNIT_OUTSIDE_HORIZON:{unit_id}")
        if (assignment.worker_ids, assignment.machine_ids, assignment.equipment_ids) != (bundle.worker_ids, bundle.machine_ids, bundle.equipment_ids):
            issues.append(f"WORK_UNIT_RESOURCE_MISMATCH:{unit_id}")

    if set(assignment_by_unit) - set(unit_by_id):
        issues.extend(f"UNKNOWN_WORK_UNIT_ASSIGNMENT:{unit_id}" for unit_id in set(assignment_by_unit) - set(unit_by_id))
    by_operation: dict[UUID, list[object]] = {}
    for item in work_units:
        by_operation.setdefault(item.unit.operation_id, []).append(item)
    for operation_id, items in by_operation.items():
        if operation_id not in operation_area_totals:
            issues.append(f"AREA_BALANCE_INVALID:{operation_id}")
        ordinals = [item.unit.ordinal for item in items]
        if len(ordinals) != len(set(ordinals)):
            issues.append(f"WORK_UNIT_ORDINAL_DUPLICATE:{operation_id}")
        if not items[0].allow_parallel_work_units:
            scheduled = sorted((assignment_by_unit[item.unit.id][0] for item in items if len(assignment_by_unit.get(item.unit.id, [])) == 1), key=lambda assignment: next(item.unit.ordinal for item in items if item.unit.id == assignment.work_unit_id))
            for earlier, later in zip(scheduled, scheduled[1:]):
                if earlier.end_slot > later.start_slot:
                    issues.append(f"WORK_UNIT_ORDER_VIOLATION:{operation_id}")
        ordered_items = sorted(items, key=lambda value: value.unit.ordinal)
        states = [state_by_unit.get(item.unit.id) for item in ordered_items]
        state_order = {"SCHEDULED": 0, "DEFERRED": 1, "BEYOND_HORIZON": 2}
        ordered_states = [state_order.get(state, -1) for state in states]
        if ordered_states != sorted(ordered_states):
            issues.append(f"WORK_UNIT_STATUS_PREFIX_SUFFIX:{operation_id}")

    if set(operation_area_totals) != set(by_operation):
        issues.append("AREA_BALANCE_OPERATION_SET_MISMATCH")
    balances_by_operation = {item.operation_id: item for item in area_balances}
    if set(balances_by_operation) != set(operation_area_totals):
        issues.append("AREA_BALANCE_RESULT_SET_MISMATCH")
    for operation_id, balance in balances_by_operation.items():
        if not balance.is_balanced or balance.total_area_ha != operation_area_totals[operation_id]:
            issues.append(f"AREA_BALANCE_INVALID:{operation_id}")
        units = by_operation.get(operation_id, [])
        planned = sum((item.unit.area_ha for item in units if state_by_unit.get(item.unit.id) == "SCHEDULED"), start=Decimal("0"))
        deferred = sum((item.unit.area_ha for item in units if state_by_unit.get(item.unit.id) == "DEFERRED"), start=Decimal("0"))
        beyond = sum((item.unit.area_ha for item in units if state_by_unit.get(item.unit.id) == "BEYOND_HORIZON"), start=Decimal("0"))
        if (planned, deferred, beyond) != (balance.newly_planned_ha, balance.deferred_ha, balance.beyond_horizon_ha):
            issues.append(f"AREA_BALANCE_CATEGORY_MISMATCH:{operation_id}")
    resource_intervals: dict[UUID, list[object]] = {}
    field_intervals: dict[UUID, list[object]] = {}
    for assignment in assignments:
        for resource_id in (*assignment.worker_ids, *assignment.machine_ids, *assignment.equipment_ids):
            resource_intervals.setdefault(resource_id, []).append(assignment)
        field_intervals.setdefault(assignment.field_id, []).append(assignment)
    for entity_id, intervals in resource_intervals.items():
        ordered = sorted(intervals, key=lambda value: (value.start_slot, value.end_slot))
        for first, second in zip(ordered, ordered[1:]):
            if first.end_slot > second.start_slot:
                issues.append(f"RESOURCE_OVERLAP:{entity_id}:{first.operation_id}:{second.operation_id}")
    for field_id, intervals in field_intervals.items():
        by_field_operation: dict[UUID, list[object]] = {}
        for assignment in intervals:
            by_field_operation.setdefault(assignment.operation_id, []).append(assignment)
        operation_ids = list(by_field_operation)
        for index, first_operation in enumerate(operation_ids):
            first_units = by_field_operation[first_operation]
            first_policy = next(item for item in work_units if item.unit.operation_id == first_operation)
            if first_policy.allow_parallel_work_units:
                events = [(item.start_slot, 1) for item in first_units] + [(item.end_slot, -1) for item in first_units]
                concurrent = 0
                for _, delta in sorted(events):
                    concurrent += delta
                    if concurrent > first_policy.field_parallel_capacity:
                        issues.append(f"FIELD_PARALLEL_CAPACITY:{field_id}:{first_operation}")
                        break
            else:
                ordered = sorted(first_units, key=lambda value: (value.start_slot, value.end_slot))
                for first, second in zip(ordered, ordered[1:]):
                    if first.end_slot > second.start_slot:
                        issues.append(f"FIELD_OVERLAP:{field_id}:{first.operation_id}:{second.operation_id}")
            for second_operation in operation_ids[index + 1 :]:
                for first in first_units:
                    for second in by_field_operation[second_operation]:
                        if first.start_slot < second.end_slot and second.start_slot < first.end_slot:
                            issues.append(f"FIELD_OVERLAP:{field_id}:{first_operation}:{second_operation}")

    operation_assignments: dict[UUID, list[object]] = {}
    for assignment in assignments:
        operation_assignments.setdefault(assignment.operation_id, []).append(assignment)
    for dependency in dependencies:
        predecessors = operation_assignments.get(dependency.predecessor_id, [])
        successors = operation_assignments.get(dependency.successor_id, [])
        if not successors:
            continue
        if not predecessors:
            issues.append(f"PREDECESSOR_NOT_SCHEDULED:{dependency.predecessor_id}:{dependency.successor_id}")
            continue
        actual_lag = (min(value.start_slot for value in successors) - max(value.end_slot for value in predecessors)) * slot_minutes
        if actual_lag < dependency.min_lag_minutes:
            issues.append(f"DEPENDENCY_VIOLATION:{dependency.predecessor_id}:{dependency.successor_id}")
        if dependency.max_lag_minutes is not None and actual_lag > dependency.max_lag_minutes:
            issues.append(f"MAX_LAG_VIOLATION:{dependency.predecessor_id}:{dependency.successor_id}")
    return ValidationReport(not issues, tuple(issues))
