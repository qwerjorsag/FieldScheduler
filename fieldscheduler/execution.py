from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Sequence
from uuid import UUID


WorkLogType = Literal["START", "AREA_DELTA", "FINISH", "CORRECTION"]


@dataclass(frozen=True)
class AssignmentExecutionFact:
    id: UUID
    source_assignment_id: UUID
    operation_id: UUID
    reserved_area_ha: Decimal
    started_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True)
class WorkLogFact:
    id: UUID
    assignment_execution_id: UUID
    event_type: WorkLogType
    occurred_at: datetime
    recorded_at: datetime
    area_delta_ha: Decimal | None = None
    correction_of_id: UUID | None = None
    future_authorized: bool = False
    future_reason: str | None = None


@dataclass(frozen=True)
class FixedAreaReservation:
    id: UUID
    source_assignment_id: UUID
    operation_id: UUID
    area_ha: Decimal


@dataclass(frozen=True)
class ExecutionState:
    execution_id: UUID
    source_assignment_id: UUID
    operation_id: UUID
    state: Literal["PLANNED", "IN_PROGRESS", "CLOSED"]
    completed_area_ha: Decimal
    reserved_area_remaining_ha: Decimal
    last_event_at: datetime | None


@dataclass(frozen=True)
class ExecutionReconciliation:
    valid: bool
    states: tuple[ExecutionState, ...]
    completed_area_by_operation: tuple[tuple[UUID, Decimal], ...]
    reserved_area_by_operation: tuple[tuple[UUID, Decimal], ...]
    latest_reality_at: datetime | None
    diagnostics: tuple[str, ...]

    def area_context(
        self,
        operation_area_totals: dict[UUID, Decimal],
        additional_reservations: Sequence[FixedAreaReservation] = (),
    ) -> dict[UUID, tuple[Decimal, Decimal, Decimal]]:
        if not self.valid:
            raise ValueError("EXECUTION_FACTS_INVALID:" + ";".join(self.diagnostics))
        completed = dict(self.completed_area_by_operation)
        reserved = dict(self.reserved_area_by_operation)
        source_assignment_ids = {state.source_assignment_id for state in self.states if state.reserved_area_remaining_ha > 0}
        additional_source_ids: set[UUID] = set()
        reservation_ids: set[UUID] = set()
        extra: dict[UUID, Decimal] = {}
        for reservation in additional_reservations:
            if reservation.id in reservation_ids:
                raise ValueError(f"AREA_BALANCE_INVALID:DUPLICATE_RESERVATION:{reservation.id}")
            reservation_ids.add(reservation.id)
            if reservation.source_assignment_id in source_assignment_ids:
                raise ValueError(f"AREA_BALANCE_INVALID:EXECUTION_RESERVATION_DUPLICATE:{reservation.source_assignment_id}")
            if reservation.source_assignment_id in additional_source_ids:
                raise ValueError(f"AREA_BALANCE_INVALID:DUPLICATE_SOURCE_RESERVATION:{reservation.source_assignment_id}")
            additional_source_ids.add(reservation.source_assignment_id)
            if not reservation.area_ha.is_finite() or reservation.area_ha <= 0:
                raise ValueError(f"AREA_BALANCE_INVALID:RESERVATION_AREA:{reservation.id}")
            extra[reservation.operation_id] = extra.get(reservation.operation_id, Decimal("0")) + reservation.area_ha
        result: dict[UUID, tuple[Decimal, Decimal, Decimal]] = {}
        for operation_id, total in operation_area_totals.items():
            if not total.is_finite() or total < 0:
                raise ValueError(f"AREA_BALANCE_INVALID:TOTAL:{operation_id}")
            completed_area = completed.get(operation_id, Decimal("0"))
            reserved_area = reserved.get(operation_id, Decimal("0")) + extra.get(operation_id, Decimal("0"))
            if completed_area < 0 or reserved_area < 0 or completed_area + reserved_area > total:
                raise ValueError(f"AREA_BALANCE_INVALID:{operation_id}")
            result[operation_id] = (total, completed_area, reserved_area)
        unknown_operations = (set(completed) | set(reserved) | set(extra)) - set(operation_area_totals)
        if unknown_operations:
            raise ValueError("AREA_BALANCE_INVALID:UNKNOWN_OPERATION:" + ",".join(sorted(map(str, unknown_operations))))
        return result


def execution_facts_from_seed(
    entities: dict[str, Any],
    farm_id: UUID,
) -> tuple[tuple[AssignmentExecutionFact, ...], tuple[WorkLogFact, ...]]:
    """Parse immutable execution facts from the normalized seed entity bundle."""
    farm_key = str(farm_id)

    def instant(value: object, path: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"INPUT_INVALID:{path}:DATETIME") from error
        if parsed.utcoffset() is None:
            raise ValueError(f"INPUT_INVALID:{path}:AWARE_DATETIME_REQUIRED")
        return parsed

    def required_instant(value: object, path: str) -> datetime:
        parsed = instant(value, path)
        if parsed is None:
            raise ValueError(f"INPUT_INVALID:{path}:REQUIRED")
        return parsed

    executions: list[AssignmentExecutionFact] = []
    for index, raw in enumerate(entities.get("assignment_executions", [])):
        if raw.get("farm_id") != farm_key:
            continue
        path = f"assignment_executions[{index}]"
        try:
            executions.append(AssignmentExecutionFact(
                UUID(raw["id"]), UUID(raw["source_assignment_id"]), UUID(raw["operation_id"]),
                Decimal(str(raw["reserved_area_ha"])), instant(raw.get("started_at"), f"{path}.started_at"),
                instant(raw.get("closed_at"), f"{path}.closed_at"),
            ))
        except (KeyError, ValueError, ArithmeticError) as error:
            if isinstance(error, ValueError) and str(error).startswith("INPUT_INVALID:"):
                raise
            raise ValueError(f"INPUT_INVALID:{path}:EXECUTION_FACT") from error

    logs: list[WorkLogFact] = []
    for index, raw in enumerate(entities.get("work_logs", [])):
        if raw.get("farm_id") != farm_key:
            continue
        path = f"work_logs[{index}]"
        try:
            logs.append(WorkLogFact(
                UUID(raw["id"]), UUID(raw["assignment_execution_id"]), raw["event_type"],
                required_instant(raw.get("occurred_at"), f"{path}.occurred_at"), required_instant(raw.get("recorded_at"), f"{path}.recorded_at"),
                Decimal(str(raw["area_delta_ha"])) if raw.get("area_delta_ha") is not None else None,
                UUID(raw["correction_of_id"]) if raw.get("correction_of_id") else None,
                bool(raw.get("future_authorized", False)), raw.get("future_reason"),
            ))
        except (KeyError, ValueError, ArithmeticError) as error:
            if isinstance(error, ValueError) and str(error).startswith("INPUT_INVALID:"):
                raise
            raise ValueError(f"INPUT_INVALID:{path}:WORK_LOG_FACT") from error
    return tuple(executions), tuple(logs)


def reconcile_execution_facts(
    executions: Sequence[AssignmentExecutionFact],
    work_logs: Sequence[WorkLogFact],
    as_of: datetime,
) -> ExecutionReconciliation:
    """Deduplicate the append-only ledger and derive completion/reservations.

    CORRECTION.area_delta_ha is the replacement value for its correction target;
    it does not erase either immutable row. A FINISH event closes the reservation
    but never infers completion of the unreported area.
    """
    diagnostics: set[str] = set()
    if as_of.utcoffset() is None:
        return ExecutionReconciliation(False, (), (), (), None, ("INPUT_INVALID:AS_OF_MUST_BE_AWARE",))
    as_of_utc = as_of.astimezone(timezone.utc)

    execution_by_id: dict[UUID, AssignmentExecutionFact] = {}
    execution_by_assignment: dict[UUID, UUID] = {}
    for execution in executions:
        previous = execution_by_id.get(execution.id)
        if previous is not None and previous != execution:
            diagnostics.add(f"EXECUTION_FACT_CONFLICT:{execution.id}")
        execution_by_id[execution.id] = execution
        previous_execution_id = execution_by_assignment.get(execution.source_assignment_id)
        if previous_execution_id is not None and previous_execution_id != execution.id:
            diagnostics.add(f"EXECUTION_SOURCE_ASSIGNMENT_DUPLICATE:{execution.source_assignment_id}")
        execution_by_assignment[execution.source_assignment_id] = execution.id
        if not execution.reserved_area_ha.is_finite() or execution.reserved_area_ha <= 0:
            diagnostics.add(f"EXECUTION_RESERVED_AREA_INVALID:{execution.id}")
        if execution.started_at and execution.closed_at and execution.closed_at < execution.started_at:
            diagnostics.add(f"EXECUTION_TIME_ORDER_INVALID:{execution.id}")
        for instant in (execution.started_at, execution.closed_at):
            if instant is not None and instant.utcoffset() is None:
                diagnostics.add(f"EXECUTION_TIME_INVALID:{execution.id}")
            elif instant is not None and instant.astimezone(timezone.utc) > as_of_utc:
                diagnostics.add(f"EXECUTION_FUTURE_FACT:{execution.id}")

    log_by_id: dict[UUID, WorkLogFact] = {}
    for log in work_logs:
        previous = log_by_id.get(log.id)
        if previous is not None and previous != log:
            diagnostics.add(f"WORK_LOG_ID_CONFLICT:{log.id}")
        log_by_id[log.id] = log
    logs_by_execution: dict[UUID, list[WorkLogFact]] = {}
    for log in log_by_id.values():
        execution = execution_by_id.get(log.assignment_execution_id)
        if execution is None:
            diagnostics.add(f"WORK_LOG_EXECUTION_MISSING:{log.id}")
            continue
        if log.occurred_at.utcoffset() is None or log.recorded_at.utcoffset() is None:
            diagnostics.add(f"WORK_LOG_TIME_INVALID:{log.id}")
            continue
        if log.event_type not in {"START", "AREA_DELTA", "FINISH", "CORRECTION"}:
            diagnostics.add(f"WORK_LOG_TYPE_INVALID:{log.id}")
            continue
        occurred = log.occurred_at.astimezone(timezone.utc)
        if occurred > as_of_utc and not (log.future_authorized and log.future_reason and log.future_reason.strip()):
            diagnostics.add(f"WORK_LOG_FUTURE_EVENT:{log.id}")
        if log.recorded_at.astimezone(timezone.utc) > as_of_utc:
            diagnostics.add(f"WORK_LOG_NOT_KNOWN_AS_OF:{log.id}")
        if log.event_type in {"AREA_DELTA", "CORRECTION"}:
            if log.area_delta_ha is None:
                diagnostics.add(f"WORK_LOG_AREA_MISSING:{log.id}")
            elif not log.area_delta_ha.is_finite():
                diagnostics.add(f"WORK_LOG_AREA_INVALID:{log.id}")
            elif log.event_type == "AREA_DELTA" and log.area_delta_ha == 0:
                diagnostics.add(f"WORK_LOG_AREA_ZERO:{log.id}")
        elif log.area_delta_ha is not None:
            diagnostics.add(f"WORK_LOG_AREA_UNEXPECTED:{log.id}")
        if log.event_type == "CORRECTION" and log.correction_of_id is None:
            diagnostics.add(f"WORK_LOG_CORRECTION_TARGET_MISSING:{log.id}")
        if log.event_type != "CORRECTION" and log.correction_of_id is not None:
            diagnostics.add(f"WORK_LOG_CORRECTION_TARGET_UNEXPECTED:{log.id}")
        logs_by_execution.setdefault(log.assignment_execution_id, []).append(log)

    if diagnostics:
        return ExecutionReconciliation(False, (), (), (), None, tuple(sorted(diagnostics)))

    completed_by_execution: dict[UUID, Decimal] = {}
    state_by_execution: dict[UUID, str] = {}
    last_event_by_execution: dict[UUID, datetime | None] = {}
    for execution_id, execution_logs in logs_by_execution.items():
        by_event_id = {item.id: item for item in execution_logs}
        finish_times = [
            item.occurred_at.astimezone(timezone.utc)
            for item in execution_logs
            if item.event_type == "FINISH" and item.occurred_at.utcoffset() is not None
        ]
        if finish_times:
            first_finish = min(finish_times)
            if any(
                item.event_type in {"START", "AREA_DELTA"}
                and item.occurred_at.utcoffset() is not None
                and item.occurred_at.astimezone(timezone.utc) > first_finish
                for item in execution_logs
            ):
                diagnostics.add(f"WORK_LOG_AFTER_FINISH:{execution_id}")
        replacements: dict[UUID, WorkLogFact] = {}
        for log in execution_logs:
            if log.event_type != "CORRECTION" or log.correction_of_id is None:
                continue
            target = by_event_id.get(log.correction_of_id)
            if target is None or target.event_type != "AREA_DELTA":
                diagnostics.add(f"WORK_LOG_CORRECTION_TARGET_INVALID:{log.id}")
                continue
            if log.occurred_at.astimezone(timezone.utc) < target.occurred_at.astimezone(timezone.utc):
                diagnostics.add(f"WORK_LOG_CORRECTION_ORDER_INVALID:{log.id}")
            if target.id in replacements:
                diagnostics.add(f"WORK_LOG_MULTIPLE_CORRECTIONS:{target.id}")
            replacements[target.id] = log

        def effective_delta(log: WorkLogFact) -> Decimal:
            replacement = replacements.get(log.id)
            if replacement is not None:
                if replacement.area_delta_ha is None or not replacement.area_delta_ha.is_finite():
                    return Decimal("0")
                return replacement.area_delta_ha
            if log.event_type == "CORRECTION":
                return Decimal("0")
            return log.area_delta_ha if log.area_delta_ha is not None and log.area_delta_ha.is_finite() else Decimal("0")

        completed = sum(
            (effective_delta(log) for log in execution_logs if log.event_type in {"AREA_DELTA", "CORRECTION"}),
            start=Decimal("0"),
        )
        if completed < 0 or completed > execution_by_id[execution_id].reserved_area_ha:
            diagnostics.add(f"WORK_LOG_AREA_OUT_OF_BOUNDS:{execution_id}")
        completed_by_execution[execution_id] = completed
        latest = max(execution_logs, key=lambda item: (item.occurred_at.astimezone(timezone.utc), item.recorded_at.astimezone(timezone.utc), str(item.id)))
        last_event_by_execution[execution_id] = latest.occurred_at.astimezone(timezone.utc)
        event_types = {item.event_type for item in execution_logs}
        is_closed = "FINISH" in event_types or execution_by_id[execution_id].closed_at is not None
        is_started = "START" in event_types or any(item.event_type in {"AREA_DELTA", "CORRECTION"} for item in execution_logs) or execution_by_id[execution_id].started_at is not None
        state_by_execution[execution_id] = "CLOSED" if is_closed else "IN_PROGRESS" if is_started else "PLANNED"

    state_rows: list[ExecutionState] = []
    completed_by_operation: dict[UUID, Decimal] = {}
    reserved_by_operation: dict[UUID, Decimal] = {}
    latest_reality: datetime | None = None
    for execution_id, execution in execution_by_id.items():
        completed = completed_by_execution.get(execution_id, Decimal("0"))
        state = state_by_execution.get(execution_id, "CLOSED" if execution.closed_at else "IN_PROGRESS" if execution.started_at else "PLANNED")
        remaining_reserved = max(Decimal("0"), execution.reserved_area_ha - completed) if state == "IN_PROGRESS" else Decimal("0")
        completed_by_operation[execution.operation_id] = completed_by_operation.get(execution.operation_id, Decimal("0")) + completed
        reserved_by_operation[execution.operation_id] = reserved_by_operation.get(execution.operation_id, Decimal("0")) + remaining_reserved
        last_event = last_event_by_execution.get(execution_id)
        execution_times = [
            instant.astimezone(timezone.utc)
            for instant in (execution.started_at, execution.closed_at)
            if instant is not None and instant.utcoffset() is not None
        ]
        if execution_times:
            execution_latest = max(execution_times)
            if last_event is None or execution_latest > last_event:
                last_event = execution_latest
        if last_event is not None and (latest_reality is None or last_event > latest_reality):
            latest_reality = last_event
        state_rows.append(ExecutionState(execution_id, execution.source_assignment_id, execution.operation_id, state, completed, remaining_reserved, last_event))

    diagnostics_tuple = tuple(sorted(diagnostics))
    return ExecutionReconciliation(
        not diagnostics_tuple,
        tuple(sorted(state_rows, key=lambda item: str(item.execution_id))),
        tuple(sorted(completed_by_operation.items(), key=lambda item: str(item[0]))),
        tuple(sorted(reserved_by_operation.items(), key=lambda item: str(item[0]))),
        latest_reality,
        diagnostics_tuple,
    )
