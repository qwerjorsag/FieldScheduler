"""Load editable farm data and derive the fixture's time and productivity inputs."""
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from zoneinfo import ZoneInfo


def configure(source, workers, fields, resources, directory: Path):
    settings = json.loads((directory / "starting_date.json").read_text(encoding="utf-8"))
    policy = json.loads((directory / "planning_policy.json").read_text(encoding="utf-8"))
    zone = ZoneInfo(settings["timezone"])
    day = date.fromisoformat(settings["starting_date"])
    origin = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=settings["horizon_days"]), time.min, zone).astimezone(timezone.utc)
    if settings["horizon_days"] <= 0 or settings["slot_minutes"] not in (15, 30, 60):
        raise ValueError("INVALID_STARTING_DATE_SETTINGS")
    source["timezone"] = settings["timezone"]
    source["horizon"] = {"start_at": origin.isoformat(), "as_of": origin.isoformat(),
                         "horizon_hours": int((end-origin).total_seconds() // 3600), "slot_minutes": settings["slot_minutes"]}
    source["planning_policy"] = policy
    windows = []
    for i in range(settings["horizon_days"]):
        d = day + timedelta(days=i)
        a = datetime.combine(d, time(policy["day_start_hour"]), zone).astimezone(timezone.utc)
        b = datetime.combine(d + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        windows.append([a.isoformat(), b.isoformat()])
    source["allowed_work_windows_utc"] = windows
    field_by_id = {x["id"]: x for x in fields}
    machine_by_id = {x["id"]: x for x in resources["machines"]}
    tools = {x["id"]: x for x in resources["equipment"]}
    compatible = {(x["machine_id"], x["equipment_id"]) for x in resources["compatibilities"] if x["compatible"]}
    for task in source["operations"]:
        field = field_by_id[task["field_id"]]
        phase = task["phase"]
        machine = machine_by_id[task["machine_id"]]
        area = Decimal(str(field["area_ha"]))
        factor = Decimal(str(field["productivity_factor"]))
        if not area.is_finite() or area <= 0 or not factor.is_finite() or not 0 < factor <= 1:
            raise ValueError("INVALID_FIELD_AREA_OR_FACTOR:" + field["id"])
        rates = [Decimal(str(machine["productivity_ha_per_hour"][phase]))]
        for tid in task["equipment_ids"]:
            if (machine["id"], tid) not in compatible:
                raise ValueError("INCOMPATIBLE_MACHINE_EQUIPMENT:" + task["id"])
            rates.append(Decimal(str(tools[tid]["productivity_ha_per_hour"][phase])))
        if any(not r.is_finite() or r <= 0 for r in rates):
            raise ValueError("INVALID_PRODUCTIVITY:" + task["id"])
        effective = min(rates) * factor
        setup = int(machine["setup_minutes"])
        if setup < 0:
            raise ValueError("NEGATIVE_SETUP_TIME")
        minutes = int((area / effective * 60 + setup).to_integral_value(rounding=ROUND_CEILING))
        task.update(area_ha=str(area), duration_minutes=minutes, earliest_start_slot=0,
                    effective_ha_per_hour=str(effective), setup_minutes=setup)
    return source
