from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol, Sequence
from uuid import UUID

from fieldscheduler.zones import PlanningZone, weather_policy_for_zone


@dataclass(frozen=True)
class WeatherObservation:
    id: UUID
    field_id: UUID
    starts_at: datetime
    ends_at: datetime
    issued_at: datetime
    source: str
    confidence: Decimal
    temperature_c: Decimal | None
    wind_mps: Decimal | None
    precip_mm: Decimal | None
    soil_moisture: Decimal | None
    valid_until: datetime | None = None
    measurement_semantics: str | None = None


@dataclass(frozen=True)
class WeatherProfile:
    max_wind_mps: Decimal | None = None
    min_temp_c: Decimal | None = None
    max_temp_c: Decimal | None = None
    max_precip_mm: Decimal | None = None
    max_soil_moisture: Decimal | None = None
    min_dry_hours_before: int = 0
    min_dry_hours_after: int = 0
    min_continuous_window_slots: int = 1


@dataclass(frozen=True)
class WeatherAssessment:
    allowed: bool
    status: str
    provenance: tuple[UUID, ...]
    diagnostics: tuple[str, ...]


class WeatherAvailabilityProvider(Protocol):
    def observations(self, field_id: UUID) -> Sequence[WeatherObservation]: ...


@dataclass(frozen=True)
class ManualWeatherProvider:
    data: tuple[WeatherObservation, ...]

    def observations(self, field_id: UUID) -> tuple[WeatherObservation, ...]:
        return tuple(item for item in self.data if item.field_id == field_id and item.source.upper() in {"MANUAL", "MANUAL_CONFIRMED"})


@dataclass(frozen=True)
class DemoWeatherProvider:
    data: tuple[WeatherObservation, ...]

    def observations(self, field_id: UUID) -> tuple[WeatherObservation, ...]:
        return tuple(item for item in self.data if item.field_id == field_id and item.source.upper() in {"DEMO", "SYNTHETIC"})


def _priority(observation: WeatherObservation) -> int:
    source = observation.source.upper()
    if source in {"MANUAL", "MANUAL_CONFIRMED"}:
        return 3
    if source in {"PROVIDER", "FORECAST", "OBSERVED"}:
        return 2
    if source in {"DEMO", "SYNTHETIC"}:
        return 1
    return 0


def _values(observation: WeatherObservation) -> tuple[Decimal | None, ...]:
    return (observation.temperature_c, observation.wind_mps, observation.precip_mm, observation.soil_moisture)


def assess_weather(
    field_id: UUID,
    starts_at: datetime,
    ends_at: datetime,
    profile: WeatherProfile,
    observations: Sequence[WeatherObservation],
    policy: str,
    zone: PlanningZone,
    trusted_until: datetime,
    slot_minutes: int,
    as_of: datetime | None = None,
) -> WeatherAssessment:
    """Assess the full operation plus required dry-before/after intervals."""
    if any(value.utcoffset() is None for value in (starts_at, ends_at, trusted_until)) or slot_minutes not in (15, 30, 60):
        return WeatherAssessment(False, "INVALID", (), ("INPUT_INVALID:WEATHER_TIMEBASE",))
    if starts_at >= ends_at or profile.min_dry_hours_before < 0 or profile.min_dry_hours_after < 0 or profile.min_continuous_window_slots < 1:
        return WeatherAssessment(False, "INVALID", (), ("INPUT_INVALID:WEATHER_PROFILE",))
    effective_policy, initial_status = weather_policy_for_zone(policy, zone)
    if effective_policy == "IGNORE":
        return WeatherAssessment(True, "IGNORED", (), ())

    start = starts_at.astimezone(timezone.utc) - timedelta(hours=profile.min_dry_hours_before)
    end = ends_at.astimezone(timezone.utc) + timedelta(hours=profile.min_dry_hours_after)
    if (end - start).total_seconds() < profile.min_continuous_window_slots * slot_minutes * 60:
        issue = "WEATHER_WINDOW_TOO_SHORT"
        allowed = effective_policy != "STRICT"
        return WeatherAssessment(allowed, "RISK" if allowed else "REJECTED", (), (issue,))
    trusted_end = trusted_until.astimezone(timezone.utc)
    relevant = [
        item for item in observations
        if item.field_id == field_id
        and item.starts_at.utcoffset() is not None
        and item.ends_at.utcoffset() is not None
        and item.issued_at.utcoffset() is not None
        and item.starts_at.astimezone(timezone.utc) < end
        and item.ends_at.astimezone(timezone.utc) > start
        and item.issued_at.astimezone(timezone.utc) <= trusted_end
        and (as_of is None or as_of.utcoffset() is not None and item.issued_at.astimezone(timezone.utc) <= as_of.astimezone(timezone.utc))
        and (item.valid_until is None or item.valid_until.utcoffset() is not None and item.valid_until.astimezone(timezone.utc) >= start)
    ]
    boundaries = {start, end, trusted_end}
    for item in relevant:
        boundaries.add(item.starts_at.astimezone(timezone.utc))
        boundaries.add(item.ends_at.astimezone(timezone.utc))
    ordered = sorted(point for point in boundaries if start <= point <= end)
    selected: list[WeatherObservation] = []
    diagnostics: set[str] = set()
    unknown = False
    adverse = False
    for left, right in zip(ordered, ordered[1:]):
        if left >= right:
            continue
        if right > trusted_end:
            unknown = True
            diagnostics.add("WEATHER_DATA_MISSING:UNTRUSTED_HORIZON")
            continue
        covering = [
            item for item in relevant
            if item.starts_at.astimezone(timezone.utc) <= left
            and item.ends_at.astimezone(timezone.utc) >= right
            and (item.valid_until is None or item.valid_until.utcoffset() is not None and item.valid_until.astimezone(timezone.utc) >= right)
        ]
        if not covering:
            unknown = True
            diagnostics.add("WEATHER_DATA_MISSING:COVERAGE_GAP")
            continue
        best_priority = max(_priority(item) for item in covering)
        covering = [item for item in covering if _priority(item) == best_priority]
        newest = max(item.issued_at.astimezone(timezone.utc) for item in covering)
        covering = [item for item in covering if item.issued_at.astimezone(timezone.utc) == newest]
        if len({_values(item) for item in covering}) > 1:
            unknown = True
            diagnostics.add("WEATHER_DATA_CONFLICT")
            continue
        chosen = min(covering, key=lambda item: str(item.id))
        selected.append(chosen)
        checks = (
            (profile.max_wind_mps, chosen.wind_mps, lambda actual, limit: actual <= limit, "WIND"),
            (profile.min_temp_c, chosen.temperature_c, lambda actual, limit: actual >= limit, "TEMPERATURE"),
            (profile.max_temp_c, chosen.temperature_c, lambda actual, limit: actual <= limit, "TEMPERATURE"),
            (profile.max_precip_mm, chosen.precip_mm, lambda actual, limit: actual <= limit, "PRECIPITATION"),
            (profile.max_soil_moisture, chosen.soil_moisture, lambda actual, limit: actual <= limit, "SOIL_MOISTURE"),
        )
        for limit, actual, predicate, label in checks:
            if limit is not None and actual is None:
                unknown = True
                diagnostics.add(f"WEATHER_DATA_MISSING:{label}")
            elif limit is not None and not predicate(actual, limit):
                adverse = True
                diagnostics.add(f"WEATHER_CONDITION_UNSAFE:{label}")
        if chosen.precip_mm is not None and profile.max_precip_mm is not None and chosen.measurement_semantics is None:
            unknown = True
            diagnostics.add("WEATHER_DATA_MISSING:PRECIPITATION_SEMANTICS")

    if effective_policy == "STRICT" and (unknown or adverse):
        return WeatherAssessment(False, "REJECTED", tuple(dict.fromkeys(item.id for item in selected)), tuple(sorted(diagnostics)))
    if unknown:
        return WeatherAssessment(True, "UNVERIFIED", tuple(dict.fromkeys(item.id for item in selected)), tuple(sorted(diagnostics or {"WEATHER_DATA_MISSING"})))
    if adverse:
        return WeatherAssessment(True, "RISK", tuple(dict.fromkeys(item.id for item in selected)), tuple(sorted(diagnostics)))
    status = "UNVERIFIED" if initial_status == "UNVERIFIED" else "VERIFIED"
    return WeatherAssessment(True, status, tuple(dict.fromkeys(item.id for item in selected)), ())


def weather_assessor_from_seed(
    entities: dict[str, object],
    farm_id: UUID,
    horizon_start_at: datetime,
    slot_minutes: int,
    zones: object,
    weather_policy: str,
    weather_data_horizon_hours: int | None,
    as_of: datetime,
):
    """Create an operation-aware assessor from PoC profiles and observations."""
    from fieldscheduler.calendar import slot_ceil, slot_floor, to_utc
    from fieldscheduler.zones import PlanningZone, ZonePolicy, weather_policy_for_zone

    if not isinstance(zones, ZonePolicy):
        raise ValueError("INPUT_INVALID:WEATHER_ZONE_POLICY")
    if slot_minutes not in (15, 30, 60) or horizon_start_at.utcoffset() is None or as_of.utcoffset() is None:
        raise ValueError("INPUT_INVALID:WEATHER_TIMEBASE")
    if not isinstance(weather_policy, str) or weather_policy.upper() not in {"IGNORE", "ADVISORY", "STRICT"}:
        raise ValueError("INPUT_INVALID:WEATHER_POLICY")
    if weather_data_horizon_hours is not None and (
        isinstance(weather_data_horizon_hours, bool)
        or not isinstance(weather_data_horizon_hours, int)
        or weather_data_horizon_hours < 0
    ):
        raise ValueError("INPUT_INVALID:WEATHER_DATA_HORIZON")
    farm_key = str(farm_id)
    operations = {row["id"]: row for row in entities.get("field_operations", []) if row.get("farm_id") == farm_key}
    cultivations = {row["id"]: row for row in entities.get("cultivations", []) if row.get("farm_id") == farm_key}
    profiles: dict[UUID, WeatherProfile] = {}
    profile_ids: dict[str, UUID] = {}
    for row in entities.get("weather_profiles", []):
        if row.get("farm_id") != farm_key:
            continue
        try:
            profile_id = UUID(row["id"])
            profiles[profile_id] = WeatherProfile(
                Decimal(str(row["max_wind_mps"])) if row.get("max_wind_mps") is not None else None,
                Decimal(str(row["min_temp_c"])) if row.get("min_temp_c") is not None else None,
                Decimal(str(row["max_temp_c"])) if row.get("max_temp_c") is not None else None,
                Decimal(str(row["max_precip_mm"])) if row.get("max_precip_mm") is not None else None,
                Decimal(str(row["max_soil_moisture"])) if row.get("max_soil_moisture") is not None else None,
                int(row.get("min_dry_hours_before", 0)),
                int(row.get("min_dry_hours_after", 0)),
                int(row.get("min_continuous_window_slots", 1)),
            )
            profile_ids[row["id"]] = profile_id
        except (ValueError, TypeError, ArithmeticError) as error:
            raise ValueError(f"INPUT_INVALID:WEATHER_PROFILE:{row.get('id')}") from error

    observations: list[WeatherObservation] = []
    for row in entities.get("weather_observations", []):
        if row.get("farm_id") != farm_key:
            continue
        try:
            def parse_time(key: str, optional: bool = False) -> datetime | None:
                value = row.get(key)
                if optional and value is None:
                    return None
                if not isinstance(value, str):
                    raise ValueError
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.utcoffset() is None:
                    raise ValueError
                return parsed

            def parse_decimal(key: str) -> Decimal | None:
                value = row.get(key)
                if value is None:
                    return None
                parsed = Decimal(str(value))
                if not parsed.is_finite():
                    raise ValueError
                return parsed

            observations.append(WeatherObservation(
                UUID(row["id"]), UUID(row["field_id"]), parse_time("starts_at"), parse_time("ends_at"),
                parse_time("issued_at"), str(row["source"]), parse_decimal("confidence") or Decimal("0"),
                parse_decimal("temperature_c"), parse_decimal("wind_mps"), parse_decimal("precip_mm"),
                parse_decimal("soil_moisture"), parse_time("valid_until", True), row.get("valid_measurement_semantics"),
            ))
        except (KeyError, ValueError, TypeError, ArithmeticError) as error:
            raise ValueError(f"INPUT_INVALID:WEATHER_OBSERVATION:{row.get('id')}") from error

    horizon_start_utc = to_utc(horizon_start_at)
    trusted_until = horizon_start_utc + timedelta(hours=weather_data_horizon_hours or 0)
    as_of_utc = to_utc(as_of)

    def assess(operation_id: UUID, starts_at: datetime, ends_at: datetime) -> WeatherAssessment:
        start_slot = slot_floor(to_utc(starts_at), horizon_start_utc, slot_minutes)
        end_slot = slot_ceil(to_utc(ends_at), horizon_start_utc, slot_minutes)
        try:
            zone = zones.for_interval(max(0, start_slot), min(zones.horizon_slots, max(start_slot + 1, end_slot)))
        except ValueError:
            return WeatherAssessment(False, "INVALID", (), ("INPUT_INVALID:WEATHER_ZONE_INTERVAL",))
        operation = operations.get(str(operation_id))
        if operation is None:
            return WeatherAssessment(False, "INVALID", (), (f"INPUT_INVALID:WEATHER_OPERATION:{operation_id}",))
        profile_ref = operation.get("weather_profile_id")
        if not profile_ref:
            return WeatherAssessment(True, "NOT_REQUIRED", (), ())
        try:
            profile_id = profile_ids[profile_ref]
            cultivation = cultivations[operation["cultivation_id"]]
            field_id = UUID(cultivation["field_id"])
        except (KeyError, ValueError):
            effective_policy, _ = weather_policy_for_zone(weather_policy, zone)
            allowed = effective_policy != "STRICT"
            return WeatherAssessment(allowed, "UNVERIFIED" if allowed else "REJECTED", (), ("WEATHER_DATA_MISSING:PROFILE_OR_FIELD",))
        return assess_weather(
            field_id, starts_at, ends_at, profiles[profile_id], observations, weather_policy,
            zone, trusted_until, slot_minutes, as_of_utc,
        )

    return assess
