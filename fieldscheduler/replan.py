from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from math import ceil
from typing import Any
from uuid import UUID

from fieldscheduler.execution import (
    ExecutionReconciliation,
    FixedAreaReservation,
    execution_facts_from_seed,
    reconcile_execution_facts,
)
from fieldscheduler.optimizer import (
    DependencyConstraint,
    SelectedOption,
    WorkUnitOptions,
    work_unit_options_from_seed,
)
from fieldscheduler.seed_calendar import (
    SeedFeasibleStartProvider,
    expand_seed_resource_windows,
    rest_guarantee_diagnostics,
)
from fieldscheduler.travel import TravelClosure, field_locations_from_seed, travel_closure_from_seed
from fieldscheduler.travel_certificate import initial_locations_from_seed
from fieldscheduler.work_limits import WorkerWorkLimit
from fieldscheduler.work_units import WorkUnit, generate_work_units
from fieldscheduler.zones import PlanningZone, ZonePolicy
from fieldscheduler.weather import weather_assessor_from_seed


@dataclass(frozen=True)
class ReplanContext:
    valid: bool
    execution_facts: ExecutionReconciliation
    locked_assignments: tuple[SelectedOption, ...]
    additional_reservations: tuple[FixedAreaReservation, ...]
    area_context_by_operation: tuple[tuple[UUID, tuple[Decimal, Decimal, Decimal]], ...]
    diagnostics: tuple[str, ...]
    verified_locations_by_resource: tuple[tuple[UUID, UUID], ...] = ()
    as_of_slot: int = 0

    def area_context(self) -> dict[UUID, tuple[Decimal, Decimal, Decimal]]:
        return dict(self.area_context_by_operation)

    def remaining_unreserved_area(self) -> dict[UUID, Decimal]:
        return {
            operation_id: total - completed - reserved
            for operation_id, (total, completed, reserved) in self.area_context_by_operation
        }

    def verified_locations(self) -> dict[UUID, UUID]:
        return dict(self.verified_locations_by_resource)

    def optimizer_inputs(self) -> dict[str, Any]:
        if not self.valid:
            raise ValueError("REPLAN_CONTEXT_INVALID:" + ";".join(self.diagnostics))
        return {
            "area_context_by_operation": self.area_context(),
            "locked_assignments": self.locked_assignments,
            "as_of_slot": self.as_of_slot,
        }

    def travel_inputs(self, entities: dict[str, Any], farm_id: UUID, as_of: datetime) -> ReplanTravelInputs:
        if not self.valid:
            raise ValueError("REPLAN_CONTEXT_INVALID:" + ";".join(self.diagnostics))
        return ReplanTravelInputs(
            travel_closure_from_seed(entities, farm_id, as_of),
            field_locations_from_seed(entities, farm_id),
            initial_locations_from_seed(entities, farm_id, as_of, self.verified_locations()),
            _travel_categories_from_seed(entities, farm_id),
        )


@dataclass(frozen=True)
class ReplanTravelInputs:
    travel_closure: TravelClosure
    location_by_field: dict[UUID, UUID]
    initial_location_by_resource: dict[UUID, UUID]
    travel_category_by_resource: dict[UUID, str]

    def optimizer_inputs(self) -> dict[str, Any]:
        return {
            "travel_closure": self.travel_closure,
            "location_by_field": self.location_by_field,
            "initial_location_by_resource": self.initial_location_by_resource,
            "travel_category_by_resource": self.travel_category_by_resource,
        }


def _travel_categories_from_seed(entities: dict[str, Any], farm_id: UUID) -> dict[UUID, str]:
    categories: dict[UUID, str] = {}
    farm_key = str(farm_id)
    for row in entities.get("machines", []):
        if row.get("farm_id") == farm_key and row.get("active", True) and row.get("machine_type"):
            categories[UUID(row["id"])] = str(row["machine_type"])
    for row in entities.get("workers", []):
        if row.get("farm_id") == farm_key and row.get("active", True):
            categories[UUID(row["id"])] = "WORKER"
    for row in entities.get("equipment", []):
        if row.get("farm_id") == farm_key and row.get("active", True):
            categories[UUID(row["id"])] = "EQUIPMENT"
    return categories


@dataclass(frozen=True)
class ReplanWorkload:
    work_units: tuple[WorkUnit, ...]
    field_by_operation: tuple[tuple[UUID, UUID], ...]
    diagnostics: tuple[str, ...]

    def fields(self) -> dict[UUID, UUID]:
        return dict(self.field_by_operation)


@dataclass(frozen=True)
class ReplanCandidateInputs:
    valid: bool
    work_unit_options: tuple[WorkUnitOptions, ...]
    availability_windows_by_resource: tuple[tuple[UUID, tuple[tuple[int, int], ...]], ...]
    hourly_cost_cents_by_resource: tuple[tuple[UUID, int], ...]
    worker_work_limits_by_id: tuple[tuple[UUID, WorkerWorkLimit], ...]
    working_windows_by_worker: tuple[tuple[UUID, tuple[tuple[datetime, datetime], ...]], ...]
    diagnostics: tuple[str, ...]

    def optimizer_inputs(self) -> dict[str, Any]:
        if not self.valid:
            raise ValueError("REPLAN_CANDIDATES_INVALID:" + ";".join(self.diagnostics))
        return {
            "availability_windows_by_resource": dict(self.availability_windows_by_resource),
            "hourly_cost_cents_by_resource": dict(self.hourly_cost_cents_by_resource),
            "worker_work_limits_by_id": dict(self.worker_work_limits_by_id),
            "working_windows_by_worker": dict(self.working_windows_by_worker),
        }


def build_replan_work_units(
    entities: dict[str, Any],
    farm_id: UUID,
    run_id: UUID,
    context: ReplanContext,
    horizon_start_at: datetime,
    horizon_hours: int,
    slot_minutes: int,
    eligible_capacities_by_operation: dict[UUID, tuple[Decimal, ...]] | None = None,
    target_chunk_hours: Decimal = Decimal("4"),
) -> ReplanWorkload:
    """Chunk only area left after completed and fixed reservations are removed."""
    if not context.valid:
        return ReplanWorkload((), (), ("REPLAN_CONTEXT_INVALID", *context.diagnostics))
    from fieldscheduler.bundles import generate_bundles

    remaining_by_operation = context.remaining_unreserved_area()
    cultivations = {
        item["id"]: item
        for item in entities.get("cultivations", [])
        if item.get("farm_id") == str(farm_id)
    }
    work_units: list[WorkUnit] = []
    fields: dict[UUID, UUID] = {}
    diagnostics: set[str] = set()
    for operation in entities.get("field_operations", []):
        if operation.get("farm_id") != str(farm_id):
            continue
        try:
            operation_id = UUID(operation["id"])
            cultivation = cultivations[operation["cultivation_id"]]
            field_id = UUID(cultivation["field_id"])
        except (KeyError, ValueError):
            diagnostics.add(f"INPUT_INVALID:REPLAN_OPERATION:{operation.get('id', 'UNKNOWN')}")
            continue
        fields[operation_id] = field_id
        remaining_area = remaining_by_operation.get(operation_id, Decimal("0"))
        if remaining_area == 0:
            continue
        if remaining_area < 0:
            diagnostics.add(f"AREA_BALANCE_INVALID:NEGATIVE_REMAINDER:{operation_id}")
            continue
        capacities = (eligible_capacities_by_operation or {}).get(operation_id)
        if capacities is None:
            generation = generate_bundles(
                entities, operation_id, farm_id, horizon_start_at, horizon_hours,
                slot_minutes, max_bundles=1000, area_override=remaining_area,
            )
            diagnostics.update(generation.diagnostics)
            capacities = tuple(sorted({candidate.effective_capacity_ha_per_hour for candidate in generation.candidates}))
            if generation.candidate_truncated:
                diagnostics.add(f"BUNDLE_PRUNING_RISK:CHUNK_CAPACITY:{operation_id}")
        try:
            units = generate_work_units(
                operation_id,
                run_id,
                remaining_area,
                capacities,
                bool(operation.get("can_split", False)),
                target_chunk_hours,
                Decimal(str(operation.get("min_chunk_ha", "0.0001"))),
                Decimal(str(operation["max_chunk_ha"])) if operation.get("max_chunk_ha") is not None else None,
            )
        except (ValueError, ArithmeticError) as error:
            diagnostics.add(str(error))
            continue
        work_units.extend(units)
    unknown = set(remaining_by_operation) - set(fields)
    diagnostics.update(f"INPUT_INVALID:REPLAN_OPERATION_MISSING:{operation_id}" for operation_id in unknown if remaining_by_operation[operation_id] > 0)
    return ReplanWorkload(
        tuple(work_units),
        tuple(sorted(fields.items(), key=lambda item: str(item[0]))),
        tuple(sorted(diagnostics)),
    )


def build_replan_candidate_inputs(
    entities: dict[str, Any],
    farm_id: UUID,
    workload: ReplanWorkload,
    horizon_start_at: datetime,
    horizon_hours: int,
    slot_minutes: int,
    as_of_slot: int,
    max_bundles_per_unit: int = 20,
    weather_assessor: Any | None = None,
    weather_zone_policy: ZonePolicy | None = None,
    weather_policy: str | None = None,
    weather_data_horizon_hours: int | None = None,
    as_of_at: datetime | None = None,
    scenario_id: UUID | None = None,
) -> ReplanCandidateInputs:
    """Build per-unit candidates, feasible starts, calendars, rates, and limits."""
    diagnostics: set[str] = set(workload.diagnostics)
    fatal_workload_prefixes = ("INPUT_INVALID:", "AREA_BALANCE_INVALID:", "PRODUCTIVITY_UNKNOWN:", "NOT_IMPLEMENTED:")
    if any(issue.startswith(fatal_workload_prefixes) for issue in workload.diagnostics):
        return ReplanCandidateInputs(False, (), (), (), (), (), tuple(sorted(diagnostics)))
    if slot_minutes not in (15, 30, 60) or horizon_hours <= 0:
        diagnostics.add("INPUT_INVALID:REPLAN_CANDIDATE_TIMEBASE")
        return ReplanCandidateInputs(False, (), (), (), (), (), tuple(sorted(diagnostics)))
    horizon_slots = horizon_hours * 60 // slot_minutes
    has_weather_profiles = any(
        row.get("farm_id") == str(farm_id) and row.get("weather_profile_id")
        for row in entities.get("field_operations", [])
    )
    if weather_assessor is None and has_weather_profiles:
        scenarios = [
            row for row in entities.get("planning_scenarios", [])
            if row.get("farm_id") == str(farm_id)
            and (scenario_id is None or row.get("id") == str(scenario_id))
        ]
        if scenario_id is not None and not scenarios:
            diagnostics.add(f"INPUT_INVALID:PLANNING_SCENARIO_MISSING:{scenario_id}")
        if scenario_id is None:
            active_scenarios = [row for row in scenarios if row.get("active", False)]
            if len(active_scenarios) == 1:
                scenarios = active_scenarios
            elif len(scenarios) > 1:
                diagnostics.add("INPUT_INVALID:PLANNING_SCENARIO_AMBIGUOUS")
        scenario = scenarios[0] if len(scenarios) == 1 else None
        effective_weather_policy = weather_policy or (scenario or {}).get("weather_policy") or "ADVISORY"
        effective_data_horizon = weather_data_horizon_hours
        if effective_data_horizon is None and scenario is not None:
            configured_horizon = scenario.get("weather_data_horizon_hours")
            if configured_horizon is not None:
                try:
                    effective_data_horizon = int(configured_horizon)
                except (TypeError, ValueError):
                    diagnostics.add("INPUT_INVALID:WEATHER_DATA_HORIZON")
        if weather_zone_policy is None and scenario is not None:
            try:
                frozen_slots = int(scenario["frozen_hours"]) * 60 // slot_minutes
                firm_slots = int(scenario["firm_hours"]) * 60 // slot_minutes
                weather_zone_policy = ZonePolicy(frozen_slots, firm_slots, horizon_slots)
            except (KeyError, TypeError, ValueError):
                diagnostics.add("INPUT_INVALID:WEATHER_ZONE_POLICY")
        if weather_zone_policy is None:
            diagnostics.add("NOT_IMPLEMENTED:WEATHER_ZONE_POLICY_REQUIRED")
        else:
            try:
                # as_of_slot is rounded up for scheduling. Without the original
                # instant, use the preceding boundary so a later-issued forecast
                # cannot become eligible accidentally.
                assessment_time = as_of_at or (
                    horizon_start_at + timedelta(minutes=max(0, as_of_slot - 1) * slot_minutes)
                )
                weather_assessor = weather_assessor_from_seed(
                    entities,
                    farm_id,
                    horizon_start_at,
                    slot_minutes,
                    weather_zone_policy,
                    effective_weather_policy,
                    effective_data_horizon,
                    assessment_time,
                )
            except ValueError as error:
                diagnostics.add(str(error))
    expansion = expand_seed_resource_windows(entities, farm_id, horizon_start_at, horizon_hours)
    diagnostics.update(expansion.diagnostics)
    slot_windows = expansion.slot_windows(horizon_start_at, horizon_slots, slot_minutes)
    fields = workload.fields()
    start_provider = SeedFeasibleStartProvider(
        entities, farm_id, horizon_start_at, horizon_hours, slot_minutes, as_of_slot,
        slot_windows, fields,
    )
    operation_policy: dict[UUID, tuple[bool, int]] = {}
    for row in entities.get("field_operations", []):
        if row.get("farm_id") != str(farm_id):
            continue
        operation_id = UUID(row["id"])
        capacity = int(row.get("field_parallel_capacity", 1))
        if capacity < 1:
            diagnostics.add(f"INPUT_INVALID:FIELD_PARALLEL_CAPACITY:{operation_id}")
            continue
        operation_policy[operation_id] = (bool(row.get("allow_parallel_work_units", False)), capacity)
    unit_options = work_unit_options_from_seed(
        entities,
        workload.work_units,
        fields,
        {},
        operation_policy,
        farm_id,
        horizon_start_at,
        horizon_hours,
        slot_minutes,
        max_bundles_per_unit,
        feasible_start_provider=start_provider,
        weather_assessor=weather_assessor,
    )
    diagnostics.update(diagnostic for item in unit_options for diagnostic in item.diagnostics)

    hourly_costs: dict[UUID, int] = {}
    workers: dict[UUID, dict[str, Any]] = {}
    for resource_group in ("workers", "machines", "equipment"):
        for row in entities.get(resource_group, []):
            if row.get("farm_id") != str(farm_id) or not row.get("active", True):
                continue
            try:
                resource_id = UUID(row["id"])
                hourly_costs[resource_id] = int(row["cost_per_hour_cents"])
                if resource_group == "workers":
                    workers[resource_id] = row
            except (KeyError, ValueError, TypeError):
                diagnostics.add(f"INPUT_INVALID:RESOURCE_COST:{resource_group}:{row.get('id')}")

    calendar_timezone: dict[UUID, str] = {}
    for calendar in entities.get("resource_calendars", []):
        if calendar.get("farm_id") != str(farm_id):
            continue
        owner = next((calendar.get(key) for key in ("owner_worker_id", "owner_machine_id", "owner_equipment_id") if calendar.get(key)), None)
        if owner:
            try:
                calendar_timezone[UUID(owner)] = calendar["timezone"]
            except (KeyError, ValueError):
                diagnostics.add(f"INPUT_INVALID:RESOURCE_TIMEZONE:{calendar.get('id')}")
    work_limits: dict[UUID, WorkerWorkLimit] = {}
    working_windows: dict[UUID, tuple[tuple[datetime, datetime], ...]] = {}
    datetime_windows = expansion.windows()
    for worker_id, row in workers.items():
        timezone_name = calendar_timezone.get(worker_id)
        if timezone_name is None:
            diagnostics.add(f"WORK_LIMITS_MISSING:TIMEZONE:{worker_id}")
            continue
        try:
            work_limits[worker_id] = WorkerWorkLimit(
                timezone_name,
                int(row["max_daily_minutes"]),
                int(row["max_weekly_minutes"]),
                int(row.get("min_rest_minutes", 0)),
            )
            working_windows[worker_id] = datetime_windows.get(worker_id, ())
            diagnostics.update(rest_guarantee_diagnostics(
                working_windows[worker_id],
                work_limits[worker_id].min_rest_minutes,
                worker_id,
            ))
        except (KeyError, ValueError, TypeError):
            diagnostics.add(f"WORK_LIMITS_MISSING:{worker_id}")

    fatal_prefixes = (
        "INPUT_INVALID:", "RESOURCE_CALENDAR_MISSING:", "WORK_LIMITS_MISSING:",
        "REST_NOT_GUARANTEED_BY_CALENDAR:", "NOT_IMPLEMENTED:",
    )
    is_valid = not any(issue.startswith(fatal_prefixes) for issue in diagnostics)
    return ReplanCandidateInputs(
        is_valid,
        unit_options,
        tuple(sorted(slot_windows.items(), key=lambda item: str(item[0]))),
        tuple(sorted(hourly_costs.items(), key=lambda item: str(item[0]))),
        tuple(sorted(work_limits.items(), key=lambda item: str(item[0]))),
        tuple(sorted(working_windows.items(), key=lambda item: str(item[0]))),
        tuple(sorted(diagnostics)),
    )


def dependencies_from_seed(entities: dict[str, Any], farm_id: UUID) -> tuple[DependencyConstraint, ...]:
    dependencies: list[DependencyConstraint] = []
    for row in entities.get("operation_dependencies", []):
        if row.get("farm_id") != str(farm_id) or not row.get("is_hard", True):
            continue
        if row.get("type", "FINISH_TO_START") != "FINISH_TO_START":
            raise ValueError(f"NOT_IMPLEMENTED:DEPENDENCY_TYPE:{row.get('type')}")
        try:
            dependencies.append(DependencyConstraint(
                UUID(row["predecessor_operation_id"]),
                UUID(row["successor_operation_id"]),
                int(row.get("min_lag_minutes", 0)),
                int(row["max_lag_minutes"]) if row.get("max_lag_minutes") is not None else None,
            ))
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError("INPUT_INVALID:OPERATION_DEPENDENCY") from error
    return tuple(dependencies)


def optimize_seed_replan(
    context: ReplanContext,
    candidates: ReplanCandidateInputs,
    travel_inputs: ReplanTravelInputs,
    dependencies: tuple[DependencyConstraint, ...],
    operation_area_totals: dict[UUID, Decimal],
    horizon_start_at: datetime,
    horizon_hours: int,
    slot_minutes: int,
    max_seconds: float = 20,
    travel_category_by_resource: dict[UUID, str] | None = None,
    max_travel_pairs: int = 2_000_000,
) -> Any:
    """Run the actual work-unit optimizer with prepared seed/replan inputs."""
    from fieldscheduler.optimizer import OptimizationResult, optimize_work_units

    diagnostics = (*context.diagnostics, *candidates.diagnostics)
    if not context.valid or not candidates.valid:
        status = "LOCKED_CONFLICT" if any(item.startswith("LOCKED_CONFLICT:") for item in diagnostics) else "INPUT_INVALID"
        return OptimizationResult(status, (), (), "FULL", False, tuple(sorted(set(diagnostics))))
    if slot_minutes not in (15, 30, 60) or horizon_hours <= 0:
        return OptimizationResult("MODEL_INVALID", (), (), "FULL", False, ("INPUT_INVALID:REPLAN_SOLVER_TIMEBASE",))
    optimizer_inputs = {
        **context.optimizer_inputs(),
        **travel_inputs.optimizer_inputs(),
        **candidates.optimizer_inputs(),
    }
    if travel_category_by_resource is not None:
        optimizer_inputs["travel_category_by_resource"] = travel_category_by_resource
    result = optimize_work_units(
        candidates.work_unit_options,
        dependencies,
        operation_area_totals,
        slot_minutes=slot_minutes,
        horizon_slots=horizon_hours * 60 // slot_minutes,
        max_seconds=max_seconds,
        max_travel_pairs=max_travel_pairs,
        horizon_start_at=horizon_start_at,
        **optimizer_inputs,
    )
    merged_diagnostics = tuple(dict.fromkeys((*candidates.diagnostics, *result.diagnostics)))
    return OptimizationResult(
        result.solver_status,
        result.selected,
        result.objective_stages,
        result.search_scope,
        result.validated,
        merged_diagnostics,
        result.unit_dispositions,
        result.area_balances,
        result.travel_legs,
    )


def build_replan_context(
    entities: dict[str, Any],
    farm_id: UUID,
    operation_area_totals: dict[UUID, Decimal],
    as_of: datetime,
    horizon_start_at: datetime,
    horizon_hours: int,
    slot_minutes: int,
    zones: ZonePolicy,
    explicitly_unlocked_assignment_ids: frozenset[UUID] = frozenset(),
) -> ReplanContext:
    """Build fixed replan inputs without mutating baseline assignments or logs."""
    errors: set[str] = set()
    if as_of.utcoffset() is None or horizon_start_at.utcoffset() is None or slot_minutes not in (15, 30, 60) or horizon_hours <= 0:
        empty = ExecutionReconciliation(False, (), (), (), None, ("INPUT_INVALID:REPLAN_TIMEBASE",))
        return ReplanContext(False, empty, (), (), (), empty.diagnostics)
    as_of_utc = as_of.astimezone(timezone.utc)
    horizon_start_utc = horizon_start_at.astimezone(timezone.utc)
    horizon_end_utc = horizon_start_utc + timedelta(hours=horizon_hours)
    slot_seconds = slot_minutes * 60
    expected_horizon_slots = horizon_hours * 60 // slot_minutes
    if zones.horizon_slots != expected_horizon_slots:
        empty = ExecutionReconciliation(False, (), (), (), None, ("INPUT_INVALID:ZONE_HORIZON_MISMATCH",))
        return ReplanContext(False, empty, (), (), (), empty.diagnostics)
    as_of_delta = (as_of_utc - horizon_start_utc).total_seconds()
    if as_of_delta < 0:
        empty = ExecutionReconciliation(False, (), (), (), None, ("INPUT_INVALID:AS_OF_SLOT_ALIGNMENT",))
        return ReplanContext(False, empty, (), (), (), empty.diagnostics)
    if as_of_utc >= horizon_end_utc:
        empty = ExecutionReconciliation(False, (), (), (), None, ("HORIZON_ALREADY_ENDED",))
        return ReplanContext(False, empty, (), (), (), empty.diagnostics)
    as_of_slot = ceil(as_of_delta / slot_seconds)

    try:
        executions, work_logs = execution_facts_from_seed(entities, farm_id)
    except ValueError as error:
        empty = ExecutionReconciliation(False, (), (), (), None, (str(error),))
        return ReplanContext(False, empty, (), (), (), empty.diagnostics)
    facts = reconcile_execution_facts(executions, work_logs, as_of_utc)
    errors.update(facts.diagnostics)
    if facts.latest_reality_at is not None and facts.latest_reality_at > as_of_utc:
        errors.add("AS_OF_BEFORE_LATEST_REALITY")

    assignments = {
        item["id"]: item
        for item in entities.get("operation_assignments", [])
        if item.get("farm_id") == str(farm_id)
    }
    field_entrances: dict[UUID, list[UUID]] = {}
    for location in entities.get("locations", []):
        if location.get("farm_id") == str(farm_id) and location.get("kind") == "FIELD_ENTRANCE" and location.get("field_id"):
            try:
                field_entrances.setdefault(UUID(location["field_id"]), []).append(UUID(location["id"]))
            except (KeyError, ValueError):
                errors.add("INPUT_INVALID:FIELD_ENTRANCE")
    workers_by_assignment: dict[str, list[UUID]] = {}
    machines_by_assignment: dict[str, list[UUID]] = {}
    equipment_by_assignment: dict[str, list[UUID]] = {}
    for entity_name, target in (
        ("assignment_workers", workers_by_assignment),
        ("assignment_machines", machines_by_assignment),
        ("assignment_equipment", equipment_by_assignment),
    ):
        for row in entities.get(entity_name, []):
            if row.get("farm_id") != str(farm_id):
                continue
            try:
                target.setdefault(str(row["assignment_id"]), []).append(UUID(row["resource_id" if "resource_id" in row else entity_name.removeprefix("assignment_").removesuffix("s") + "_id"]))
            except (KeyError, ValueError):
                errors.add(f"INPUT_INVALID:{entity_name}:RESOURCE_LINK")

    execution_states = {state.source_assignment_id: state for state in facts.states}
    fixed: list[SelectedOption] = []
    extra_reservations: list[FixedAreaReservation] = []
    verified_locations: dict[UUID, UUID] = {}
    baseline_ids: set[UUID] = set()
    for assignment_id, row in assignments.items():
        try:
            op_id = UUID(row.get("field_operation_id", row.get("operation_id")))
            assignment_uuid = UUID(assignment_id)
            baseline_ids.add(assignment_uuid)
            field_id = UUID(row["field_id"])
            start_at = datetime.fromisoformat(row["starts_at"].replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(row["ends_at"].replace("Z", "+00:00"))
            area = Decimal(str(row["area_ha"]))
        except (KeyError, ValueError, ArithmeticError, TypeError):
            errors.add(f"INPUT_INVALID:BASELINE_ASSIGNMENT:{assignment_id}")
            continue
        if start_at.utcoffset() is None or end_at.utcoffset() is None or start_at >= end_at or not area.is_finite() or area <= 0:
            errors.add(f"INPUT_INVALID:BASELINE_ASSIGNMENT:{assignment_id}")
            continue
        start_at, end_at = start_at.astimezone(timezone.utc), end_at.astimezone(timezone.utc)
        if op_id not in operation_area_totals:
            errors.add(f"AREA_BALANCE_INVALID:BASELINE_OPERATION_UNKNOWN:{op_id}")
            continue
        state = execution_states.get(assignment_uuid)
        ongoing = state is not None and state.state == "IN_PROGRESS" and state.reserved_area_remaining_ha > 0
        if end_at <= as_of_utc and not ongoing:
            # Historical baseline rows are not reservations. Their accepted
            # completion belongs to the execution ledger, when one exists.
            if (row.get("locked") or row.get("is_manual")) and state is None:
                errors.add(f"EXECUTION_FACT_MISSING_FOR_PAST_LOCK:{assignment_id}")
            continue
        if state is not None and state.state == "CLOSED":
            continue
        zone_start = max(0, min(zones.horizon_slots, int((start_at - horizon_start_utc).total_seconds() // slot_seconds)))
        zone_end = max(0, min(zones.horizon_slots, ceil((end_at - horizon_start_utc).total_seconds() / slot_seconds)))
        if zone_end <= zone_start:
            if ongoing or row.get("locked") or row.get("is_manual"):
                errors.add(f"LOCKED_CONFLICT:BASELINE_OUTSIDE_HORIZON:{assignment_id}")
            continue
        frozen = zones.for_interval(zone_start, zone_end) == PlanningZone.FROZEN
        is_manual_lock = bool(row.get("locked", False) or row.get("is_manual", False))
        is_locked = ongoing or frozen or is_manual_lock
        if not is_locked or (not ongoing and assignment_uuid in explicitly_unlocked_assignment_ids):
            continue

        exact_seconds = (end_at - horizon_start_utc).total_seconds()
        if exact_seconds % slot_seconds:
            errors.add(f"LOCKED_CONFLICT:BASELINE_SLOT_ALIGNMENT:{assignment_id}")
            continue
        end_slot = int(exact_seconds // slot_seconds)
        if ongoing:
            start_slot = as_of_slot
            locked_area = state.reserved_area_remaining_ha
            if end_slot <= start_slot:
                errors.add(f"LOCKED_CONFLICT:ONGOING_NO_REMAINING_WINDOW:{assignment_id}")
                continue
        else:
            start_seconds = (start_at - horizon_start_utc).total_seconds()
            if start_seconds < 0 or start_seconds % slot_seconds:
                errors.add(f"LOCKED_CONFLICT:BASELINE_SLOT_ALIGNMENT:{assignment_id}")
                continue
            start_slot = int(start_seconds // slot_seconds)
            locked_area = area
            if start_slot < as_of_slot:
                errors.add(f"LOCKED_CONFLICT:BASELINE_LOCK_OVERLAPS_AS_OF:{assignment_id}")
                continue
        if start_slot < 0 or end_slot > zones.horizon_slots:
            errors.add(f"LOCKED_CONFLICT:BASELINE_OUTSIDE_HORIZON:{assignment_id}")
            continue

        worker_ids = tuple(workers_by_assignment.get(assignment_id, ()))
        machine_ids = tuple(machines_by_assignment.get(assignment_id, ()))
        equipment_ids = tuple(equipment_by_assignment.get(assignment_id, ()))
        if not worker_ids or not machine_ids:
            errors.add(f"LOCKED_CONFLICT:BASELINE_RESOURCES_MISSING:{assignment_id}")
            continue
        source_bundle_id = UUID(row["bundle_id"]) if row.get("bundle_id") else assignment_uuid
        estimate = int(row.get("estimated_cost_cents", 0))
        if ongoing and area > 0:
            estimate = int((Decimal(estimate) * locked_area / area).to_integral_value(rounding=ROUND_CEILING))
        fixed.append(SelectedOption(
            op_id, source_bundle_id, field_id, start_slot, end_slot, str(locked_area),
            worker_ids, machine_ids, equipment_ids, estimate,
            UUID(row["work_unit_id"]) if row.get("work_unit_id") else None,
            row.get("weather_status", "UNVERIFIED"),
        ))
        if ongoing:
            entrances = field_entrances.get(field_id, [])
            if len(entrances) != 1:
                errors.add(f"LOCATION_STALE:ONGOING_FIELD_ENTRANCE:{field_id}")
            else:
                for resource_id in (*worker_ids, *machine_ids, *equipment_ids):
                    previous_location = verified_locations.get(resource_id)
                    if previous_location is not None and previous_location != entrances[0]:
                        errors.add(f"LOCATION_STALE:CONFLICTING_ONGOING_EXECUTIONS:{resource_id}")
                    verified_locations[resource_id] = entrances[0]
        if not ongoing:
            extra_reservations.append(FixedAreaReservation(assignment_uuid, assignment_uuid, op_id, locked_area))

    for state in facts.states:
        if state.state == "IN_PROGRESS" and state.reserved_area_remaining_ha > 0 and state.source_assignment_id not in baseline_ids:
            errors.add(f"EXECUTION_SOURCE_ASSIGNMENT_MISSING:{state.source_assignment_id}")

    if errors:
        diagnostics = tuple(sorted(errors))
        return ReplanContext(False, facts, (), (), (), diagnostics)
    try:
        area_context = facts.area_context(operation_area_totals, tuple(extra_reservations))
    except ValueError as error:
        diagnostics = (str(error),)
        return ReplanContext(False, facts, (), (), (), diagnostics)
    if horizon_end_utc <= as_of_utc:
        diagnostics = ("HORIZON_ALREADY_ENDED",)
        return ReplanContext(False, facts, (), (), (), diagnostics)
    return ReplanContext(
        True,
        facts,
        tuple(sorted(fixed, key=lambda item: (item.start_slot, str(item.operation_id), str(item.bundle_id)))),
        tuple(extra_reservations),
        tuple(sorted(area_context.items(), key=lambda item: str(item[0]))),
        (),
        tuple(sorted(verified_locations.items(), key=lambda item: str(item[0]))),
        as_of_slot,
    )
