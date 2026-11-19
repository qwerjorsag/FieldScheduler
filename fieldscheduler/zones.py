from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence, TypeVar


class PlanningZone(StrEnum):
    FROZEN = "FROZEN"
    FIRM = "FIRM"
    TENTATIVE = "TENTATIVE"


@dataclass(frozen=True)
class ZonePolicy:
    frozen_slots: int
    firm_slots: int
    horizon_slots: int

    def __post_init__(self) -> None:
        if not 0 <= self.frozen_slots <= self.firm_slots <= self.horizon_slots:
            raise ValueError("INPUT_INVALID:ZONE_BOUNDARIES")

    def for_interval(self, start_slot: int, end_slot: int) -> PlanningZone:
        if start_slot < 0 or end_slot <= start_slot or end_slot > self.horizon_slots:
            raise ValueError("INPUT_INVALID:ZONE_INTERVAL")
        # Start time chooses the normal zone; crossing a boundary upgrades the
        # interval to the strictest zone it touches.
        if start_slot < self.frozen_slots or end_slot <= self.frozen_slots:
            return PlanningZone.FROZEN
        if start_slot < self.firm_slots or end_slot <= self.firm_slots:
            return PlanningZone.FIRM
        return PlanningZone.TENTATIVE


_Assignment = TypeVar("_Assignment")


def select_fixed_baseline_assignments(
    assignments: Sequence[_Assignment],
    zones: ZonePolicy,
    manual_lock_keys: frozenset[tuple[object, object, int]] = frozenset(),
    ongoing_keys: frozenset[tuple[object, object, int]] = frozenset(),
    explicitly_unlocked_keys: frozenset[tuple[object, object, int]] = frozenset(),
) -> tuple[_Assignment, ...]:
    """Select unchanged baseline rows that must be passed as fixed intervals."""
    fixed: list[_Assignment] = []
    for assignment in assignments:
        key = (assignment.operation_id, assignment.bundle_id, assignment.start_slot)
        if key in explicitly_unlocked_keys:
            continue
        zone = zones.for_interval(assignment.start_slot, assignment.end_slot)
        if zone == PlanningZone.FROZEN or key in manual_lock_keys or key in ongoing_keys:
            fixed.append(assignment)
    return tuple(fixed)


def weather_policy_for_zone(policy: str, zone: PlanningZone) -> tuple[str, str]:
    normalized = policy.upper()
    if normalized not in {"IGNORE", "ADVISORY", "STRICT"}:
        raise ValueError("INPUT_INVALID:WEATHER_POLICY")
    if normalized == "IGNORE":
        return "IGNORE", "IGNORED"
    if normalized == "STRICT" and zone == PlanningZone.TENTATIVE:
        return "ADVISORY", "UNVERIFIED"
    return normalized, "UNVERIFIED" if normalized == "ADVISORY" else "PENDING"
