from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime

from fieldscheduler.bundles import generate_bundles, load_seed
from fieldscheduler.engine import load_solver_fixture, solve_fixture


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Field Scheduler engine utilities")
    parser.add_argument("fixture", nargs="?", help="Path to a solver fixture JSON file")
    parser.add_argument("--max-seconds", type=float, default=10)
    parser.add_argument("--seed", help="Path to a synthetic PoC seed JSON file; emits candidate bundles")
    parser.add_argument("--farm-id", help="Farm UUID for candidate generation")
    parser.add_argument("--operation-id", help="Optional operation UUID; defaults to every farm operation")
    parser.add_argument("--horizon-start", help="Timezone-aware ISO timestamp for the solver horizon")
    parser.add_argument("--horizon-hours", type=int, default=720)
    parser.add_argument("--slot-minutes", type=int, choices=(15, 30, 60), default=60)
    parser.add_argument("--max-bundles", type=int, default=20)
    arguments = parser.parse_args()
    if arguments.seed:
        if not arguments.farm_id or not arguments.horizon_start:
            parser.error("--seed requires --farm-id and --horizon-start")
        entities = load_seed(arguments.seed)
        operation_ids = [arguments.operation_id] if arguments.operation_id else [
            operation["id"] for operation in entities.get("field_operations", []) if operation["farm_id"] == arguments.farm_id
        ]
        horizon_start = datetime.fromisoformat(arguments.horizon_start.replace("Z", "+00:00"))
        output = {
            operation_id: asdict(generate_bundles(entities, operation_id, arguments.farm_id, horizon_start, arguments.horizon_hours, arguments.slot_minutes, arguments.max_bundles))
            for operation_id in operation_ids
        }
        print(json.dumps(output, default=str, indent=2))
        return
    if not arguments.fixture:
        parser.error("provide a solver fixture or --seed")
    result = solve_fixture(load_solver_fixture(arguments.fixture), arguments.max_seconds)
    print(json.dumps(asdict(result), default=list, indent=2))


if __name__ == "__main__":
    main()
