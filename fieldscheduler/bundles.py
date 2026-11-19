from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from itertools import product
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5


_BUNDLE_NAMESPACE = UUID("1741fe52-72c8-49e2-a9a5-8c7463b9b71e")


@dataclass(frozen=True)
class BundleCandidate:
    id: UUID
    operation_id: UUID
    area_ha: Decimal
    worker_ids: tuple[UUID, ...]
    machine_ids: tuple[UUID, ...]
    equipment_ids: tuple[UUID, ...]
    duration_minutes: int
    duration_slots: int
    estimated_cost_cents: int
    effective_capacity_ha_per_hour: Decimal
    productivity_valid_from: datetime | None
    productivity_valid_to: datetime | None
    worker_qualification_valid_until: tuple[date, ...]


@dataclass(frozen=True)
class BundleGeneration:
    candidates: tuple[BundleCandidate, ...]
    diagnostics: tuple[str, ...]
    candidate_truncated: bool


def load_seed(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as seed_file:
        seed = json.load(seed_file)
    if seed.get("metadata", {}).get("schema_version") != "field-scheduler-poc-seed/v1":
        raise ValueError("Unsupported PoC seed schema")
    if seed["metadata"].get("production_use_permitted") is not False:
        raise ValueError("PoC seed must not be marked for production use")
    return seed["entities"]


def _instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Input timestamps must include an offset")
    return parsed.astimezone(timezone.utc)


def _rate_for(
    entities: dict[str, Any],
    operation: dict[str, Any],
    machine: dict[str, Any],
    equipment: dict[str, Any] | None,
    horizon_start: datetime,
    horizon_end: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    cultivation_by_id = {item["id"]: item for item in entities.get("cultivations", [])}
    field_by_id = {item["id"]: item for item in entities.get("fields", [])}
    cultivation = cultivation_by_id.get(operation.get("cultivation_id"), {})
    field = field_by_id.get(cultivation.get("field_id"), {})
    soil_type = field.get("soil_type")
    matches: list[tuple[int, dict[str, Any]]] = []
    for rate in entities.get("productivity_rates", []):
        if rate["farm_id"] != operation["farm_id"] or rate["operation_type"] != operation["operation_type"]:
            continue
        if rate.get("machine_type") not in (None, machine.get("machine_type")):
            continue
        if rate.get("equipment_type") not in (None, equipment.get("equipment_type") if equipment else None):
            continue
        if rate.get("soil_type") not in (None, soil_type):
            continue
        valid_from, valid_to = _instant(rate.get("valid_from")), _instant(rate.get("valid_to"))
        if valid_from and valid_from >= horizon_end or valid_to and valid_to <= horizon_start:
            continue
        specificity = sum(rate.get(key) is not None for key in ("machine_type", "equipment_type", "soil_type"))
        matches.append((specificity, rate))
    if not matches:
        return None, "PRODUCTIVITY_UNKNOWN"
    best_specificity = max(specificity for specificity, _ in matches)
    best = [rate for specificity, rate in matches if specificity == best_specificity]
    if len(best) != 1:
        return None, "PRODUCTIVITY_AMBIGUOUS"
    return best[0], None


def generate_bundles(
    entities: dict[str, Any],
    operation_id: str | UUID,
    farm_id: str | UUID,
    horizon_start: datetime,
    horizon_hours: int,
    slot_minutes: int = 60,
    max_bundles: int = 20,
    area_override: Decimal | None = None,
    identity_scope: UUID | None = None,
) -> BundleGeneration:
    operation_key, farm_key = str(operation_id), str(farm_id)
    operation = next((item for item in entities.get("field_operations", []) if item["id"] == operation_key and item["farm_id"] == farm_key), None)
    if operation is None:
        return BundleGeneration((), ("INPUT_INVALID:OPERATION_NOT_FOUND",), False)
    if slot_minutes not in (15, 30, 60) or max_bundles < 1:
        return BundleGeneration((), ("INPUT_INVALID:SOLVER_CONFIG",), False)
    horizon_start = horizon_start.astimezone(timezone.utc)
    horizon_end = horizon_start + timedelta(hours=horizon_hours)
    requirements = [item for item in entities.get("operation_resource_requirements", []) if item["operation_id"] == operation_key and item["farm_id"] == farm_key]
    if not requirements:
        return BundleGeneration((), ("NO_COMPATIBLE_BUNDLE",), False)
    if any(int(item["required_count"]) != 1 for item in requirements):
        return BundleGeneration((), ("NOT_IMPLEMENTED:REQUIRED_COUNT_ABOVE_ONE",), False)

    workers = {item["id"]: item for item in entities.get("workers", []) if item["farm_id"] == farm_key and item.get("active")}
    machines = [item for item in entities.get("machines", []) if item["farm_id"] == farm_key and item.get("active")]
    equipment = [item for item in entities.get("equipment", []) if item["farm_id"] == farm_key and item.get("active")]
    qualifications = {(item["worker_id"], item["qualification_id"]): date.fromisoformat(item["valid_until"]) if item.get("valid_until") else None for item in entities.get("worker_qualifications", []) if item["farm_id"] == farm_key}
    compatibility = {(item["machine_id"], item["equipment_id"]): item["is_compatible"] for item in entities.get("machine_equipment_compatibilities", []) if item["farm_id"] == farm_key}
    machine_requirements = [item for item in requirements if item["role"] in {"TRACTOR", "SPRAYER", "COMBINE", "MACHINE"}]
    equipment_requirements = [item for item in requirements if item["role"] == "EQUIPMENT"]
    operator_requirements = [item for item in requirements if item.get("required_qualification_id") is not None]
    if len(machine_requirements) != 1 or len(operator_requirements) != 1 or len(equipment_requirements) > 1:
        return BundleGeneration((), ("NOT_IMPLEMENTED:UNSUPPORTED_RESOURCE_ROLES",), False)
    machine_requirement, operator_requirement = machine_requirements[0], operator_requirements[0]
    equipment_requirement = equipment_requirements[0] if equipment_requirements else None
    required_equipment_type = equipment_requirement.get("required_equipment_type") if equipment_requirement else None
    worker_options = [
        (worker, valid_until)
        for worker in workers.values()
        if (worker["id"], operator_requirement["required_qualification_id"]) in qualifications
        for valid_until in [qualifications[(worker["id"], operator_requirement["required_qualification_id"])] ]
        if valid_until is None or valid_until >= horizon_start.date()
    ]
    machine_options = [item for item in machines if item.get("machine_type") == machine_requirement.get("required_machine_type") and (machine_requirement.get("min_power_kw") is None or item.get("rated_power_kw") is not None and item["rated_power_kw"] >= machine_requirement["min_power_kw"])]
    equipment_options = [item for item in equipment if item.get("equipment_type") == required_equipment_type] if required_equipment_type else [None]
    diagnostics: set[str] = set()
    candidates: list[BundleCandidate] = []
    template = next((item for item in entities.get("operation_templates", []) if item["id"] == operation.get("operation_template_id")), {})
    defaults = template.get("defaults_json", {})
    area = area_override if area_override is not None else Decimal(operation["area_ha"])
    if area <= 0:
        return BundleGeneration((), (f"INPUT_INVALID:AREA:{operation_key}",), False)
    setup_minutes, cleanup_minutes = int(defaults.get("setup_minutes", 0)), int(defaults.get("cleanup_minutes", 0))
    for worker, qualification_valid_until in worker_options:
        for machine, implement in product(machine_options, equipment_options):
            if implement is not None and compatibility.get((machine["id"], implement["id"])) is not True:
                continue
            rate, rate_issue = _rate_for(entities, operation, machine, implement, horizon_start, horizon_end)
            if rate_issue:
                diagnostics.add(rate_issue)
                continue
            capacities = [Decimal(rate["ha_per_hour"])]
            for resource in (machine, implement):
                if resource and resource.get("capacity_ha_per_hour") is not None:
                    capacities.append(Decimal(resource["capacity_ha_per_hour"]))
            capacity = min(capacities)
            if capacity <= 0:
                diagnostics.add("PRODUCTIVITY_UNKNOWN")
                continue
            productive_minutes = int((Decimal(60) * area / capacity).to_integral_value(rounding=ROUND_CEILING))
            duration_minutes = productive_minutes + setup_minutes + cleanup_minutes
            duration_slots = (duration_minutes + slot_minutes - 1) // slot_minutes
            hourly_cost = int(worker["cost_per_hour_cents"]) + int(machine["cost_per_hour_cents"])
            if implement:
                hourly_cost += int(implement["cost_per_hour_cents"])
            estimated_cost = int((Decimal(hourly_cost) * duration_minutes / Decimal(60)).to_integral_value(rounding=ROUND_CEILING))
            members = (worker["id"], machine["id"], implement["id"] if implement else "-")
            bundle_id = uuid5(_BUNDLE_NAMESPACE, f"{operation_key}:{identity_scope or operation_key}:{':'.join(members)}")
            valid_from, valid_to = _instant(rate.get("valid_from")), _instant(rate.get("valid_to"))
            candidates.append(BundleCandidate(bundle_id, UUID(operation_key), area, (UUID(worker["id"]),), (UUID(machine["id"]),), (UUID(implement["id"]),) if implement else (), duration_minutes, duration_slots, estimated_cost, capacity, valid_from, valid_to, (qualification_valid_until,) if qualification_valid_until else ()))
    candidates.sort(key=lambda candidate: (candidate.estimated_cost_cents, candidate.duration_minutes, str(candidate.id)))
    truncated = len(candidates) > max_bundles
    if truncated:
        diagnostics.add("BUNDLE_PRUNING_RISK")
        candidates = candidates[:max_bundles]
    if not candidates and not diagnostics:
        diagnostics.add("NO_COMPATIBLE_BUNDLE")
    return BundleGeneration(tuple(candidates), tuple(sorted(diagnostics)), truncated)
