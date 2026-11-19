from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable, Literal
from datetime import datetime
from uuid import UUID

from ortools.sat.python import cp_model

from fieldscheduler.bundles import BundleCandidate
from fieldscheduler.validation import validate_selected_options, validate_work_unit_schedule
from fieldscheduler.work_units import AreaBalance, WorkUnit, adaptively_rechunk_work_units
from fieldscheduler.travel import TravelClosure


@dataclass(frozen=True)
class CandidateOption:
    bundle: BundleCandidate
    feasible_start_slots: tuple[int, ...]
    weather_status_by_start: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class OperationOptions:
    operation_id: UUID
    field_id: UUID
    mandatory: bool
    candidates: tuple[CandidateOption, ...]


@dataclass(frozen=True)
class WorkUnitOptions:
    unit: WorkUnit
    field_id: UUID
    mandatory: bool
    candidates: tuple[CandidateOption, ...]
    allow_parallel_work_units: bool = False
    field_parallel_capacity: int = 1
    candidate_truncated: bool = False
    diagnostics: tuple[str, ...] = ()
    allow_deferred: bool = False
    allow_beyond_horizon: bool = False
    heuristic_chunking: bool = False


@dataclass(frozen=True)
class DependencyConstraint:
    predecessor_id: UUID
    successor_id: UUID
    min_lag_minutes: int
    max_lag_minutes: int | None = None


@dataclass(frozen=True)
class SelectedOption:
    operation_id: UUID
    bundle_id: UUID
    field_id: UUID
    start_slot: int
    end_slot: int
    area_ha: str
    worker_ids: tuple[UUID, ...]
    machine_ids: tuple[UUID, ...]
    equipment_ids: tuple[UUID, ...]
    estimated_cost_cents: int
    work_unit_id: UUID | None = None
    weather_status: str = "NOT_ASSESSED"


@dataclass(frozen=True)
class ObjectiveStage:
    name: str
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"]
    value: int | None
    best_bound: int | None


@dataclass(frozen=True)
class UnitDisposition:
    work_unit_id: UUID
    state: Literal["SCHEDULED", "DEFERRED", "BEYOND_HORIZON"]


@dataclass(frozen=True)
class OptimizationResult:
    solver_status: str
    selected: tuple[SelectedOption, ...]
    objective_stages: tuple[ObjectiveStage, ...]
    search_scope: str
    validated: bool
    diagnostics: tuple[str, ...]
    unit_dispositions: tuple[UnitDisposition, ...] = ()
    area_balances: tuple[AreaBalance, ...] = ()
    travel_legs: tuple[Any, ...] = ()


def operation_options_from_seed(
    entities: dict,
    farm_id: UUID,
    candidates_by_operation: dict[UUID, tuple[CandidateOption, ...]],
) -> tuple[OperationOptions, ...]:
    cultivations = {item["id"]: item for item in entities.get("cultivations", []) if item["farm_id"] == str(farm_id)}
    operations: list[OperationOptions] = []
    for item in entities.get("field_operations", []):
        if item["farm_id"] != str(farm_id):
            continue
        cultivation = cultivations.get(item.get("cultivation_id"))
        if cultivation is None:
            raise ValueError(f"INPUT_INVALID:UNKNOWN_CULTIVATION:{item['id']}")
        operation_id = UUID(item["id"])
        operations.append(OperationOptions(operation_id, UUID(cultivation["field_id"]), bool(item.get("mandatory", True)), candidates_by_operation.get(operation_id, ())))
    return tuple(operations)


def work_unit_options_from_seed(
    entities: dict,
    work_units: tuple[WorkUnit, ...],
    field_by_operation: dict[UUID, UUID],
    feasible_starts_by_bundle: dict[UUID, tuple[int, ...]],
    operation_policy: dict[UUID, tuple[bool, int]],
    farm_id: UUID,
    horizon_start: Any,
    horizon_hours: int,
    slot_minutes: int = 60,
    max_bundles_per_unit: int = 20,
    feasible_start_provider: Callable[[BundleCandidate], tuple[int, ...]] | None = None,
    max_rechunk_depth: int = 2,
    weather_assessor: Callable[[UUID, datetime, datetime], Any] | None = None,
) -> tuple[WorkUnitOptions, ...]:
    from fieldscheduler.bundles import generate_bundles

    result: list[WorkUnitOptions] = []
    source_operations = {item["id"]: item for item in entities.get("field_operations", []) if item["farm_id"] == str(farm_id)}
    horizon_end = horizon_start + timedelta(hours=horizon_hours)
    cache: dict[UUID, tuple[Any, tuple[CandidateOption, ...]]] = {}
    weather_diagnostics_by_operation: dict[UUID, set[str]] = {}

    def options_for(unit: WorkUnit) -> tuple[Any, tuple[CandidateOption, ...]]:
        if unit.id in cache:
            return cache[unit.id]
        generation = generate_bundles(
            entities,
            unit.operation_id,
            farm_id,
            horizon_start,
            horizon_hours,
            slot_minutes,
            max_bundles_per_unit,
            area_override=unit.area_ha,
            identity_scope=unit.id,
        )
        generated_options: list[CandidateOption] = []
        for bundle in generation.candidates:
            starts = feasible_start_provider(bundle) if feasible_start_provider else feasible_starts_by_bundle.get(bundle.id, ())
            statuses: list[tuple[int, str]] = []
            if weather_assessor is not None:
                allowed_starts: list[int] = []
                for start_slot in starts:
                    starts_at = horizon_start + timedelta(minutes=start_slot * slot_minutes)
                    ends_at = starts_at + timedelta(minutes=bundle.duration_minutes)
                    assessment = weather_assessor(bundle.operation_id, starts_at, ends_at)
                    if assessment.allowed:
                        allowed_starts.append(start_slot)
                        statuses.append((start_slot, assessment.status))
                    else:
                        weather_diagnostics_by_operation.setdefault(bundle.operation_id, set()).update(assessment.diagnostics)
                starts = tuple(allowed_starts)
            generated_options.append(CandidateOption(bundle, tuple(starts), tuple(statuses)))
        options = tuple(generated_options)
        cache[unit.id] = generation, options
        return generation, options

    source_by_operation = {UUID(key): value for key, value in source_operations.items()}
    split_operations = frozenset(operation_id for operation_id, source in source_by_operation.items() if source.get("can_split"))
    minimums = {operation_id: Decimal(source.get("min_chunk_ha", "0.0001")) for operation_id, source in source_by_operation.items()}
    can_assess_fit = feasible_start_provider is not None or all(
        all(bundle.id in feasible_starts_by_bundle for bundle in options_for(unit)[0].candidates)
        for unit in work_units
    )

    def can_fit(unit: WorkUnit) -> bool | None:
        generation, options = options_for(unit)
        if not generation.candidates or not can_assess_fit:
            return None
        if feasible_start_provider is None and any(bundle.id not in feasible_starts_by_bundle for bundle in generation.candidates):
            return None
        return any(option.feasible_start_slots for option in options)

    rechunked = adaptively_rechunk_work_units(work_units, can_fit, minimums, split_operations, max_rechunk_depth) if can_assess_fit and max_rechunk_depth else None
    planned_units = rechunked.work_units if rechunked else work_units
    heuristic_chunking = rechunked.heuristic_chunking if rechunked else False
    for unit in planned_units:
        generation, options = options_for(unit)
        allow_parallel, field_capacity = operation_policy.get(unit.operation_id, (False, 1))
        source_operation = source_operations.get(str(unit.operation_id), {})
        operation_mandatory = bool(source_operation.get("mandatory", True))
        latest_finish = source_operation.get("latest_finish_at")
        beyond_allowed = bool(
            source_operation.get("can_split")
            and source_operation.get("allow_partial_at_horizon")
            and not source_operation.get("require_full_in_horizon")
            and latest_finish
            and datetime.fromisoformat(latest_finish.replace("Z", "+00:00")) > horizon_end
        )
        mandatory = operation_mandatory and not beyond_allowed
        allow_deferred = not operation_mandatory
        diagnostics = generation.diagnostics
        diagnostics = (*diagnostics, *sorted(weather_diagnostics_by_operation.get(unit.operation_id, set())))
        if rechunked and unit.id in rechunked.failed_unit_ids:
            diagnostics = (*diagnostics, f"UNIT_DOES_NOT_FIT_WINDOW:{unit.id}")
        elif rechunked and rechunked.heuristic_chunking and feasible_start_provider is None and not any(option.feasible_start_slots for option in options):
            diagnostics = (*diagnostics, f"RECHUNK_FEASIBILITY_UNKNOWN:{unit.id}")
        result.append(WorkUnitOptions(unit, field_by_operation[unit.operation_id], mandatory, options, allow_parallel, field_capacity, generation.candidate_truncated, diagnostics, allow_deferred, beyond_allowed, heuristic_chunking))
    return tuple(result)


def optimize_bundles(
    operations: tuple[OperationOptions, ...],
    dependencies: tuple[DependencyConstraint, ...],
    slot_minutes: int = 60,
    horizon_slots: int = 720,
    max_seconds_per_stage: float = 10,
) -> OptimizationResult:
    if slot_minutes not in (15, 30, 60) or horizon_slots <= 0:
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:SOLVER_CONFIG",))
    operation_ids = [operation.operation_id for operation in operations]
    if len(operation_ids) != len(set(operation_ids)):
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:DUPLICATE_OPERATION",))
    known_ids = set(operation_ids)
    if any(dependency.predecessor_id not in known_ids or dependency.successor_id not in known_ids or dependency.min_lag_minutes < 0 or dependency.max_lag_minutes is not None and (dependency.max_lag_minutes < 0 or dependency.max_lag_minutes < dependency.min_lag_minutes) for dependency in dependencies):
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:DEPENDENCY",))

    model = cp_model.CpModel()
    presence: dict[UUID, Any] = {}
    choices: dict[UUID, list[tuple[CandidateOption, Any, Any, Any]]] = {}
    resource_intervals: dict[UUID, list[Any]] = {}
    field_intervals: dict[UUID, list[Any]] = {}
    for operation in operations:
        op_presence = model.new_bool_var(f"scheduled_{operation.operation_id}")
        presence[operation.operation_id] = op_presence
        operation_choices: list[tuple[CandidateOption, Any, Any, Any]] = []
        for index, option in enumerate(operation.candidates):
            if option.bundle.operation_id != operation.operation_id or option.bundle.duration_slots <= 0 or option.bundle.area_ha <= 0 or option.bundle.estimated_cost_cents < 0:
                return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"INPUT_INVALID:BUNDLE:{operation.operation_id}",))
            starts = tuple(sorted({start for start in option.feasible_start_slots if 0 <= start < horizon_slots and start + option.bundle.duration_slots <= horizon_slots}))
            if not starts:
                continue
            bundle = option.bundle
            selected = model.new_bool_var(f"select_{operation.operation_id}_{index}")
            start = model.new_int_var(0, horizon_slots, f"start_{operation.operation_id}_{index}")
            model.add_linear_expression_in_domain(start, cp_model.Domain.from_values(starts)).only_enforce_if(selected)
            model.add(start == 0).only_enforce_if(selected.Not())
            end = model.new_int_var(0, horizon_slots, f"end_{operation.operation_id}_{index}")
            model.add(end == 0).only_enforce_if(selected.Not())
            interval = model.new_optional_interval_var(start, bundle.duration_slots, end, selected, f"interval_{operation.operation_id}_{index}")
            operation_choices.append((option, selected, start, end))
            for resource_id in (*bundle.worker_ids, *bundle.machine_ids, *bundle.equipment_ids):
                resource_intervals.setdefault(resource_id, []).append(interval)
            field_intervals.setdefault(operation.field_id, []).append(interval)
        choices[operation.operation_id] = operation_choices
        model.add(sum(item[1] for item in operation_choices) == op_presence)
        if operation.mandatory:
            model.add(op_presence == 1)

    for intervals in resource_intervals.values():
        model.add_no_overlap(intervals)
    for intervals in field_intervals.values():
        model.add_no_overlap(intervals)

    for dependency in dependencies:
        predecessor_choices = choices[dependency.predecessor_id]
        successor_choices = choices[dependency.successor_id]
        predecessor_presence = presence[dependency.predecessor_id]
        successor_presence = presence[dependency.successor_id]
        model.add(successor_presence <= predecessor_presence)
        minimum_lag_slots = (dependency.min_lag_minutes + slot_minutes - 1) // slot_minutes
        maximum_lag_slots = dependency.max_lag_minutes // slot_minutes if dependency.max_lag_minutes is not None else None
        for _, predecessor_selected, _, predecessor_end in predecessor_choices:
            for _, successor_selected, successor_start, _ in successor_choices:
                model.add(successor_start >= predecessor_end + minimum_lag_slots).only_enforce_if([predecessor_selected, successor_selected])
                if maximum_lag_slots is not None:
                    model.add(successor_start <= predecessor_end + maximum_lag_slots).only_enforce_if([predecessor_selected, successor_selected])

    optional_presence = [presence[operation.operation_id] for operation in operations if not operation.mandatory]
    total_cost = sum(option.bundle.estimated_cost_cents * selected for operation in operations for option, selected, _, _ in choices[operation.operation_id])
    total_start = sum(start for operation in operations for _, _, start, _ in choices[operation.operation_id])
    objectives = (
        ("optional_coverage", -sum(optional_presence)),
        ("estimated_cost_cents", total_cost),
        ("start_slots", total_start),
    )
    stages: list[ObjectiveStage] = []
    final_solver: cp_model.CpSolver | None = None
    final_status = "UNKNOWN"
    for stage_name, expression in objectives:
        model.minimize(expression)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max_seconds_per_stage
        status = solver.solve(model)
        status_name = solver.status_name(status)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            stages.append(ObjectiveStage(stage_name, status_name, None, None))
            final_status = status_name
            if final_solver is None:
                return OptimizationResult(final_status, (), tuple(stages), "FULL", False, ("SOLVER_NO_SOLUTION", "NOT_IMPLEMENTED:TRAVEL_CERTIFICATE"))
            break
        value = int(round(solver.objective_value))
        stages.append(ObjectiveStage(stage_name, status_name, value, int(round(solver.best_objective_bound))))
        final_solver, final_status = solver, status_name
        model.add(expression == value)
        if status != cp_model.OPTIMAL:
            break

    if final_solver is None:
        return OptimizationResult(final_status, (), tuple(stages), "FULL", False, ("SOLVER_NO_SOLUTION", "NOT_IMPLEMENTED:TRAVEL_CERTIFICATE"))
    selected_options: list[SelectedOption] = []
    for operation in operations:
        for option, selected, start, end in choices[operation.operation_id]:
            if final_solver.boolean_value(selected):
                bundle = option.bundle
                selected_options.append(SelectedOption(bundle.operation_id, bundle.id, operation.field_id, final_solver.value(start), final_solver.value(end), str(bundle.area_ha), bundle.worker_ids, bundle.machine_ids, bundle.equipment_ids, bundle.estimated_cost_cents))
    stage_optimal = len(stages) == len(objectives) and all(stage.status == "OPTIMAL" for stage in stages)
    validation = validate_selected_options(operations, dependencies, selected_options, slot_minutes, horizon_slots)
    diagnostic = list(validation.issues)
    diagnostic.append("NOT_IMPLEMENTED:TRAVEL_CERTIFICATE")
    if not validation.valid:
        final_status = "FAILED_VALIDATION"
    if any(stage.status != "OPTIMAL" for stage in stages):
        diagnostic.append("SEARCH_SPACE_INCOMPLETE:LEXICOGRAPHIC_PROOF")
    result_status = final_status if final_status == "FAILED_VALIDATION" else "OPTIMAL" if stage_optimal else "FEASIBLE"
    return OptimizationResult(result_status, tuple(selected_options), tuple(stages), "FULL", False, tuple(diagnostic))


def optimize_work_units(
    work_units: tuple[WorkUnitOptions, ...],
    dependencies: tuple[DependencyConstraint, ...],
    operation_area_totals: dict[UUID, object],
    slot_minutes: int = 60,
    horizon_slots: int = 720,
    max_seconds: float = 20,
    area_context_by_operation: dict[UUID, tuple[Decimal, Decimal, Decimal]] | None = None,
    travel_closure: TravelClosure | None = None,
    location_by_field: dict[UUID, UUID] | None = None,
    travel_category_by_resource: dict[UUID, str] | None = None,
    max_travel_pairs: int = 2_000_000,
    initial_location_by_resource: dict[UUID, UUID] | None = None,
    availability_windows_by_resource: dict[UUID, tuple[tuple[int, int], ...]] | None = None,
    hourly_cost_cents_by_resource: dict[UUID, int] | None = None,
    horizon_start_at: datetime | None = None,
    as_of_slot: int = 0,
    _forbidden_travel_solutions: tuple[tuple[tuple[UUID, UUID | None, int], ...], ...] = (),
    worker_work_limits_by_id: dict[UUID, Any] | None = None,
    working_windows_by_worker: dict[UUID, tuple[tuple[datetime, datetime], ...]] | None = None,
    locked_assignments: tuple[SelectedOption, ...] = (),
) -> OptimizationResult:
    if slot_minutes not in (15, 30, 60) or horizon_slots <= 0:
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:SOLVER_CONFIG",))
    if max_travel_pairs < 1:
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:MAX_TRAVEL_PAIRS",))
    if any(unit.field_parallel_capacity < 1 or unit.allow_parallel_work_units and unit.field_parallel_capacity < 2 for unit in work_units):
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:FIELD_PARALLEL_CAPACITY",))
    by_operation: dict[UUID, list[WorkUnitOptions]] = {}
    generation_diagnostics = sorted({diagnostic for unit in work_units for diagnostic in unit.diagnostics})
    candidates_truncated = any(unit.candidate_truncated for unit in work_units)
    heuristic_chunking = any(unit.heuristic_chunking for unit in work_units)
    search_scope = "HEURISTIC_CHUNKING" if heuristic_chunking else "REDUCED_CANDIDATES" if candidates_truncated else "FULL"
    unit_ids = [unit.unit.id for unit in work_units]
    if len(unit_ids) != len(set(unit_ids)):
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:DUPLICATE_WORK_UNIT",))
    for unit in work_units:
        if unit.unit.area_ha <= 0 or unit.unit.ordinal < 0 or unit.unit.chunk_revision < 1:
            return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"INPUT_INVALID:WORK_UNIT:{unit.unit.id}",))
        if unit.unit.area_ha * Decimal("10000") != (unit.unit.area_ha * Decimal("10000")).to_integral_value():
            return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"INPUT_INVALID:AREA_SCALE:{unit.unit.id}",))
        by_operation.setdefault(unit.unit.operation_id, []).append(unit)
    if set(by_operation) != set(operation_area_totals):
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("AREA_BALANCE_INVALID:OPERATION_SET",))
    locked_work_unit_ids = [item.work_unit_id for item in locked_assignments if item.work_unit_id is not None]
    if len(locked_work_unit_ids) != len(set(locked_work_unit_ids)):
        return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, ("LOCKED_CONFLICT:DUPLICATE_RESERVATION",))
    if set(locked_work_unit_ids) & set(unit_ids):
        return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, ("LOCKED_CONFLICT:RESERVED_WORK_UNIT_REGENERATED",))
    locked_resource_use: dict[UUID, list[SelectedOption]] = {}
    locked_field_use: dict[UUID, list[SelectedOption]] = {}
    for lock in locked_assignments:
        if lock.start_slot < as_of_slot or lock.end_slot > horizon_slots or lock.end_slot <= lock.start_slot:
            return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, (f"LOCKED_CONFLICT:TIME_WINDOW:{lock.operation_id}",))
        if lock.operation_id not in operation_area_totals or Decimal(lock.area_ha) <= 0:
            return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, (f"LOCKED_CONFLICT:INVALID_RESERVATION:{lock.operation_id}",))
        resources = (*lock.worker_ids, *lock.machine_ids, *lock.equipment_ids)
        if not resources or len(resources) != len(set(resources)):
            return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, (f"LOCKED_CONFLICT:RESOURCE_SET:{lock.operation_id}",))
        for resource_id in resources:
            if availability_windows_by_resource is not None and not any(
                start <= lock.start_slot and lock.end_slot <= end
                for start, end in availability_windows_by_resource.get(resource_id, ())
            ):
                return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, (f"LOCKED_CONFLICT:RESOURCE_UNAVAILABLE:{resource_id}:{lock.operation_id}",))
            locked_resource_use.setdefault(resource_id, []).append(lock)
        locked_field_use.setdefault(lock.field_id, []).append(lock)
    for label, grouped in (("RESOURCE", locked_resource_use), ("FIELD", locked_field_use)):
        for entity_id, locks in grouped.items():
            ordered_locks = sorted(locks, key=lambda item: (item.start_slot, item.end_slot))
            for left, right in zip(ordered_locks, ordered_locks[1:]):
                if left.end_slot > right.start_slot:
                    return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, (f"LOCKED_CONFLICT:{label}_OVERLAP:{entity_id}",))
    if locked_assignments and travel_closure is None:
        return OptimizationResult("UNKNOWN", (), (), "FULL", False, ("TRAVEL_CERTIFICATE_UNRESOLVED:LOCKED_TRAVEL_REQUIRED",))
    if locked_assignments and travel_closure is not None:
        if initial_location_by_resource is None or availability_windows_by_resource is None or hourly_cost_cents_by_resource is None or horizon_start_at is None:
            return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, ("LOCKED_CONFLICT:TRAVEL_CONTEXT_MISSING",))
        from fieldscheduler.travel_certificate import build_travel_certificate

        locked_certificate = build_travel_certificate(
            locked_assignments, travel_closure, location_by_field or {}, initial_location_by_resource,
            availability_windows_by_resource, hourly_cost_cents_by_resource, horizon_start_at,
            slot_minutes, travel_category_by_resource, as_of_slot,
        )
        if not locked_certificate.valid:
            return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, tuple(f"LOCKED_CONFLICT:{issue}" for issue in locked_certificate.diagnostics))
    if travel_closure is not None and (location_by_field is None or any(unit.field_id not in location_by_field for unit in work_units)):
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("TRAVEL_DATA_MISSING:FIELD_LOCATION",))
    for operation_id, units in by_operation.items():
        total, completed, reserved = (area_context_by_operation or {}).get(operation_id, (Decimal(operation_area_totals[operation_id]), Decimal("0"), Decimal("0")))
        remaining_area = total - completed - reserved
        locked_area = sum((Decimal(item.area_ha) for item in locked_assignments if item.operation_id == operation_id), start=Decimal("0"))
        if locked_area > reserved:
            return OptimizationResult("LOCKED_CONFLICT", (), (), "FULL", False, (f"LOCKED_CONFLICT:AREA_NOT_RESERVED:{operation_id}",))
        if remaining_area < 0 or sum((unit.unit.area_ha for unit in units), start=Decimal("0")) != remaining_area:
            return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"AREA_BALANCE_INVALID:WORK_UNIT_SUM:{operation_id}",))
        ordinals = [unit.unit.ordinal for unit in units]
        if len(ordinals) != len(set(ordinals)):
            return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"INPUT_INVALID:DUPLICATE_UNIT_ORDINAL:{operation_id}",))
        if any(unit.allow_parallel_work_units != units[0].allow_parallel_work_units or unit.field_parallel_capacity != units[0].field_parallel_capacity for unit in units):
            return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"INPUT_INVALID:INCONSISTENT_FIELD_POLICY:{operation_id}",))
    model = cp_model.CpModel()
    unit_choices: dict[UUID, list[tuple[CandidateOption, Any, Any, Any, Any]]] = {}
    scheduled_units: dict[UUID, Any] = {}
    deferred_units: dict[UUID, Any] = {}
    beyond_units: dict[UUID, Any] = {}
    resource_intervals: dict[UUID, list[Any]] = {}
    field_intervals_by_operation: dict[tuple[UUID, UUID], list[Any]] = {}
    operation_starts: dict[UUID, list[Any] | Any] = {}
    operation_ends: dict[UUID, list[Any] | Any] = {}
    for item in work_units:
        choices: list[tuple[CandidateOption, Any, Any, Any, Any]] = []
        for option_index, option in enumerate(item.candidates):
            bundle = option.bundle
            if bundle.operation_id != item.unit.operation_id or bundle.area_ha != item.unit.area_ha or bundle.duration_slots <= 0:
                return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, (f"INPUT_INVALID:UNIT_BUNDLE:{item.unit.id}",))
            starts = tuple(sorted({value for value in option.feasible_start_slots if 0 <= value < horizon_slots and value + bundle.duration_slots <= horizon_slots}))
            if not starts:
                continue
            selected = model.new_bool_var(f"select_{item.unit.id}_{option_index}")
            start = model.new_int_var(0, horizon_slots, f"start_{item.unit.id}_{option_index}")
            model.add_linear_expression_in_domain(start, cp_model.Domain.from_values(starts)).only_enforce_if(selected)
            model.add(start == 0).only_enforce_if(selected.Not())
            end = model.new_int_var(0, horizon_slots, f"end_{item.unit.id}_{option_index}")
            model.add(end == 0).only_enforce_if(selected.Not())
            interval = model.new_optional_interval_var(start, bundle.duration_slots, end, selected, f"interval_{item.unit.id}_{option_index}")
            choices.append((option, selected, start, end, interval))
            for resource_id in (*bundle.worker_ids, *bundle.machine_ids, *bundle.equipment_ids):
                resource_intervals.setdefault(resource_id, []).append(interval)
            field_intervals_by_operation.setdefault((item.unit.operation_id, item.field_id), []).append(interval)
        unit_choices[item.unit.id] = choices
        scheduled = model.new_bool_var(f"scheduled_{item.unit.id}")
        deferred = model.new_bool_var(f"deferred_{item.unit.id}")
        beyond = model.new_bool_var(f"beyond_{item.unit.id}")
        scheduled_units[item.unit.id] = scheduled
        deferred_units[item.unit.id] = deferred
        beyond_units[item.unit.id] = beyond
        model.add(sum(choice[1] for choice in choices) == scheduled)
        model.add(scheduled + deferred + beyond == 1)
        if item.mandatory:
            model.add(scheduled == 1)
        if not item.allow_deferred:
            model.add(deferred == 0)
        if not item.allow_beyond_horizon:
            model.add(beyond == 0)
        unit_start = model.new_int_var(0, horizon_slots, f"unit_start_{item.unit.id}")
        unit_end = model.new_int_var(0, horizon_slots, f"unit_end_{item.unit.id}")
        model.add(unit_start == sum(choice[2] for choice in choices))
        model.add(unit_end == sum(choice[3] for choice in choices))
        operation_starts.setdefault(item.unit.operation_id, []).append(unit_start)
        operation_ends.setdefault(item.unit.operation_id, []).append(unit_end)

    fixed_field_intervals: dict[UUID, list[Any]] = {}
    for lock_index, lock in enumerate(locked_assignments):
        duration = lock.end_slot - lock.start_slot
        interval = model.new_fixed_size_interval_var(lock.start_slot, duration, f"locked_interval_{lock_index}_{lock.operation_id}")
        for resource_id in (*lock.worker_ids, *lock.machine_ids, *lock.equipment_ids):
            resource_intervals.setdefault(resource_id, []).append(interval)
        fixed_field_intervals.setdefault(lock.field_id, []).append(interval)

    # Exclude only the exact selected bundle/start combination found invalid by
    # the independent travel certificate. A bundle may still be retried at a
    # different feasible start slot.
    for cut_index, forbidden in enumerate(_forbidden_travel_solutions):
        exact_choice_literals: list[Any] = []
        forbidden_by_unit = {unit_id: (bundle_id, slot) for unit_id, bundle_id, slot in forbidden}
        for item in work_units:
            forbidden_choice = forbidden_by_unit.get(item.unit.id)
            if forbidden_choice is None:
                continue
            forbidden_bundle_id, forbidden_slot = forbidden_choice
            if forbidden_bundle_id is None:
                exact_choice_literals.append(scheduled_units[item.unit.id].Not())
                continue
            for option, selected, start, _, _ in unit_choices[item.unit.id]:
                if option.bundle.id != forbidden_bundle_id:
                    continue
                at_forbidden_start = model.new_bool_var(f"travel_cut_{cut_index}_{item.unit.id}_at_start")
                exact_choice = model.new_bool_var(f"travel_cut_{cut_index}_{item.unit.id}_selected")
                model.add(start == forbidden_slot).only_enforce_if(at_forbidden_start)
                model.add(start != forbidden_slot).only_enforce_if(at_forbidden_start.Not())
                model.add_bool_and([selected, at_forbidden_start]).only_enforce_if(exact_choice)
                model.add_bool_or([selected.Not(), at_forbidden_start.Not(), exact_choice])
                exact_choice_literals.append(exact_choice)
        if exact_choice_literals:
            model.add(sum(exact_choice_literals) <= len(work_units) - 1)

    for intervals in resource_intervals.values():
        model.add_no_overlap(intervals)
    if travel_closure is not None:
        candidate_intervals = [
            (item, option, selected, start, end)
            for item in work_units
            for option, selected, start, end, _ in unit_choices[item.unit.id]
        ]
        from fieldscheduler.travel_certificate import _intersections, _k1_departure

        machine_predecessors: dict[int, list[Any]] = {index: [] for index in range(len(candidate_intervals))}
        for first_index, first in enumerate(candidate_intervals):
            for second_index in range(first_index + 1, len(candidate_intervals)):
                second = candidate_intervals[second_index]
                if first[0].unit.id == second[0].unit.id:
                    continue
                if not set(first[1].bundle.machine_ids).intersection(second[1].bundle.machine_ids):
                    continue
                forward = model.new_bool_var(f"machine_order_forward_{first_index}_{second_index}")
                reverse = model.new_bool_var(f"machine_order_reverse_{first_index}_{second_index}")
                for order in (forward, reverse):
                    model.add(order <= first[2])
                    model.add(order <= second[2])
                model.add_bool_or([first[2].Not(), second[2].Not(), forward, reverse])
                model.add(second[3] >= first[4]).only_enforce_if(forward)
                model.add(first[3] >= second[4]).only_enforce_if(reverse)
                machine_predecessors[first_index].append(reverse)
                machine_predecessors[second_index].append(forward)

        for index, (item, option, selected, start, _) in enumerate(candidate_intervals):
            first_job = model.new_bool_var(f"first_machine_job_{index}")
            predecessors = machine_predecessors[index]
            model.add(first_job <= selected)
            for predecessor in predecessors:
                model.add(first_job + predecessor <= 1)
            model.add(first_job >= selected - sum(predecessors))

            bundle = option.bundle
            if len(bundle.machine_ids) != 1 or len(bundle.worker_ids) != 1 or len(bundle.equipment_ids) > 1:
                model.add(first_job == 0)
                continue
            machine_id = bundle.machine_ids[0]
            participants = (*bundle.machine_ids, *bundle.worker_ids, *bundle.equipment_ids)
            initial_location = (initial_location_by_resource or {}).get(machine_id)
            field_location = (location_by_field or {}).get(item.field_id)
            if initial_location is None or field_location is None or any(
                (initial_location_by_resource or {}).get(resource_id) != initial_location
                for resource_id in participants
            ):
                model.add(first_job == 0)
                continue
            route = travel_closure.route(
                initial_location,
                field_location,
                (travel_category_by_resource or {}).get(machine_id, "ANY"),
            )
            if route is None:
                model.add(first_job == 0)
                continue
            if route.duration_minutes <= 0:
                continue
            common_windows = _intersections(participants, availability_windows_by_resource or {})
            duration_slots = (route.duration_minutes + slot_minutes - 1) // slot_minutes
            reachable_starts = tuple(
                start_slot
                for start_slot in option.feasible_start_slots
                if _k1_departure(as_of_slot, start_slot, duration_slots, common_windows, initial=True) is not None
            )
            if reachable_starts:
                model.add_linear_expression_in_domain(
                    start, cp_model.Domain.from_values(reachable_starts)
                ).only_enforce_if(first_job)
            else:
                model.add(first_job == 0)

        candidate_pairs: list[tuple[Any, Any]] = []
        for first_index, first in enumerate(candidate_intervals):
            first_resources = set((*first[1].bundle.worker_ids, *first[1].bundle.machine_ids, *first[1].bundle.equipment_ids))
            for second in candidate_intervals[first_index + 1 :]:
                if first[0].unit.id == second[0].unit.id:
                    continue
                second_resources = set((*second[1].bundle.worker_ids, *second[1].bundle.machine_ids, *second[1].bundle.equipment_ids))
                shared_resources = first_resources & second_resources
                if not shared_resources:
                    continue
                first_location = location_by_field[first[0].field_id]
                second_location = location_by_field[second[0].field_id]
                if first_location != second_location:
                    candidate_pairs.append((first, second, shared_resources))
                    if len(candidate_pairs) > max_travel_pairs:
                        return OptimizationResult("SIZE_LIMIT", (), (), "FULL", False, ("SIZE_LIMIT:MAX_TRAVEL_PAIRS",))
        for pair_index, (first, second, shared_resources) in enumerate(candidate_pairs):
            first_before_second = model.new_bool_var(f"travel_order_{pair_index}_forward")
            second_before_first = model.new_bool_var(f"travel_order_{pair_index}_reverse")
            model.add_bool_or([first[2].Not(), second[2].Not(), first_before_second, second_before_first])
            forward_durations: list[int] = []
            reverse_durations: list[int] = []
            for resource_id in shared_resources:
                category = (travel_category_by_resource or {}).get(resource_id, "ANY")
                forward = travel_closure.route(location_by_field[first[0].field_id], location_by_field[second[0].field_id], category)
                reverse = travel_closure.route(location_by_field[second[0].field_id], location_by_field[first[0].field_id], category)
                if forward is None:
                    model.add(first_before_second == 0)
                else:
                    forward_durations.append(forward.duration_minutes)
                if reverse is None:
                    model.add(second_before_first == 0)
                else:
                    reverse_durations.append(reverse.duration_minutes)
            if forward_durations:
                forward_slots = (max(forward_durations) + slot_minutes - 1) // slot_minutes
                model.add(second[3] >= first[4] + forward_slots).only_enforce_if([first[2], second[2], first_before_second])
            if reverse_durations:
                reverse_slots = (max(reverse_durations) + slot_minutes - 1) // slot_minutes
                model.add(first[3] >= second[4] + reverse_slots).only_enforce_if([first[2], second[2], second_before_first])
    for operation_id, units in by_operation.items():
        ordered_units = sorted(units, key=lambda value: value.unit.ordinal)
        for earlier, later in zip(ordered_units, ordered_units[1:]):
            model.add(scheduled_units[later.unit.id] <= scheduled_units[earlier.unit.id])
            model.add(beyond_units[earlier.unit.id] <= beyond_units[later.unit.id])
        if not ordered_units[0].allow_parallel_work_units:
            for previous, following in zip(ordered_units, ordered_units[1:]):
                previous_end = model.new_int_var(0, horizon_slots, f"sequence_end_{previous.unit.id}")
                following_start = model.new_int_var(0, horizon_slots, f"sequence_start_{following.unit.id}")
                model.add(previous_end == sum(choice[3] for choice in unit_choices[previous.unit.id]))
                model.add(following_start == sum(choice[2] for choice in unit_choices[following.unit.id]))
                model.add(following_start >= previous_end)
            for first_index, first in enumerate(ordered_units):
                for second in ordered_units[first_index + 1 :]:
                    model.add_no_overlap([choice[4] for choice in unit_choices[first.unit.id] + unit_choices[second.unit.id]])
        else:
            cap = ordered_units[0].field_parallel_capacity
            all_intervals = [choice[4] for unit in ordered_units for choice in unit_choices[unit.unit.id]]
            model.add_cumulative(all_intervals, [1] * len(all_intervals), cap)

    field_groups: dict[UUID, list[WorkUnitOptions]] = {}
    for item in work_units:
        field_groups.setdefault(item.field_id, []).append(item)
    for field_id, fixed_intervals in fixed_field_intervals.items():
        candidate_intervals = [
            interval
            for (operation_id, candidate_field), intervals in field_intervals_by_operation.items()
            if candidate_field == field_id
            for interval in intervals
        ]
        for fixed_interval in fixed_intervals:
            for candidate_interval in candidate_intervals:
                model.add_no_overlap([fixed_interval, candidate_interval])
    for field_id, items in field_groups.items():
        operation_ids = sorted({item.unit.operation_id for item in items}, key=str)
        for first_index, first_operation in enumerate(operation_ids):
            for second_operation in operation_ids[first_index + 1 :]:
                first_intervals = [interval for (operation_id, candidate_field), intervals in field_intervals_by_operation.items() if operation_id == first_operation and candidate_field == field_id for interval in intervals]
                second_intervals = [interval for (operation_id, candidate_field), intervals in field_intervals_by_operation.items() if operation_id == second_operation and candidate_field == field_id for interval in intervals]
                for first_interval in first_intervals:
                    for second_interval in second_intervals:
                        model.add_no_overlap([first_interval, second_interval])

    for operation_id in operation_starts:
        start_values = operation_starts[operation_id]
        end_values = operation_ends[operation_id]
        operation_start = model.new_int_var(0, horizon_slots, f"operation_start_{operation_id}")
        operation_end = model.new_int_var(0, horizon_slots, f"operation_end_{operation_id}")
        model.add_min_equality(operation_start, start_values)
        model.add_max_equality(operation_end, end_values)
        operation_starts[operation_id] = operation_start
        operation_ends[operation_id] = operation_end
    for dependency in dependencies:
        if dependency.predecessor_id not in operation_ends or dependency.successor_id not in operation_starts:
            return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:DEPENDENCY_OPERATION",))
        predecessor_complete = model.new_bool_var(f"complete_{dependency.predecessor_id}")
        successor_complete = model.new_bool_var(f"complete_{dependency.successor_id}")
        predecessor_units = by_operation[dependency.predecessor_id]
        successor_units = by_operation[dependency.successor_id]
        for unit in predecessor_units:
            model.add(predecessor_complete <= scheduled_units[unit.unit.id])
        model.add(predecessor_complete >= sum(scheduled_units[unit.unit.id] for unit in predecessor_units) - len(predecessor_units) + 1)
        for unit in successor_units:
            model.add(successor_complete <= scheduled_units[unit.unit.id])
        model.add(successor_complete >= sum(scheduled_units[unit.unit.id] for unit in successor_units) - len(successor_units) + 1)
        model.add(successor_complete <= predecessor_complete)
        min_lag_slots = (dependency.min_lag_minutes + slot_minutes - 1) // slot_minutes
        model.add(operation_starts[dependency.successor_id] >= operation_ends[dependency.predecessor_id] + min_lag_slots).only_enforce_if(successor_complete)
        if dependency.max_lag_minutes is not None:
            max_lag_slots = dependency.max_lag_minutes // slot_minutes
            model.add(operation_starts[dependency.successor_id] <= operation_ends[dependency.predecessor_id] + max_lag_slots).only_enforce_if(successor_complete)

    cost = sum(option.bundle.estimated_cost_cents * selected for choices in unit_choices.values() for option, selected, _, _, _ in choices)
    total_start = sum(start for choices in unit_choices.values() for _, _, start, _, _ in choices)
    planned_area_scaled = sum(int(item.unit.area_ha * Decimal("10000")) * scheduled_units[item.unit.id] for item in work_units)
    deferred_area_scaled = sum(int(item.unit.area_ha * Decimal("10000")) * deferred_units[item.unit.id] for item in work_units)
    stages: list[ObjectiveStage] = []
    final_solver: cp_model.CpSolver | None = None
    objectives = (("planned_area_ha_1e4", -planned_area_scaled), ("deferred_area_ha_1e4", deferred_area_scaled), ("estimated_cost_cents", cost), ("start_slots", total_start))
    for stage_name, objective in objectives:
        model.minimize(objective)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max_seconds
        status = solver.solve(model)
        status_name = solver.status_name(status)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            stages.append(ObjectiveStage(stage_name, status_name, None, None))
            if final_solver is None:
                no_solution_diagnostics = [*generation_diagnostics, "SOLVER_NO_SOLUTION"]
                if _forbidden_travel_solutions and status == cp_model.INFEASIBLE:
                    no_solution_diagnostics.append("REPAIR_EXHAUSTED:ALL_CUT_SOLUTIONS")
                return OptimizationResult(status_name, (), tuple(stages), search_scope, False, tuple(no_solution_diagnostics))
            break
        value = int(round(solver.objective_value))
        stages.append(ObjectiveStage(stage_name, status_name, value, int(round(solver.best_objective_bound))))
        final_solver = solver
        model.add(objective == value)
        if status != cp_model.OPTIMAL:
            break
    if final_solver is None:
        no_solution_diagnostics = [*generation_diagnostics, "SOLVER_NO_SOLUTION"]
        final_solver_status = stages[-1].status if stages else "UNKNOWN"
        if _forbidden_travel_solutions and final_solver_status == "INFEASIBLE":
            no_solution_diagnostics.append("REPAIR_EXHAUSTED:ALL_CUT_SOLUTIONS")
        return OptimizationResult(final_solver_status, (), tuple(stages), search_scope, False, tuple(no_solution_diagnostics))
    selected_assignments: list[SelectedOption] = []
    for item in work_units:
        for option, selected, start, end, _ in unit_choices[item.unit.id]:
            if final_solver.boolean_value(selected):
                bundle = option.bundle
                selected_start = final_solver.value(start)
                weather_status = dict(option.weather_status_by_start).get(selected_start, "NOT_ASSESSED")
                selected_assignments.append(SelectedOption(bundle.operation_id, bundle.id, item.field_id, selected_start, final_solver.value(end), str(bundle.area_ha), bundle.worker_ids, bundle.machine_ids, bundle.equipment_ids, bundle.estimated_cost_cents, item.unit.id, weather_status))
    all_selected_assignments = (*locked_assignments, *selected_assignments)
    dispositions = tuple(
        UnitDisposition(unit.unit.id, "SCHEDULED" if final_solver.boolean_value(scheduled_units[unit.unit.id]) else "BEYOND_HORIZON" if final_solver.boolean_value(beyond_units[unit.unit.id]) else "DEFERRED")
        for unit in work_units
    )
    area_balances: list[AreaBalance] = []
    for operation_id, units in by_operation.items():
        total, completed, reserved = (area_context_by_operation or {}).get(operation_id, (Decimal(operation_area_totals[operation_id]), Decimal("0"), Decimal("0")))
        planned = sum((unit.unit.area_ha for unit in units if final_solver.boolean_value(scheduled_units[unit.unit.id])), start=Decimal("0"))
        beyond_area = sum((unit.unit.area_ha for unit in units if final_solver.boolean_value(beyond_units[unit.unit.id])), start=Decimal("0"))
        deferred_area_value = sum((unit.unit.area_ha for unit in units if final_solver.boolean_value(deferred_units[unit.unit.id])), start=Decimal("0"))
        area_balances.append(AreaBalance(operation_id, total, completed, reserved, planned, beyond_area, deferred_area_value))
    validation = validate_work_unit_schedule(work_units, dependencies, selected_assignments, operation_area_totals, slot_minutes, horizon_slots, dispositions, tuple(area_balances))
    proved = len(stages) == len(objectives) and all(stage.status == "OPTIMAL" for stage in stages) and not candidates_truncated and not heuristic_chunking
    status = "OPTIMAL" if proved else "FEASIBLE"
    diagnostics = [*generation_diagnostics, *validation.issues]
    travel_legs: tuple[Any, ...] = ()
    travel_validated = False
    work_limits_validated = False
    if travel_closure is not None:
        diagnostics.extend(travel_closure.warnings)
        if initial_location_by_resource is None or availability_windows_by_resource is None or hourly_cost_cents_by_resource is None or horizon_start_at is None:
            diagnostics.append("TRAVEL_CERTIFICATE_UNRESOLVED:REQUIRED_CONTEXT_MISSING")
        else:
            from fieldscheduler.travel_certificate import build_travel_certificate, validate_travel_certificate

            certificate = build_travel_certificate(
                all_selected_assignments,
                travel_closure,
                location_by_field or {},
                initial_location_by_resource,
                availability_windows_by_resource,
                hourly_cost_cents_by_resource,
                horizon_start_at,
                slot_minutes,
                travel_category_by_resource,
                as_of_slot,
            )
            travel_legs = certificate.legs
            diagnostics.extend(certificate.diagnostics)
            if not certificate.valid:
                diagnostics.append("TRAVEL_CERTIFICATE_UNRESOLVED")
                if len(_forbidden_travel_solutions) < 32:
                    selected_by_unit = {item.work_unit_id: item for item in selected_assignments}
                    forbidden = tuple(sorted(
                        ((unit.unit.id, selected_by_unit[unit.unit.id].bundle_id, selected_by_unit[unit.unit.id].start_slot)
                         if unit.unit.id in selected_by_unit else (unit.unit.id, None, -1) for unit in work_units),
                        key=lambda row: (str(row[0]), str(row[1]), row[2]),
                    ))
                    if forbidden and forbidden not in _forbidden_travel_solutions:
                        repaired = optimize_work_units(
                            work_units, dependencies, operation_area_totals, slot_minutes, horizon_slots, max_seconds,
                            area_context_by_operation, travel_closure, location_by_field, travel_category_by_resource,
                            max_travel_pairs, initial_location_by_resource, availability_windows_by_resource,
                            hourly_cost_cents_by_resource, horizon_start_at, as_of_slot,
                            (*_forbidden_travel_solutions, forbidden), worker_work_limits_by_id,
                            working_windows_by_worker, locked_assignments,
                        )
                        return OptimizationResult(
                            repaired.solver_status, repaired.selected, repaired.objective_stages, repaired.search_scope,
                            repaired.validated, (f"TRAVEL_REPAIR_APPLIED:{len(_forbidden_travel_solutions) + 1}", *repaired.diagnostics),
                            repaired.unit_dispositions, repaired.area_balances, repaired.travel_legs,
                        )
                else:
                    diagnostics.append("TRAVEL_REPAIR_LIMIT_REACHED:32")
            else:
                travel_issues = validate_travel_certificate(
                    certificate,
                    all_selected_assignments,
                    travel_closure,
                    location_by_field or {},
                    initial_location_by_resource,
                    availability_windows_by_resource,
                    horizon_start_at,
                    slot_minutes,
                    travel_category_by_resource,
                )
                diagnostics.extend(travel_issues)
                travel_validated = not travel_issues
                if travel_issues:
                    diagnostics.append("TRAVEL_CERTIFICATE_UNRESOLVED:INDEPENDENT_VALIDATION_FAILED")
                    if len(_forbidden_travel_solutions) < 32:
                        selected_by_unit = {item.work_unit_id: item for item in selected_assignments}
                        forbidden = tuple(sorted(
                            ((unit.unit.id, selected_by_unit[unit.unit.id].bundle_id, selected_by_unit[unit.unit.id].start_slot)
                             if unit.unit.id in selected_by_unit else (unit.unit.id, None, -1) for unit in work_units),
                            key=lambda row: (str(row[0]), str(row[1]), row[2]),
                        ))
                        if forbidden and forbidden not in _forbidden_travel_solutions:
                            repaired = optimize_work_units(
                                work_units, dependencies, operation_area_totals, slot_minutes, horizon_slots, max_seconds,
                                area_context_by_operation, travel_closure, location_by_field, travel_category_by_resource,
                                max_travel_pairs, initial_location_by_resource, availability_windows_by_resource,
                                hourly_cost_cents_by_resource, horizon_start_at, as_of_slot,
                                (*_forbidden_travel_solutions, forbidden), worker_work_limits_by_id,
                                working_windows_by_worker, locked_assignments,
                            )
                            return OptimizationResult(
                                repaired.solver_status, repaired.selected, repaired.objective_stages, repaired.search_scope,
                                repaired.validated, (f"TRAVEL_REPAIR_APPLIED:{len(_forbidden_travel_solutions) + 1}", *repaired.diagnostics),
                                repaired.unit_dispositions, repaired.area_balances, repaired.travel_legs,
                            )
                    else:
                        diagnostics.append("TRAVEL_REPAIR_LIMIT_REACHED:32")
    else:
        diagnostics.append("NOT_IMPLEMENTED:TRAVEL_CERTIFICATE")
        if locked_assignments:
            diagnostics.append("TRAVEL_CERTIFICATE_UNRESOLVED:LOCKED_TRAVEL_REQUIRED")
    if candidates_truncated:
        diagnostics.append("SEARCH_SPACE_INCOMPLETE:CANDIDATE_PRUNING")
    if heuristic_chunking:
        diagnostics.append("SEARCH_SPACE_INCOMPLETE:HEURISTIC_CHUNKING")
    if selected_assignments:
        if worker_work_limits_by_id is None or horizon_start_at is None:
            diagnostics.append("WORK_LIMITS_CONTEXT_MISSING")
        else:
            from fieldscheduler.work_limits import validate_worker_work_limits

            duration_minutes_by_bundle = {
                option.bundle.id: option.bundle.duration_minutes
                for item in work_units
                for option in item.candidates
            }
            duration_minutes_by_bundle.update({
                lock.bundle_id: (lock.end_slot - lock.start_slot) * slot_minutes
                for lock in locked_assignments
            })
            work_limit_issues = validate_worker_work_limits(
                all_selected_assignments,
                travel_legs,
                duration_minutes_by_bundle,
                worker_work_limits_by_id,
                horizon_start_at,
                slot_minutes,
                working_windows_by_worker,
            )
            diagnostics.extend(work_limit_issues)
            if work_limit_issues:
                status = "UNKNOWN"
            hard_limit_issues = tuple(
                issue for issue in work_limit_issues
                if issue.startswith(("DAILY_WORK_LIMIT_EXCEEDED:", "WEEKLY_WORK_LIMIT_EXCEEDED:", "MIN_REST_VIOLATION:"))
            )
            work_limits_validated = not work_limit_issues
            if hard_limit_issues and len(_forbidden_travel_solutions) < 32:
                selected_by_unit = {item.work_unit_id: item for item in selected_assignments}
                forbidden = tuple(sorted(
                    ((unit.unit.id, selected_by_unit[unit.unit.id].bundle_id, selected_by_unit[unit.unit.id].start_slot)
                     if unit.unit.id in selected_by_unit else (unit.unit.id, None, -1) for unit in work_units),
                    key=lambda row: (str(row[0]), str(row[1]), row[2]),
                ))
                if forbidden and forbidden not in _forbidden_travel_solutions:
                    repaired = optimize_work_units(
                        work_units, dependencies, operation_area_totals, slot_minutes, horizon_slots, max_seconds,
                        area_context_by_operation, travel_closure, location_by_field, travel_category_by_resource,
                        max_travel_pairs, initial_location_by_resource, availability_windows_by_resource,
                        hourly_cost_cents_by_resource, horizon_start_at, as_of_slot,
                        (*_forbidden_travel_solutions, forbidden), worker_work_limits_by_id,
                        working_windows_by_worker, locked_assignments,
                    )
                    return OptimizationResult(
                        repaired.solver_status, repaired.selected, repaired.objective_stages, repaired.search_scope,
                        repaired.validated, (f"WORK_LIMIT_REPAIR_APPLIED:{len(_forbidden_travel_solutions) + 1}", *repaired.diagnostics),
                        repaired.unit_dispositions, repaired.area_balances, repaired.travel_legs,
                    )
                elif forbidden in _forbidden_travel_solutions:
                    diagnostics.append("WORK_LIMIT_REPAIR_CUT_DUPLICATE")
            elif hard_limit_issues:
                diagnostics.append("WORK_LIMIT_REPAIR_LIMIT_REACHED:32")
    else:
        work_limits_validated = True
    if travel_closure is not None and ("TRAVEL_CERTIFICATE_UNRESOLVED" in diagnostics or "TRAVEL_CERTIFICATE_UNRESOLVED:REQUIRED_CONTEXT_MISSING" in diagnostics):
        status = "UNKNOWN"
    if "WORK_LIMITS_CONTEXT_MISSING" in diagnostics or any(issue.startswith("REST_NOT_GUARANTEED_BY_CALENDAR:") or issue.startswith("WORK_LIMITS_MISSING:") for issue in diagnostics):
        status = "UNKNOWN"
    return OptimizationResult(status if validation.valid else "FAILED_VALIDATION", tuple(all_selected_assignments), tuple(stages), search_scope, validation.valid and travel_validated and work_limits_validated, tuple(diagnostics), dispositions, tuple(area_balances), travel_legs)
