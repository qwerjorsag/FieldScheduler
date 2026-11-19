from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from statistics import median
from typing import Callable
from uuid import UUID, uuid5


@dataclass(frozen=True)
class AreaBalance:
    operation_id: UUID
    total_area_ha: Decimal
    completed_ha: Decimal = Decimal("0")
    fixed_reserved_ha: Decimal = Decimal("0")
    newly_planned_ha: Decimal = Decimal("0")
    beyond_horizon_ha: Decimal = Decimal("0")
    deferred_ha: Decimal = Decimal("0")

    @property
    def accounted_area_ha(self) -> Decimal:
        return self.completed_ha + self.fixed_reserved_ha + self.newly_planned_ha + self.beyond_horizon_ha + self.deferred_ha

    @property
    def is_balanced(self) -> bool:
        return self.accounted_area_ha == self.total_area_ha


@dataclass(frozen=True)
class WorkUnit:
    id: UUID
    operation_id: UUID
    execution_id: UUID
    ordinal: int
    area_ha: Decimal
    chunk_revision: int


@dataclass(frozen=True)
class RechunkResult:
    work_units: tuple[WorkUnit, ...]
    heuristic_chunking: bool
    failed_unit_ids: tuple[UUID, ...]


def adaptively_rechunk_work_units(
    work_units: tuple[WorkUnit, ...],
    can_fit: Callable[[WorkUnit], bool | None],
    min_chunk_ha_by_operation: dict[UUID, Decimal],
    splittable_operations: frozenset[UUID],
    max_rechunk_depth: int = 2,
) -> RechunkResult:
    if max_rechunk_depth < 0:
        raise ValueError("INPUT_INVALID:MAX_RECHUNK_DEPTH")
    output: list[WorkUnit] = []
    failed: list[UUID] = []
    changed = False
    for original in work_units:
        stack: list[tuple[WorkUnit, int]] = [(original, 0)]
        parts: list[WorkUnit] = []
        while stack:
            current, depth = stack.pop()
            fit = can_fit(current)
            minimum = min_chunk_ha_by_operation.get(current.operation_id, Decimal("0.0001"))
            if fit is not False or current.operation_id not in splittable_operations or depth >= max_rechunk_depth:
                if fit is False:
                    failed.append(current.id)
                parts.append(current)
                continue
            first_area = (current.area_ha / 2).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            second_area = current.area_ha - first_area
            if first_area < minimum or second_area < minimum:
                failed.append(current.id)
                parts.append(current)
                continue
            changed = True
            revision = current.chunk_revision + 1
            first_id = uuid5(current.id, f"rechunk:{revision}:0")
            second_id = uuid5(current.id, f"rechunk:{revision}:1")
            first = WorkUnit(first_id, current.operation_id, current.execution_id, current.ordinal, first_area, revision)
            second = WorkUnit(second_id, current.operation_id, current.execution_id, current.ordinal, second_area, revision)
            stack.append((second, depth + 1))
            stack.append((first, depth + 1))
        parts.sort(key=lambda item: (item.ordinal, str(item.id)))
        output.extend(parts)

    result: list[WorkUnit] = []
    ordinals: dict[UUID, int] = {}
    for unit in sorted(output, key=lambda item: (str(item.operation_id), item.ordinal, str(item.id))):
        ordinal = ordinals.get(unit.operation_id, 0)
        result.append(WorkUnit(unit.id, unit.operation_id, unit.execution_id, ordinal, unit.area_ha, unit.chunk_revision))
        ordinals[unit.operation_id] = ordinal + 1
    return RechunkResult(tuple(result), changed, tuple(failed))


def validate_area_balance(balance: AreaBalance) -> tuple[str, ...]:
    values = (
        balance.total_area_ha,
        balance.completed_ha,
        balance.fixed_reserved_ha,
        balance.newly_planned_ha,
        balance.beyond_horizon_ha,
        balance.deferred_ha,
    )
    if any(value < 0 for value in values):
        return (f"AREA_BALANCE_INVALID:NEGATIVE:{balance.operation_id}",)
    if balance.accounted_area_ha > balance.total_area_ha:
        return (f"AREA_BALANCE_INVALID:OVERALLOCATED:{balance.operation_id}",)
    if balance.accounted_area_ha != balance.total_area_ha:
        return (f"AREA_BALANCE_INVALID:UNACCOUNTED:{balance.operation_id}",)
    return ()


def generate_work_units(
    operation_id: UUID,
    run_id: UUID,
    remaining_area_ha: Decimal,
    eligible_capacities_ha_per_hour: tuple[Decimal, ...],
    can_split: bool,
    target_chunk_hours: Decimal = Decimal("4"),
    min_chunk_ha: Decimal = Decimal("0.0001"),
    max_chunk_ha: Decimal | None = None,
    chunk_revision: int = 1,
) -> tuple[WorkUnit, ...]:
    if remaining_area_ha <= 0:
        if remaining_area_ha == 0:
            return ()
        raise ValueError(f"AREA_BALANCE_INVALID:NEGATIVE_REMAINDER:{operation_id}")
    if min_chunk_ha <= 0 or target_chunk_hours <= 0 or chunk_revision < 1:
        raise ValueError(f"INPUT_INVALID:CHUNK_CONFIGURATION:{operation_id}")
    if max_chunk_ha is not None and max_chunk_ha < min_chunk_ha:
        raise ValueError(f"INPUT_INVALID:MAX_CHUNK_BELOW_MIN:{operation_id}")

    if not can_split:
        sizes = [remaining_area_ha]
    else:
        capacities = sorted(capacity for capacity in eligible_capacities_ha_per_hour if capacity > 0)
        if not capacities:
            raise ValueError(f"PRODUCTIVITY_UNKNOWN:{operation_id}")
        target_area = median(capacities) * target_chunk_hours
        if max_chunk_ha is not None:
            target_area = min(target_area, max_chunk_ha)
        target_area = max(target_area, min_chunk_ha)
        count, remainder = divmod(remaining_area_ha, target_area)
        sizes = [target_area] * int(count)
        if remainder:
            if remainder < min_chunk_ha:
                if not sizes:
                    raise ValueError(f"AREA_BALANCE_INVALID:BELOW_MIN_CHUNK:{operation_id}")
                sizes[-1] += remainder
            else:
                sizes.append(remainder)

    if max_chunk_ha is not None and any(size > max_chunk_ha for size in sizes):
        raise ValueError(f"AREA_BALANCE_INVALID:MAX_CHUNK_EXCEEDED:{operation_id}")

    if sum(sizes, Decimal("0")) != remaining_area_ha:
        raise ValueError(f"AREA_BALANCE_INVALID:CHUNK_SUM:{operation_id}")
    if any(size < min_chunk_ha for size in sizes):
        raise ValueError(f"AREA_BALANCE_INVALID:BELOW_MIN_CHUNK:{operation_id}")

    execution_id = uuid5(operation_id, "operation-execution")
    return tuple(
        WorkUnit(
            uuid5(run_id, f"work-unit:{operation_id}:{chunk_revision}:{ordinal}"),
            operation_id,
            execution_id,
            ordinal,
            area,
            chunk_revision,
        )
        for ordinal, area in enumerate(sizes)
    )
