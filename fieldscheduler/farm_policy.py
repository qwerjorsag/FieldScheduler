"""Additional, explicit policies for the editable farm fixture runner."""
from collections import defaultdict
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from ortools.sat.python import cp_model


class FarmPolicy:
    def __init__(self, fixture):
        self.raw = fixture
        self.config = fixture.get("planning_policy")
        self.ops = {o["id"]: o for o in fixture["operations"]}
        self.workers = {w["id"]: w for w in fixture["resources"]["workers"]}
        self.resources = {r["id"]: r for k in ("machines", "equipment") for r in fixture["resources"][k]}
        self.origin = datetime.fromisoformat(fixture["horizon"]["start_at"].replace("Z", "+00:00"))
        self.slot = fixture["horizon"]["slot_minutes"]
        self.zone = ZoneInfo(fixture["horizon"]["timezone"])
        if self.config:
            c = self.config
            if c.get("day_end_hour", 24) != 24:
                raise ValueError("POLICY_DAY_END_MUST_BE_MIDNIGHT")
            if not 0 <= c["day_start_hour"] < c["quiet_start_hour"] < 24:
                raise ValueError("INVALID_POLICY_HOURS")
            if not 0 < c["max_daily_minutes"] <= 720 or c["field_switch_penalty_minutes"] < 0:
                raise ValueError("INVALID_POLICY_LIMIT")
            if c["field_switch_buffer_minutes"] < 0:
                raise ValueError("INVALID_SWITCH_BUFFER")
            for w in self.workers.values():
                if not 0 < w["max_daily_minutes"] <= 720 or w["max_weekly_minutes"] <= 0 or not 0 <= w["min_rest_minutes"] <= 1440:
                    raise ValueError("INVALID_WORKER_LIMIT:" + w["id"])
                if w.get("timezone") != fixture["horizon"]["timezone"]:
                    raise ValueError("WORKER_TIMEZONE_MISMATCH:" + w["id"])
            for op in self.ops.values():
                if type(op.get("is_non_disruptive")) is not bool:
                    raise ValueError("NOISE_ATTRIBUTE_REQUIRED:" + op["id"])
                for wid in op["resource_bundle"]["worker_ids"]:
                    w = self.workers[wid]
                    allowed = any(p["field_id"] == op["field_id"] and op["operation_type"] in p["operation_types"]
                                  and set(op["resource_bundle"]["machine_ids"]).issubset(p["machine_ids"])
                                  for p in w.get("work_permissions", []))
                    if not w.get("active") or not allowed:
                        raise ValueError("WORKER_NOT_AUTHORIZED:" + wid + ":" + op["id"])

    def instant(self, slot):
        return (self.origin + timedelta(minutes=slot * self.slot)).astimezone(self.zone)

    def quiet(self, op):
        ids = op["resource_bundle"]["machine_ids"] + op["resource_bundle"]["equipment_ids"]
        return op.get("is_non_disruptive") is True and all(self.resources[i].get("is_non_disruptive") is True for i in ids)

    def allowed(self, op_id, start, duration):
        if not self.config:
            return True
        s, e = self.instant(start), self.instant(start + duration)
        midnight = datetime.combine(s.date() + timedelta(days=1), time.min, self.zone)
        limit = midnight if self.quiet(self.ops[op_id]) else s.replace(hour=self.config["quiet_start_hour"], minute=0, second=0)
        return s.hour >= self.config["day_start_hour"] and e <= limit

    def constrain(self, model, choices, starts_by_op, horizon_slots):
        if not self.config:
            return 0
        days, weeks, daily_edges = defaultdict(list), defaultdict(list), defaultdict(list)
        by_worker = defaultdict(list)
        for oid, (present, start, end, duration) in choices.items():
            groups = defaultdict(list)
            for candidate in starts_by_op[oid]:
                groups[self.instant(candidate).date()].append(candidate)
            flags = []
            for day, starts in groups.items():
                flag = model.new_bool_var(f"day_{oid}_{day}")
                model.add_linear_expression_in_domain(start, cp_model.Domain.from_values(starts)).only_enforce_if(flag)
                flags.append(flag)
                first = model.new_int_var(0, horizon_slots, f"first_{oid}_{day}")
                last = model.new_int_var(0, horizon_slots, f"last_{oid}_{day}")
                model.add(first == start).only_enforce_if(flag)
                model.add(first == horizon_slots).only_enforce_if(flag.Not())
                model.add(last == end).only_enforce_if(flag)
                model.add(last == 0).only_enforce_if(flag.Not())
                for wid in self.ops[oid]["resource_bundle"]["worker_ids"]:
                    days[wid, day].append(flag * duration * self.slot)
                    weeks[wid, day.isocalendar()[:2]].append(flag * duration * self.slot)
                    daily_edges[wid, day].append((flag, first, last))
            model.add(sum(flags) == present)
            for wid in self.ops[oid]["resource_bundle"]["worker_ids"]:
                by_worker[wid].append(oid)
        for (wid, day), terms in days.items():
            model.add(sum(terms) <= min(self.config["max_daily_minutes"], self.workers[wid]["max_daily_minutes"]))
        for (wid, week), terms in weeks.items():
            model.add(sum(terms) <= self.workers[wid]["max_weekly_minutes"])
        boundaries = {}
        for key, edges in daily_edges.items():
            used = model.new_bool_var(f"working_{key}")
            model.add_max_equality(used, [x[0] for x in edges])
            first = model.new_int_var(0, horizon_slots, f"day_first_{key}")
            last = model.new_int_var(0, horizon_slots, f"day_last_{key}")
            model.add_min_equality(first, [x[1] for x in edges])
            model.add_max_equality(last, [x[2] for x in edges])
            boundaries[key] = used, first, last
        for (wid, day), (_, _, last) in boundaries.items():
            next_key = wid, day + timedelta(days=1)
            if next_key in boundaries:
                used, first, _ = boundaries[next_key]
                rest = (self.workers[wid]["min_rest_minutes"] + self.slot - 1) // self.slot
                model.add(first >= last + rest).only_enforce_if([boundaries[wid, day][0], used])

        # A circuit identifies consecutive jobs, so only actual field changes are penalized.
        penalties = []
        for wid, ids in by_worker.items():
            arcs = []
            empty = model.new_bool_var(f"idle_{wid}")
            model.add(sum(choices[o][0] for o in ids) == 0).only_enforce_if(empty)
            model.add(sum(choices[o][0] for o in ids) >= 1).only_enforce_if(empty.Not())
            arcs.append((0, 0, empty))
            for i, oid in enumerate(ids, 1):
                arcs.append((i, i, choices[oid][0].Not()))
                for a, b in ((0, i), (i, 0)):
                    arc = model.new_bool_var(f"route_{wid}_{a}_{b}")
                    arcs.append((a, b, arc))
                for j, other in enumerate(ids, 1):
                    if i == j:
                        continue
                    arc = model.new_bool_var(f"route_{wid}_{i}_{j}")
                    arcs.append((i, j, arc))
                    changes_field = self.ops[oid]["field_id"] != self.ops[other]["field_id"]
                    buffer = self.config["field_switch_buffer_minutes"] if changes_field else 0
                    model.add(choices[other][1] >= choices[oid][2] + (buffer + self.slot - 1) // self.slot).only_enforce_if(arc)
                    if changes_field:
                        penalties.append(arc * self.config["field_switch_penalty_minutes"])
            model.add_circuit(arcs)
        return sum(penalties)

    def validate(self, assignments):
        if not self.config:
            return []
        issues, by_worker = [], defaultdict(list)
        daily, weekly = defaultdict(int), defaultdict(int)
        for a in assignments:
            if not self.allowed(a.operation_id, a.start_slot, a.end_slot - a.start_slot):
                issues.append("FARM_WINDOW_VIOLATION:" + a.operation_id)
            if tuple(self.ops[a.operation_id]["resource_bundle"]["worker_ids"]) != a.worker_ids:
                issues.append("WORKER_BUNDLE_MISMATCH:" + a.operation_id)
            day = self.instant(a.start_slot).date()
            for wid in a.worker_ids:
                daily[wid, day] += (a.end_slot - a.start_slot) * self.slot
                weekly[wid, day.isocalendar()[:2]] += (a.end_slot - a.start_slot) * self.slot
                by_worker[wid].append(a)
        for (wid, day), minutes in daily.items():
            if minutes > min(self.config["max_daily_minutes"], self.workers[wid]["max_daily_minutes"]):
                issues.append(f"DAILY_LIMIT:{wid}:{day}")
        for (wid, week), minutes in weekly.items():
            if minutes > self.workers[wid]["max_weekly_minutes"]:
                issues.append(f"WEEKLY_LIMIT:{wid}:{week}")
        for wid, items in by_worker.items():
            for a, b in zip(sorted(items, key=lambda a: a.start_slot), sorted(items, key=lambda a: a.start_slot)[1:]):
                gap = (b.start_slot - a.end_slot) * self.slot
                # Ends exactly at midnight belong to the preceding work day.
                if self.instant(a.start_slot).date() != self.instant(b.start_slot).date() and gap < self.workers[wid]["min_rest_minutes"]:
                    issues.append("REST_LIMIT:" + wid)
                if a.field_id != b.field_id and gap < self.config["field_switch_buffer_minutes"]:
                    issues.append("FIELD_SWITCH_BUFFER:" + wid)
        return issues
