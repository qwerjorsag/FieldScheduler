from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Sequence
from uuid import UUID

from fieldscheduler.optimizer import SelectedOption
from fieldscheduler.travel import TravelClosure


@dataclass(frozen=True)
class TravelLegCertificate:
    origin_id: UUID
    destination_id: UUID
    departure_slot: int
    arrival_slot: int
    departure_at: datetime
    arrival_at: datetime
    duration_minutes: int
    source_path: tuple[UUID, ...]
    resource_ids: tuple[UUID, ...]
    driver_worker_id: UUID
    towing_machine_id: UUID
    equipment_ids: tuple[UUID, ...]
    cost_cents: int


@dataclass(frozen=True)
class TravelCertificate:
    valid: bool
    legs: tuple[TravelLegCertificate, ...]
    diagnostics: tuple[str, ...]


def _intersections(
    resource_ids: Sequence[UUID],
    windows_by_resource: dict[UUID, tuple[tuple[int, int], ...]],
) -> tuple[tuple[int, int], ...]:
    if not resource_ids:
        return ()
    current = list(windows_by_resource.get(resource_ids[0], ()))
    for resource_id in resource_ids[1:]:
        next_windows = windows_by_resource.get(resource_id, ())
        current = [
            (max(start_a, start_b), min(end_a, end_b))
            for start_a, end_a in current
            for start_b, end_b in next_windows
            if max(start_a, start_b) < min(end_a, end_b)
        ]
    return tuple(sorted(current))


def _k1_departure(
    previous_end: int,
    next_start: int,
    duration_slots: int,
    common_windows: tuple[tuple[int, int], ...],
    initial: bool = False,
) -> int | None:
    possibilities: set[int] = set()
    for window_start, window_end in common_windows:
        if initial:
            if window_start >= previous_end and window_start + duration_slots <= min(window_end, next_start):
                possibilities.add(window_start)
            elif window_start < previous_end < window_end and previous_end + duration_slots <= min(window_end, next_start):
                possibilities.add(previous_end)
            continue
        if window_end - duration_slots >= previous_end:
            last_activity_departure = window_end - duration_slots
            if last_activity_departure + duration_slots <= next_start:
                possibilities.add(last_activity_departure)
        first_activity_departure = max(previous_end, window_start)
        if first_activity_departure == window_start and first_activity_departure + duration_slots <= min(window_end, next_start):
            possibilities.add(first_activity_departure)
        latest_before_next = next_start - duration_slots
        if max(previous_end, window_start) <= latest_before_next and latest_before_next + duration_slots <= window_end:
            possibilities.add(latest_before_next)
    return min(possibilities) if possibilities else None


def build_travel_certificate(
    assignments: Sequence[SelectedOption],
    travel_closure: TravelClosure,
    location_by_field: dict[UUID, UUID],
    initial_location_by_resource: dict[UUID, UUID],
    availability_windows_by_resource: dict[UUID, tuple[tuple[int, int], ...]],
    hourly_cost_cents_by_resource: dict[UUID, int],
    horizon_start_at: datetime,
    slot_minutes: int,
    travel_category_by_machine: dict[UUID, str] | None = None,
    as_of_slot: int = 0,
) -> TravelCertificate:
    if slot_minutes not in (15, 30, 60) or horizon_start_at.utcoffset() is None:
        return TravelCertificate(False, (), ("INPUT_INVALID:TRAVEL_TIMEBASE",))
    if any(assignment.field_id not in location_by_field for assignment in assignments):
        return TravelCertificate(False, (), ("TRAVEL_DATA_MISSING:FIELD_LOCATION",))
    diagnostics: list[str] = []
    legs: list[TravelLegCertificate] = []
    machine_assignments: dict[UUID, list[SelectedOption]] = {}
    for assignment in assignments:
        if len(assignment.machine_ids) != 1 or len(assignment.worker_ids) != 1 or len(assignment.equipment_ids) > 1:
            diagnostics.append(f"TRANSPORT_MODE_UNSUPPORTED:{assignment.operation_id}")
            continue
        machine_assignments.setdefault(assignment.machine_ids[0], []).append(assignment)
    if diagnostics:
        return TravelCertificate(False, (), tuple(diagnostics))

    current_locations = dict(initial_location_by_resource)
    horizon_start_utc = horizon_start_at.astimezone(timezone.utc)
    categories = travel_category_by_machine or {}
    for machine_id, machine_jobs in machine_assignments.items():
        machine_jobs.sort(key=lambda item: (item.start_slot, item.end_slot, str(item.operation_id)))
        previous_assignment: SelectedOption | None = None
        for assignment in machine_jobs:
            field_location = location_by_field[assignment.field_id]
            machine_origin = current_locations.get(machine_id)
            if machine_origin is None:
                diagnostics.append(f"LOCATION_STALE:{machine_id}")
                break
            equipment_ids = assignment.equipment_ids
            participants = (machine_id, assignment.worker_ids[0], *equipment_ids)
            for resource_id in participants:
                resource_location = current_locations.get(resource_id)
                if resource_location is None:
                    diagnostics.append(f"LOCATION_STALE:{resource_id}:{work_item_id}")
                    break
                if resource_location != machine_origin:
                    diagnostics.append(
                        f"TRANSPORT_MODE_UNSUPPORTED:DISCONNECTED_RESOURCE:{resource_id}:{machine_origin}:{work_item_id}"
                    )
                    break
            if diagnostics:
                break
            route = travel_closure.route(machine_origin, field_location, categories.get(machine_id, "ANY"))
            work_item_id = assignment.work_unit_id or assignment.operation_id
            if route is None:
                diagnostics.append(f"TRAVEL_UNREACHABLE:{machine_id}:{work_item_id}")
                break
            if route.duration_minutes:
                if previous_assignment is not None:
                    if previous_assignment.equipment_ids != equipment_ids and (previous_assignment.equipment_ids or equipment_ids):
                        diagnostics.append(f"EQUIPMENT_TRANSPORT_UNSUPPORTED:{machine_id}:{work_item_id}")
                        break
                    if previous_assignment.worker_ids[0] != assignment.worker_ids[0]:
                        diagnostics.append(
                            "TRANSPORT_MODE_UNSUPPORTED:DRIVER_CHANGE:"
                            f"{previous_assignment.work_unit_id or previous_assignment.operation_id}:{work_item_id}"
                        )
                        break
                duration_slots = (route.duration_minutes + slot_minutes - 1) // slot_minutes
                if previous_assignment is None:
                    previous_end = as_of_slot
                    next_start = assignment.start_slot
                else:
                    previous_end = previous_assignment.end_slot
                    next_start = assignment.start_slot
                common_windows = _intersections(participants, availability_windows_by_resource)
                departure = _k1_departure(previous_end, next_start, duration_slots, common_windows, previous_assignment is None)
                if departure is None or departure < as_of_slot:
                    diagnostics.append(f"TRAVEL_IMPOSSIBLE:{machine_id}:{work_item_id}")
                    break
                arrival_slot = departure + duration_slots
                duration_cost = sum(hourly_cost_cents_by_resource.get(resource_id, 0) for resource_id in participants)
                missing_costs = set(participants) - set(hourly_cost_cents_by_resource)
                if missing_costs:
                    diagnostics.append("TRAVEL_COST_UNKNOWN:" + ",".join(sorted(map(str, missing_costs))))
                    break
                cost = int((Decimal(duration_cost * route.duration_minutes) / Decimal(60)).to_integral_value(rounding=ROUND_CEILING))
                if any(
                    bool(set(participants) & set((*job.worker_ids, *job.machine_ids, *job.equipment_ids)))
                    and departure < job.end_slot
                    and job.start_slot < arrival_slot
                    for job in assignments
                ):
                    diagnostics.append(f"TRAVEL_WORK_OVERLAP:{machine_id}:{work_item_id}")
                    break
                legs.append(
                    TravelLegCertificate(
                        machine_origin,
                        field_location,
                        departure,
                        arrival_slot,
                        slot_to_utc(departure, horizon_start_utc, slot_minutes),
                        slot_to_utc(departure, horizon_start_utc, slot_minutes) + timedelta(minutes=route.duration_minutes),
                        route.duration_minutes,
                        route.path,
                        tuple(participants),
                        assignment.worker_ids[0],
                        machine_id,
                        tuple(equipment_ids),
                        cost,
                    )
                )
            for resource_id in participants:
                current_locations[resource_id] = field_location
            previous_assignment = assignment

    if diagnostics:
        return TravelCertificate(False, tuple(legs), tuple(diagnostics))
    legs.sort(key=lambda leg: (leg.departure_slot, str(leg.towing_machine_id), str(leg.origin_id), str(leg.destination_id)))
    legs_by_resource: dict[UUID, list[TravelLegCertificate]] = {}
    for leg in legs:
        for resource_id in leg.resource_ids:
            legs_by_resource.setdefault(resource_id, []).append(leg)
    for resource_id, resource_legs in legs_by_resource.items():
        ordered = sorted(resource_legs, key=lambda leg: (leg.departure_slot, leg.arrival_slot))
        for first, second in zip(ordered, ordered[1:]):
            if first.arrival_slot > second.departure_slot:
                diagnostics.append(f"TRAVEL_RESOURCE_OVERLAP:{resource_id}")
    return TravelCertificate(not diagnostics, tuple(legs), tuple(diagnostics))


def validate_travel_certificate(
    certificate: TravelCertificate,
    assignments: Sequence[SelectedOption],
    travel_closure: TravelClosure,
    location_by_field: dict[UUID, UUID],
    initial_location_by_resource: dict[UUID, UUID],
    availability_windows_by_resource: dict[UUID, tuple[tuple[int, int], ...]],
    horizon_start_at: datetime,
    slot_minutes: int,
    travel_category_by_machine: dict[UUID, str] | None = None,
) -> tuple[str, ...]:
    """Independently check a proposed certificate against assignments and routes."""
    issues: list[str] = []
    if slot_minutes not in (15, 30, 60) or horizon_start_at.utcoffset() is None:
        return ("INPUT_INVALID:TRAVEL_VALIDATION_TIMEBASE",)
    if not certificate.valid:
        return ("TRAVEL_CERTIFICATE_INVALID",)

    by_machine: dict[UUID, list[SelectedOption]] = {}
    for assignment in assignments:
        if len(assignment.machine_ids) != 1 or len(assignment.worker_ids) != 1:
            issues.append(f"TRAVEL_ASSIGNMENT_UNSUPPORTED:{assignment.operation_id}")
            continue
        by_machine.setdefault(assignment.machine_ids[0], []).append(assignment)
    legs_for_assignment: dict[UUID, TravelLegCertificate] = {}
    for leg in certificate.legs:
        if leg.driver_worker_id not in leg.resource_ids or leg.towing_machine_id not in leg.resource_ids:
            issues.append(f"TRAVEL_LEG_RESOURCES_INVALID:{leg.towing_machine_id}")
        if len(leg.source_path) < 2 or leg.source_path[0] != leg.origin_id or leg.source_path[-1] != leg.destination_id:
            issues.append(f"TRAVEL_PATH_INVALID:{leg.towing_machine_id}")
        if leg.arrival_slot <= leg.departure_slot or leg.duration_minutes <= 0:
            issues.append(f"TRAVEL_TIME_INVALID:{leg.towing_machine_id}")
        duration_slots = (leg.duration_minutes + slot_minutes - 1) // slot_minutes
        if leg.arrival_slot - leg.departure_slot != duration_slots:
            issues.append(f"TRAVEL_SLOT_DURATION_MISMATCH:{leg.towing_machine_id}")
        if leg.departure_at != slot_to_utc(leg.departure_slot, horizon_start_at, slot_minutes):
            issues.append(f"TRAVEL_DEPARTURE_TIMESTAMP_MISMATCH:{leg.towing_machine_id}")
        if leg.arrival_at != leg.departure_at + timedelta(minutes=leg.duration_minutes):
            issues.append(f"TRAVEL_ARRIVAL_TIMESTAMP_MISMATCH:{leg.towing_machine_id}")
        if leg.departure_at.astimezone(timezone.utc) < horizon_start_at.astimezone(timezone.utc):
            issues.append(f"TRAVEL_BEFORE_HORIZON:{leg.towing_machine_id}")

    for machine_id, jobs in by_machine.items():
        ordered = sorted(jobs, key=lambda item: (item.start_slot, item.end_slot, str(item.operation_id)))
        location = initial_location_by_resource.get(machine_id)
        if location is None:
            issues.append(f"TRAVEL_INITIAL_LOCATION_MISSING:{machine_id}")
            continue
        previous_end = 0
        for assignment in ordered:
            destination = location_by_field.get(assignment.field_id)
            if destination is None:
                issues.append(f"TRAVEL_FIELD_LOCATION_MISSING:{assignment.field_id}")
                continue
            candidates = [
                leg for leg in certificate.legs
                if leg.towing_machine_id == machine_id
                and leg.destination_id == destination
                and leg.driver_worker_id == assignment.worker_ids[0]
                and tuple(assignment.equipment_ids) == tuple(leg.equipment_ids)
                and leg.arrival_slot <= assignment.start_slot
                and leg.departure_slot >= previous_end
            ]
            route = travel_closure.route(location, destination, (travel_category_by_machine or {}).get(machine_id, "ANY"))
            if location == destination:
                if candidates:
                    issues.append(f"UNEXPECTED_ZERO_TRAVEL_LEG:{machine_id}:{assignment.operation_id}")
            elif route is None:
                issues.append(f"TRAVEL_ROUTE_MISSING:{machine_id}:{location}:{destination}")
            elif not candidates:
                issues.append(f"TRAVEL_LEG_MISSING:{machine_id}:{assignment.operation_id}")
            else:
                leg = min(candidates, key=lambda item: (item.departure_slot, item.arrival_slot))
                if leg.origin_id != location or leg.duration_minutes != route.duration_minutes or leg.source_path != route.path:
                    issues.append(f"TRAVEL_ROUTE_CERTIFICATE_MISMATCH:{machine_id}:{assignment.operation_id}")
                if set(leg.resource_ids) != {machine_id, assignment.worker_ids[0], *assignment.equipment_ids}:
                    issues.append(f"TRAVEL_PARTICIPANTS_MISMATCH:{machine_id}:{assignment.operation_id}")
                for resource_id in leg.resource_ids:
                    if not any(start <= leg.departure_slot and leg.arrival_slot <= end for start, end in availability_windows_by_resource.get(resource_id, ())):
                        issues.append(f"TRAVEL_OUTSIDE_AVAILABILITY:{resource_id}:{machine_id}")
                if any(
                    resource_id in (*job.worker_ids, *job.machine_ids, *job.equipment_ids)
                    and leg.departure_slot < job.end_slot
                    and job.start_slot < leg.arrival_slot
                    for job in assignments
                    for resource_id in leg.resource_ids
                ):
                    issues.append(f"TRAVEL_OVERLAPS_WORK:{machine_id}:{assignment.operation_id}")
                legs_for_assignment[assignment.operation_id] = leg
            location = destination
            previous_end = assignment.end_slot

    # Every certified leg must lead to a selected job; this rejects orphan or duplicated legs.
    for leg in certificate.legs:
        matching = [
            assignment for assignment in assignments
            if leg.towing_machine_id in assignment.machine_ids
            and assignment.field_id in location_by_field
            and location_by_field[assignment.field_id] == leg.destination_id
            and assignment.start_slot >= leg.arrival_slot
            and assignment.worker_ids == (leg.driver_worker_id,)
            and assignment.equipment_ids == leg.equipment_ids
        ]
        if not matching:
            issues.append(f"TRAVEL_LEG_ORPHANED:{leg.towing_machine_id}:{leg.destination_id}")

    resource_legs: dict[UUID, list[TravelLegCertificate]] = {}
    for leg in certificate.legs:
        for resource_id in leg.resource_ids:
            resource_legs.setdefault(resource_id, []).append(leg)
    for resource_id, legs in resource_legs.items():
        legs.sort(key=lambda item: (item.departure_slot, item.arrival_slot))
        for first, second in zip(legs, legs[1:]):
            if first.arrival_slot > second.departure_slot:
                issues.append(f"TRAVEL_RESOURCE_OVERLAP:{resource_id}")
    return tuple(dict.fromkeys(issues))


def slot_to_utc(slot: int, horizon_start_at: datetime, slot_minutes: int) -> datetime:
    return horizon_start_at.astimezone(timezone.utc) + timedelta(minutes=slot * slot_minutes)


def initial_locations_from_seed(
    entities: dict[str, object],
    farm_id: UUID,
    as_of: datetime,
    verified_locations: dict[UUID, UUID] | None = None,
) -> dict[UUID, UUID]:
    if as_of.utcoffset() is None:
        raise ValueError("INPUT_INVALID:AS_OF_MUST_BE_AWARE")
    as_of_utc = as_of.astimezone(timezone.utc)
    locations = dict(verified_locations or {})
    for entity_name, location_field, observed_field, allow_home in (
        ("workers", "home_location_id", None, True),
        ("machines", "current_location_id", "current_location_observed_at", True),
        ("equipment", "current_location_id", "current_location_observed_at", False),
    ):
        for resource in entities.get(entity_name, []):
            if resource.get("farm_id") != str(farm_id) or not resource.get("active", True):
                continue
            resource_id = UUID(resource["id"])
            if resource_id in locations:
                continue
            current_location = resource.get(location_field)
            if observed_field and current_location is not None and not resource.get(observed_field):
                current_location = None
            if current_location is not None:
                observed_value = resource.get(observed_field) if observed_field else None
                if observed_value:
                    observed_at = datetime.fromisoformat(observed_value.replace("Z", "+00:00"))
                    if observed_at.utcoffset() is None or observed_at.astimezone(timezone.utc) > as_of_utc:
                        raise ValueError(f"LOCATION_STALE:{resource_id}:OBSERVED_AFTER_AS_OF")
                locations[resource_id] = UUID(current_location)
                continue
            home = resource.get("home_location_id") if allow_home else None
            if home:
                locations[resource_id] = UUID(home)
            else:
                raise ValueError(f"LOCATION_STALE:{resource_id}:NO_VERIFIED_LOCATION")
    return locations
