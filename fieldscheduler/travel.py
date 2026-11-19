from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class TravelEdge:
    origin_id: UUID
    destination_id: UUID
    category: str
    duration_minutes: int
    source_id: UUID | None = None


@dataclass(frozen=True)
class TravelRoute:
    origin_id: UUID
    destination_id: UUID
    category: str
    duration_minutes: int
    path: tuple[UUID, ...]


@dataclass(frozen=True)
class TravelClosure:
    routes: tuple[TravelRoute, ...]
    warnings: tuple[str, ...]

    def route(self, origin_id: UUID, destination_id: UUID, category: str = "ANY") -> TravelRoute | None:
        if origin_id == destination_id:
            return TravelRoute(origin_id, destination_id, category, 0, (origin_id,))
        exact = next((route for route in self.routes if route.origin_id == origin_id and route.destination_id == destination_id and route.category == category), None)
        if exact is not None or category == "ANY":
            return exact
        return next((route for route in self.routes if route.origin_id == origin_id and route.destination_id == destination_id and route.category == "ANY"), None)


def build_shortest_path_closure(edges: tuple[TravelEdge, ...], categories: tuple[str, ...] | None = None) -> TravelClosure:
    if any(edge.duration_minutes < 0 for edge in edges):
        raise ValueError("INPUT_INVALID:TRAVEL_NEGATIVE_DURATION")
    nodes = sorted({node for edge in edges for node in (edge.origin_id, edge.destination_id)}, key=str)
    categories = categories or tuple(sorted({edge.category for edge in edges} | {"ANY"}))
    all_routes: list[TravelRoute] = []
    warnings: set[str] = set()
    for category in categories:
        distances: dict[tuple[UUID, UUID], int] = {}
        paths: dict[tuple[UUID, UUID], tuple[UUID, ...]] = {}
        direct: dict[tuple[UUID, UUID], int] = {}
        for node in nodes:
            distances[(node, node)] = 0
            paths[(node, node)] = (node,)
        category_edges: dict[tuple[UUID, UUID], list[TravelEdge]] = {}
        for edge in edges:
            if edge.category in {"ANY", category}:
                category_edges.setdefault((edge.origin_id, edge.destination_id), []).append(edge)
        for pair, matching in category_edges.items():
            exact = [edge for edge in matching if edge.category == category]
            available = exact or matching
            chosen = min(available, key=lambda edge: (edge.duration_minutes, str(edge.source_id or "")))
            distances[pair] = chosen.duration_minutes
            paths[pair] = pair
            direct[pair] = chosen.duration_minutes
        for pivot in nodes:
            for origin in nodes:
                left = distances.get((origin, pivot))
                if left is None:
                    continue
                for destination in nodes:
                    right = distances.get((pivot, destination))
                    if right is None:
                        continue
                    candidate = left + right
                    pair = (origin, destination)
                    if candidate < distances.get(pair, candidate + 1):
                        left_path = paths[(origin, pivot)]
                        right_path = paths[(pivot, destination)]
                        paths[pair] = left_path + right_path[1:]
                        distances[pair] = candidate
        for (origin, destination), duration in distances.items():
            if origin == destination:
                continue
            all_routes.append(TravelRoute(origin, destination, category, duration, paths[(origin, destination)]))
            direct_duration = direct.get((origin, destination))
            if direct_duration is not None and duration < direct_duration:
                warnings.add(f"TRAVEL_TRIANGLE_VIOLATION:{category}:{origin}:{destination}:{direct_duration}:{duration}")
    return TravelClosure(tuple(all_routes), tuple(sorted(warnings)))


def travel_closure_from_seed(
    entities: dict[str, Any],
    farm_id: UUID,
    as_of: datetime,
) -> TravelClosure:
    if as_of.utcoffset() is None:
        raise ValueError("INPUT_INVALID:AS_OF_MUST_BE_AWARE")
    as_of_utc = as_of.astimezone(timezone.utc)
    location_ids = {
        UUID(item["id"])
        for item in entities.get("locations", [])
        if item["farm_id"] == str(farm_id)
    }
    edges: list[TravelEdge] = []
    for row in entities.get("travel_times", []):
        if row["farm_id"] != str(farm_id):
            continue
        starts = datetime.fromisoformat(row["valid_from"].replace("Z", "+00:00")) if row.get("valid_from") else None
        ends = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00")) if row.get("valid_until") else None
        if starts and starts.astimezone(timezone.utc) > as_of_utc or ends and ends.astimezone(timezone.utc) <= as_of_utc:
            continue
        origin_id, destination_id = UUID(row["origin_location_id"]), UUID(row["destination_location_id"])
        if origin_id not in location_ids or destination_id not in location_ids:
            raise ValueError(f"INPUT_INVALID:TRAVEL_LOCATION_REFERENCE:{row['id']}")
        edge = TravelEdge(origin_id, destination_id, row["resource_category"], int(row["duration_minutes"]), UUID(row["id"]))
        edges.append(edge)
        if row.get("symmetric") and origin_id != destination_id:
            edges.append(TravelEdge(destination_id, origin_id, row["resource_category"], edge.duration_minutes, edge.source_id))
    category_values = tuple(sorted({edge.category for edge in edges} | {"ANY"}))
    return build_shortest_path_closure(tuple(edges), category_values)


def field_locations_from_seed(entities: dict[str, Any], farm_id: UUID) -> dict[UUID, UUID]:
    entrances: dict[UUID, list[UUID]] = {}
    for location in entities.get("locations", []):
        if location["farm_id"] != str(farm_id) or location.get("kind") != "FIELD_ENTRANCE":
            continue
        field_id = UUID(location["field_id"])
        entrances.setdefault(field_id, []).append(UUID(location["id"]))
    fields = {UUID(item["field_id"]) for item in entities.get("cultivations", []) if item["farm_id"] == str(farm_id)}
    missing = fields - set(entrances)
    ambiguous = {field_id for field_id, locations in entrances.items() if len(locations) != 1}
    if missing:
        raise ValueError("TRAVEL_DATA_MISSING:FIELD_ENTRANCE:" + ",".join(sorted(map(str, missing))))
    if ambiguous:
        raise ValueError("TRAVEL_DATA_MISSING:AMBIGUOUS_FIELD_ENTRANCE:" + ",".join(sorted(map(str, ambiguous))))
    return {field_id: locations[0] for field_id, locations in entrances.items()}
