# Field Scheduler

## Annual farm plan

Run `.\.venv\Scripts\python.exe main.py` to generate a full-year schedule for
20 growing fields and 50 locations. Editable crop, recurrence, BPEJ, worker and
machinery data live in `data/annual/`; start and horizon are in
`data/starting_date.json`. See [annual data instructions](data/annual/README.md).
Each run exports CSV, JSON, SQLite, monthly overviews and weekly worker/location
timelines to `Schedules/annual_...`. This is a resource-constrained heuristic;
agronomic conditions, real routes and production readiness are reported separately.

The fixture utilities below remain available for the smaller technical examples.

Current work focuses on the standalone planning engine. The web application,
API, persistence layer, and full V3 constraint set are not implemented yet.

## Solver fixture

The engine uses the synthetic solver fixture as input; it does not treat seed
data as an already computed schedule. After installing dependencies with `uv
sync`, run:

```powershell
uv run python -m fieldscheduler "Work Instructions/data/fixtures/minimal_solver_input.json"
```

The current solver handles fixture resource and field collisions, hard work
windows, and minimum dependency lags. Its independent validator checks those
same result properties. Results remain unvalidated for publication because
physical travel certification and the rest of the V3 constraints are not yet
implemented.

Generate compatible equipment bundles from the synthetic PoC seed:

```powershell
uv run python -m fieldscheduler --seed "Work Instructions/data/poc_seed.json" --farm-id 5fbeebba-b34c-592a-a488-4e40353f6b5b --horizon-start 2026-11-22T23:00:00Z
```

The bundle generator filters inactive or unqualified workers, machine type
and power, explicit machine/equipment compatibility, and productivity rates.
Candidate truncation is reported as `BUNDLE_PRUNING_RISK`.
