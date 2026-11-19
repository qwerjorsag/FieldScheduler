from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fieldscheduler.engine import solve_fixture  # noqa: E402
from farm_inputs import configure
from timeline import render_timeline


DATA = ROOT / "data"
SCHEDULES = ROOT / "Schedules"


def read_json(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def build_fixture() -> dict:
    workers_data = read_json("workers.json")["workers"]
    fields_data = read_json("fields.json")["fields"]
    resource_data = read_json("resources.json")
    locations_data = read_json("locations.json")["locations"]
    source = configure(read_json("sample_schedule.json"), workers_data, fields_data, resource_data, DATA)
    workers = {item["id"]: item for item in workers_data}
    fields = {item["id"]: item for item in fields_data}
    machines = {item["id"]: item for item in resource_data["machines"]}
    equipment = {item["id"]: item for item in resource_data["equipment"]}
    locations_by_field = {
        item["field_id"]: item
        for item in locations_data
        if item.get("field_id")
    }

    horizon = source["horizon"]
    horizon_start = datetime.fromisoformat(horizon["start_at"].replace("Z", "+00:00"))
    local_zone = ZoneInfo(source["timezone"])
    fixture_horizon = {**horizon, "timezone": source["timezone"]}
    allowed_windows = source["allowed_work_windows_utc"]

    operations = []
    for task in source["operations"]:
        worker = workers[task["worker_id"]]
        machine = machines[task["machine_id"]]
        operation = {
            "id": task["id"],
            "name": task["name"],
            "operation_type": task.get("phase", ""),
            "is_non_disruptive": task["is_non_disruptive"],
            "effective_ha_per_hour": task["effective_ha_per_hour"],
            "setup_minutes": task["setup_minutes"],
            "field_id": task["field_id"],
            "field_name": fields[task["field_id"]]["name"],
            "area_ha": task["area_ha"],
            "mandatory": task["mandatory"],
            "duration_minutes": task["duration_minutes"],
            "earliest_start_slot": task["earliest_start_slot"],
            "resource_bundle": {
                "worker_ids": [task["worker_id"]],
                "machine_ids": [task["machine_id"]],
                "equipment_ids": task["equipment_ids"],
            },
            "worker_name": worker["name"],
            "machine_name": machine["name"],
            "equipment_names": [equipment[item]["name"] for item in task["equipment_ids"]],
        }
        operations.append(operation)

    return {
        "schema_version": "field-scheduler-solver-fixture/v1",
        "synthetic": True,
        "reference_spec": "expanded sample run",
        "horizon": fixture_horizon,
        "planning_policy": source["planning_policy"],
        "agronomic_status": source["agronomic_status"],
        "resources": {
            "workers": [{**item, "qualification_ids": item["qualifications"]} for item in workers_data],
            "machines": [{**item, "machine_type": item["type"]} for item in resource_data["machines"]],
            "equipment": [{**item, "equipment_type": item["type"]} for item in resource_data["equipment"]],
        },
        "locations": [
            {"id": locations_by_field[item["id"]]["id"], "field_id": item["id"], "field_name": item["name"]}
            for item in fields_data
        ],
        "operations": operations,
        "dependencies": source["dependencies"],
        "allowed_work_windows_utc": allowed_windows,
        "test_assertions": [
            "No worker, machine or equipment overlap",
            "No field overlap",
            "All mandatory operations scheduled inside work windows",
        ],
        "adapter_note": "Synthetic sample. Travel legs are not represented by this fixture solver.",
    }


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def named_assignments(fixture: dict, result: object) -> list[dict]:
    operation_by_id = {item["id"]: item for item in fixture["operations"]}
    horizon_start = datetime.fromisoformat(fixture["horizon"]["start_at"].replace("Z", "+00:00"))
    zone = ZoneInfo(fixture["horizon"]["timezone"])
    output = []
    for assignment in result.assignments:
        source = operation_by_id[assignment.operation_id]
        start_utc = horizon_start + timedelta(minutes=assignment.start_slot * fixture["horizon"]["slot_minutes"])
        end_utc = horizon_start + timedelta(minutes=assignment.end_slot * fixture["horizon"]["slot_minutes"])
        output.append({
            "operation_id": assignment.operation_id,
            "operation_name": source["name"],
            "operation_type": source.get("operation_type", ""),
            "field_id": assignment.field_id,
            "field_name": source["field_name"],
            "area_ha": source["area_ha"],
            "start_slot": assignment.start_slot,
            "end_slot": assignment.end_slot,
            "start_at": start_utc.astimezone(zone).isoformat(),
            "end_at": end_utc.astimezone(zone).isoformat(),
            "worker_ids": list(assignment.worker_ids),
            "worker_names": [item["name"] for item in fixture["resources"]["workers"] if item["id"] in assignment.worker_ids],
            "machine_ids": list(assignment.machine_ids),
            "machine_names": [item["name"] for item in fixture["resources"]["machines"] if item["id"] in assignment.machine_ids],
            "equipment_ids": list(assignment.equipment_ids),
            "equipment_names": [item["name"] for item in fixture["resources"]["equipment"] if item["id"] in assignment.equipment_ids],
        })
    return sorted(output, key=lambda item: (item["start_at"], item["field_name"], item["operation_name"]))


def write_schedule_csv(path: Path, rows: list[dict]) -> None:
    columns = [
        "start_at", "end_at", "start_slot", "end_slot", "worker_names", "operation_name", "operation_type",
        "field_id", "field_name", "area_ha", "machine_ids", "machine_names",
        "equipment_ids", "equipment_names", "worker_ids", "operation_id",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **row,
                "worker_names": "; ".join(row["worker_names"]),
                "machine_names": "; ".join(row["machine_names"]),
                "equipment_names": "; ".join(row["equipment_names"]),
                "machine_ids": "; ".join(row["machine_ids"]),
                "equipment_ids": "; ".join(row["equipment_ids"]),
                "worker_ids": "; ".join(row["worker_ids"]),
            })


def schedule_svg(fixture: dict, rows: list[dict], week_index: int) -> str:
    return render_timeline(fixture, rows, week_index)


def field_outputs(fixture: dict, rows: list[dict], run_dir: Path) -> list[str]:
    """Write a field-first work plan so users can see the full cycle per field."""
    stages = ["PLOUGH", "SOW", "SPRAY", "HARVEST"]
    stage_labels = {
        "PLOUGH": "Orba",
        "SOW": "Seti",
        "SPRAY": "Post\u0159ik",
        "HARVEST": "Sklize\u0148",
    }
    by_field = {location["field_id"]: [] for location in fixture["locations"]}
    for row in rows:
        by_field.setdefault(row["field_id"], []).append(row)
    field_names = {location["field_id"]: location["field_name"] for location in fixture["locations"]}
    field_plan = []
    for field_id, field_name in field_names.items():
        tasks = by_field.get(field_id, [])
        ordered = sorted(tasks, key=lambda x: (stages.index(x["operation_type"]) if x["operation_type"] in stages else 99, x["start_at"]))
        task_by_stage = {task["operation_type"]: task for task in ordered}
        plan_tasks = []
        for stage in stages:
            task = task_by_stage.get(stage)
            plan_tasks.append({
                "stage": stage,
                "stage_name": stage_labels[stage],
                "status": "naplanovano" if task else "nenaplanovano",
                "operation": task,
            })
        field_plan.append({"field_id": field_id, "field_name": field_name, "operations": plan_tasks})

    write_json(run_dir / "field_schedule.json", {
        "view": "by_field",
        "cycle_order": stages,
        "fields": field_plan,
    })
    csv_columns = ["field_name", "stage", "status", "start_at", "end_at", "operation_name", "worker_names", "machine_names", "equipment_names"]
    with (run_dir / "field_schedule.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=csv_columns)
        writer.writeheader()
        for field in field_plan:
            for task in field["operations"]:
                row = task["operation"] or {}
                writer.writerow({
                    "field_name": field["field_name"],
                    "stage": task["stage_name"],
                    "status": task["status"],
                    "start_at": row.get("start_at", ""),
                    "end_at": row.get("end_at", ""),
                    "operation_name": row.get("operation_name", ""),
                    "worker_names": "; ".join(row.get("worker_names", [])),
                    "machine_names": "; ".join(row.get("machine_names", [])),
                    "equipment_names": "; ".join(row.get("equipment_names", [])),
                })

    cols, card_w, row_h = 4, 320, 132
    left, top = 24, 112
    gap_x, gap_y = 12, 12
    width = left * 2 + cols * card_w + (cols - 1) * gap_x
    height = top + len(field_plan) * (row_h + gap_y) + 24
    colors = {"PLOUGH": "#a56a35", "SOW": "#458b51", "SPRAY": "#3679a8", "HARVEST": "#bd8a27"}
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f4f7fb"/>',
        f'<text x="24" y="42" font-family="Segoe UI,Arial,sans-serif" font-size="27" font-weight="700" fill="#152238">Práce podle polí</text>',
        f'<text x="24" y="72" font-family="Segoe UI,Arial,sans-serif" font-size="14" fill="#536176">Každý řádek ukazuje pořadí prací, termín a přiřazené lidi i techniku. Solver: {escape(str(fixture.get("solver_status", "plán")))}</text>',
    ]
    for i, stage in enumerate(stages):
        x = left + i * (card_w + gap_x)
        svg.append(f'<text x="{x+8}" y="100" font-family="Segoe UI,Arial,sans-serif" font-size="13" font-weight="700" fill="{colors[stage]}">{stage_labels[stage]}</text>')
    for field_index, field in enumerate(field_plan):
        y = top + field_index * (row_h + gap_y)
        svg.append(f'<rect x="{left}" y="{y}" width="{width-left*2}" height="{row_h}" rx="10" fill="#ffffff" stroke="#d9e1ec"/>')
        svg.append(f'<text x="{left+10}" y="{y+23}" font-family="Segoe UI,Arial,sans-serif" font-size="13" font-weight="700" fill="#19283d">{escape(field["field_name"])}</text>')
        for stage_index, task in enumerate(field["operations"]):
            x = left + stage_index * (card_w + gap_x) + 8
            op = task["operation"]
            if op:
                start = datetime.fromisoformat(op["start_at"]).astimezone(ZoneInfo(fixture["horizon"]["timezone"]))
                end = datetime.fromisoformat(op["end_at"]).astimezone(ZoneInfo(fixture["horizon"]["timezone"]))
                details = [
                    f'{start:%d.%m.} {start:%H:%M}–{end:%H:%M}',
                    ", ".join(op["worker_names"]),
                    ", ".join(op["machine_names"]),
                    ", ".join(op["equipment_names"]) or "Bez dalšího nástroje",
                ]
                status_color = colors[task["stage"]]
                title = op["operation_name"]
            else:
                details = ["Není naplánováno", "", "", ""]
                status_color = "#9aa6b5"
                title = task["stage_name"]
            svg.append(f'<text x="{x}" y="{y+43}" font-family="Segoe UI,Arial,sans-serif" font-size="12" font-weight="700" fill="{status_color}">{escape(title)}</text>')
            for line_index, detail in enumerate(details):
                svg.append(f'<text x="{x}" y="{y+62+line_index*16}" font-family="Segoe UI,Arial,sans-serif" font-size="11" fill="#536176">{escape(detail[:43])}</text>')
    svg.append('</svg>')
    (run_dir / "field_schedule.svg").write_text("".join(svg), encoding="utf-8")
    return ["field_schedule.json", "field_schedule.csv", "field_schedule.svg"]


def main() -> None:
    fixture = build_fixture()
    # JSON files are authoritative; this CSV is a refreshed, human-readable input export.
    input_columns = ["operation_id", "phase", "operation_name", "field_name", "area_ha", "duration_minutes", "effective_ha_per_hour", "setup_minutes", "is_non_disruptive", "worker_name", "machine_name", "equipment_names"]
    with (DATA / "sample_schedule.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=input_columns)
        writer.writeheader()
        for op in fixture["operations"]:
            writer.writerow({"operation_id": op["id"], "phase": op["operation_type"], "operation_name": op["name"],
                             **{k: op[k] for k in input_columns[3:] if k != "equipment_names"},
                             "equipment_names": "; ".join(op["equipment_names"])})
    result = solve_fixture(fixture, max_seconds=15)
    rows = named_assignments(fixture, result)
    SCHEDULES.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("run_%Y%m%d_%H%M%S")
    run_dir = SCHEDULES / stamp
    suffix = 1
    while run_dir.exists():
        run_dir = SCHEDULES / f"{stamp}_{suffix}"
        suffix += 1
    run_dir.mkdir()

    input_names = ["starting_date.json", "planning_policy.json", "workers.json", "fields.json", "resources.json", "sample_schedule.json", "operation_rules.json", "locations.json", "travel_times.json"]
    write_json(run_dir / "input_snapshot.json", {name: read_json(name) for name in input_names})
    workloads = defaultdict(lambda: {"minutes": 0, "fields": set(), "tasks": 0})
    for row in rows:
        for name in row["worker_names"]:
            key = name, row["start_at"][:10]
            workloads[key]["minutes"] += (row["end_slot"] - row["start_slot"]) * fixture["horizon"]["slot_minutes"]
            workloads[key]["fields"].add(row["field_name"])
            workloads[key]["tasks"] += 1
    with (run_dir / "worker_daily_load.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["worker_name", "date", "reserved_work_minutes", "task_count", "fields"])
        for (name, day), load in sorted(workloads.items()):
            writer.writerow([name, day, load["minutes"], load["tasks"], "; ".join(sorted(load["fields"]))])

    write_json(run_dir / "solver_fixture.json", fixture)
    write_json(run_dir / "solver_result.json", asdict(result))
    write_json(run_dir / "named_schedule.json", {
        "sample_only": True,
        "solver_status": result.solver_status,
        "validated": result.validated,
        "diagnostics": list(result.diagnostics),
        "worker_count": len(fixture["resources"]["workers"]),
        "field_count": len(fixture["locations"]),
        "equipment_count": len(fixture["resources"]["equipment"]),
        "assignments": rows,
    })
    write_schedule_csv(run_dir / "schedule.csv", rows)
    week_count = (read_json("starting_date.json")["horizon_days"] + 6) // 7
    week_files = []
    for week_index in range(week_count):
        svg = schedule_svg(fixture, rows, week_index)
        filename = f"schedule_week_{week_index + 1:02d}.svg"
        (run_dir / filename).write_text(svg, encoding="utf-8")
        week_files.append(filename)
        field_filename = f"field_schedule_week_{week_index + 1:02d}.svg"
        field_svg = render_timeline(fixture, rows, week_index, by_field=True)
        (run_dir / field_filename).write_text(field_svg, encoding="utf-8")
        week_files.append(field_filename)
        if week_index == 0:
            (DATA / "sample_field_timeline.svg").write_text(field_svg, encoding="utf-8")
        if week_index == 0:
            (DATA / "sample_schedule.svg").write_text(svg, encoding="utf-8")
    field_files = field_outputs(fixture, rows, run_dir)
    (DATA / "sample_field_schedule.svg").write_text(
        (run_dir / "field_schedule.svg").read_text(encoding="utf-8"), encoding="utf-8"
    )
    summary = {
        "run_id": run_dir.name,
        "solver_status": result.solver_status,
        "validated": result.validated,
        "assignment_count": len(rows),
        "worker_count": len(fixture["resources"]["workers"]),
        "field_count": len(fixture["locations"]),
        "machine_count": len(fixture["resources"]["machines"]),
        "equipment_count": len(fixture["resources"]["equipment"]),
        "diagnostics": list(result.diagnostics),
        "max_reserved_worker_minutes_per_day": max((load["minutes"] for load in workloads.values()), default=0),
        "agronomic_status": fixture["agronomic_status"],
        "files": ["input_snapshot.json", "worker_daily_load.csv", "solver_fixture.json", "solver_result.json", "named_schedule.json", "schedule.csv", *week_files, *field_files],
    }
    write_json(run_dir / "run_summary.json", summary)
    readme = (
        "# V\u00fdstup b\u011bhu\n\n"
        f"- Solver: `{result.solver_status}`; validov\u00e1no: `{str(result.validated).lower()}`.\n"
        f"- P\u0159i\u0159azen\u00ed: {len(rows)}; pracovn\u00edci: {len(fixture['resources']['workers'])}; "
        f"pole: {len(fixture['locations'])}; n\u00e1stroje: {len(fixture['resources']['equipment'])}.\n"
        f"- T\u00fddenn\u00ed obr\u00e1zky: {', '.join(week_files)}.\n"
        f"- P\u0159ehled podle pol\u00ed: {', '.join(field_files)}.\n\n"
        "Jde o vzorov\u00fd v\u00fdstup. Cestovn\u00ed \u010dasy nejsou zahrnut\u00e9 do v\u00fdpo\u010dtu, proto nejde o ov\u011b\u0159en\u00fd "
        "provozn\u00ed pl\u00e1n. Denn\u00ed i t\u00fddenn\u00ed limit, odpo\u010dinek a no\u010dn\u00ed hluk jsou kontrolov\u00e1ny. "
        "Diagnostiku obsahuje `solver_result.json`.\n"
    )
    (run_dir / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
