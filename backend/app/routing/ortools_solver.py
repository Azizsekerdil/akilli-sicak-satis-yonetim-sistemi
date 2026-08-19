"""
Optional OR-Tools backend for the same VRP contract.

OR-Tools is *never* a hard dependency: it is imported lazily inside a function
so that a deployment without it still starts, and :func:`is_available` is the
only thing callers need to check.  When present it typically finds shorter
plans than the built-in savings heuristic on larger territories, so it is
preferred when installed.
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logging_config import get_logger
from app.routing.distance import pair_km
from app.routing.solver import (
    PRIORITY_UNASSIGNED_PENALTY_KM,
    UNASSIGNED_PENALTY_KM,
    VrpProblem,
    VrpRoute,
    VrpSolution,
)

log = get_logger("app.routing.ortools")

SOLVER_NAME = "ORTOOLS"

#: True once the OR-Tools import has been attempted and succeeded.
AVAILABLE: bool = False
_CHECKED: bool = False

#: Integer scaling — OR-Tools works on integers, so distances become metres.
_SCALE = 1000
#: Demands are scaled by this so fractional litres/kilograms survive rounding.
_DEMAND_SCALE = 1000
_HORIZON_MIN = 24 * 60
#: Cost per minute a priority customer is served after the shift starts.
_PRIORITY_SOFT_COEF = 20


def _import_ortools() -> tuple[Any, Any] | None:
    """Import OR-Tools once, recording availability.  Never raises."""
    global AVAILABLE, _CHECKED
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except Exception:  # ImportError, or a broken/partial installation
        AVAILABLE = False
        _CHECKED = True
        return None
    AVAILABLE = True
    _CHECKED = True
    return pywrapcp, routing_enums_pb2


def is_available() -> bool:
    """Whether the exact solver can be used in this installation."""
    if not _CHECKED:
        _import_ortools()
    return AVAILABLE


def solve(problem: VrpProblem, time_limit_s: int = 10) -> VrpSolution:
    """
    Solve *problem* with OR-Tools' routing library.

    Raises :class:`RuntimeError` when OR-Tools is not installed or produces no
    solution — :func:`app.routing.optimize` catches that and falls back.
    """
    started = time.perf_counter()
    modules = _import_ortools()
    if modules is None:
        raise RuntimeError("ortools_not_available")
    pywrapcp, routing_enums_pb2 = modules

    stops = problem.stops
    vehicles = problem.vehicles
    n = len(stops)
    m = len(vehicles)
    if n == 0 or m == 0:
        return VrpSolution(
            unassigned=[s.customer_id for s in stops],
            solver_name=SOLVER_NAME,
            seconds=round(time.perf_counter() - started, 3),
        )

    factor = max(1.0, problem.detour_factor)
    speed = problem.avg_speed_kmh if problem.avg_speed_kmh > 0 else 30.0

    # Node layout: 0..n-1 customers, then a start and an end node per vehicle,
    # so vans that leave from and return to different depots are modelled exactly.
    coords: list[tuple[float, float]] = [(s.lat, s.lng) for s in stops]
    starts: list[int] = []
    ends: list[int] = []
    for v in vehicles:
        starts.append(len(coords))
        coords.append((v.start_lat, v.start_lng))
        ends.append(len(coords))
        coords.append((v.end_lat, v.end_lng))

    total_nodes = len(coords)
    dist_m: list[list[int]] = [[0] * total_nodes for _ in range(total_nodes)]
    for i in range(total_nodes):
        for j in range(i + 1, total_nodes):
            metres = int(round(pair_km(coords[i], coords[j], factor) * _SCALE))
            dist_m[i][j] = metres
            dist_m[j][i] = metres

    service_min = [int(round(s.service_minutes)) for s in stops] + [0] * (2 * m)
    volume = [int(round(s.demand_volume * _DEMAND_SCALE)) for s in stops] + [0] * (2 * m)
    weight = [int(round(s.demand_weight * _DEMAND_SCALE)) for s in stops] + [0] * (2 * m)

    manager = pywrapcp.RoutingIndexManager(total_nodes, m, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_index: int, to_index: int) -> int:
        return dist_m[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]

    transit_cb = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    def _big(value: float) -> int:
        """0 capacity in the domain model means 'unconstrained'."""
        return int(round(value * _DEMAND_SCALE)) if value > 0 else 10**9

    def volume_cb(from_index: int) -> int:
        return volume[manager.IndexToNode(from_index)]

    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(volume_cb),
        0,
        [_big(v.capacity_volume) for v in vehicles],
        True,
        "Volume",
    )

    def weight_cb(from_index: int) -> int:
        return weight[manager.IndexToNode(from_index)]

    routing.AddDimensionWithVehicleCapacity(
        routing.RegisterUnaryTransitCallback(weight_cb),
        0,
        [_big(v.capacity_weight) for v in vehicles],
        True,
        "Weight",
    )

    if any(v.max_stops > 0 for v in vehicles):
        def stop_count_cb(from_index: int) -> int:
            return 1 if manager.IndexToNode(from_index) < n else 0

        routing.AddDimensionWithVehicleCapacity(
            routing.RegisterUnaryTransitCallback(stop_count_cb),
            0,
            [v.max_stops if v.max_stops > 0 else n for v in vehicles],
            True,
            "Stops",
        )

    def time_cb(from_index: int, to_index: int) -> int:
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        travel = int(round(dist_m[i][j] / _SCALE / speed * 60.0))
        return service_min[i] + travel

    routing.AddDimension(
        routing.RegisterTransitCallback(time_cb),
        _HORIZON_MIN,          # slack: allowed waiting before an opening time
        2 * _HORIZON_MIN,      # cumul upper bound
        False,                 # start cumul is the shift start, not zero
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    earliest_start = min(int(v.start_minutes) for v in vehicles)
    for k, v in enumerate(vehicles):
        if v.max_minutes > 0:
            time_dim.SetSpanUpperBoundForVehicle(int(v.max_minutes), k)
        start_index = routing.Start(k)
        time_dim.CumulVar(start_index).SetRange(int(v.start_minutes), int(v.start_minutes))

    for node, stop in enumerate(stops):
        index = manager.NodeToIndex(node)
        low = int(stop.ready_minutes) if stop.ready_minutes is not None else 0
        high = int(stop.due_minutes) if stop.due_minutes is not None else 2 * _HORIZON_MIN
        time_dim.CumulVar(index).SetRange(low, max(low, high))
        if stop.priority:
            # Soft "serve me early" pressure without making the plan infeasible.
            time_dim.SetCumulVarSoftUpperBound(index, earliest_start, _PRIORITY_SOFT_COEF)
        penalty = int(
            (PRIORITY_UNASSIGNED_PENALTY_KM if stop.priority else UNASSIGNED_PENALTY_KM)
            * _SCALE
        )
        routing.AddDisjunction([index], penalty)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(max(1, int(time_limit_s)))
    params.log_search = False

    assignment = routing.SolveWithParameters(params)
    if assignment is None:
        raise RuntimeError("ortools_no_solution")

    routes: list[VrpRoute] = []
    visited: set[int] = set()
    total_km = 0.0

    for k, vehicle in enumerate(vehicles):
        index = routing.Start(k)
        seq: list[int] = []
        arrivals: list[float] = []
        metres = 0
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node < n:
                seq.append(node)
                arrivals.append(float(assignment.Min(time_dim.CumulVar(index))))
                visited.add(node)
            nxt = assignment.Value(routing.NextVar(index))
            metres += routing.GetArcCostForVehicle(index, nxt, k)
            index = nxt
        if not seq:
            continue
        duration = float(assignment.Min(time_dim.CumulVar(index)) - vehicle.start_minutes)
        km = metres / _SCALE
        total_km += km
        routes.append(
            VrpRoute(
                vehicle_id=vehicle.vehicle_id,
                stop_ids=[stops[node].customer_id for node in seq],
                distance_km=round(km, 3),
                duration_min=round(duration, 1),
                load_volume=round(sum(stops[node].demand_volume for node in seq), 3),
                load_weight=round(sum(stops[node].demand_weight for node in seq), 3),
                arrival_minutes=arrivals,
            )
        )

    unassigned_nodes = sorted(node for node in range(n) if node not in visited)
    penalty = sum(
        PRIORITY_UNASSIGNED_PENALTY_KM if stops[node].priority else UNASSIGNED_PENALTY_KM
        for node in unassigned_nodes
    )

    log.info(
        "ortools solved: %d routes, %d unassigned, %.2f km",
        len(routes), len(unassigned_nodes), total_km,
    )
    return VrpSolution(
        routes=routes,
        unassigned=[stops[node].customer_id for node in unassigned_nodes],
        total_distance_km=round(total_km, 3),
        objective=round(total_km + penalty, 3),
        solver_name=SOLVER_NAME,
        seconds=round(time.perf_counter() - started, 3),
    )
