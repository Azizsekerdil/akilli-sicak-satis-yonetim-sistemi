"""
Self-contained capacitated vehicle-routing solver.

No third-party dependency: OR-Tools is optional and this module is what runs
when it is absent, so route optimisation can never be "unavailable" in the
field.

Pipeline
--------
1. **Clarke-Wright savings** builds an initial set of routes by repeatedly
   merging the two route ends whose merge saves the most distance.
2. **Assignment** maps those routes onto the real (possibly heterogeneous)
   fleet, best-fit-decreasing so big loads get big vans.
3. **Cheapest insertion** places any leftover customer into whichever route
   absorbs it most cheaply.
4. **2-opt + Or-opt** local search polishes each route.

Every step is deterministic — identical input always yields an identical plan,
which is what makes a suggested route defensible to a supervisor.

Constraints honoured: vehicle volume and weight capacity, per-stop service
time, customer opening/closing windows, the vehicle's maximum workday, and
priority customers (heavily penalised if dropped, pulled earlier in the day).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.routing.distance import (
    DEFAULT_DETOUR_FACTOR,
    DEFAULT_SPEED_KMH,
    build_matrix,
    pair_km,
)

SOLVER_NAME = "SAVINGS"

#: Kilometre-equivalent charged per hour a priority customer waits.  Small
#: enough that it never justifies a large detour, big enough to break ties in
#: favour of serving key accounts in the morning.
PRIORITY_DELAY_WEIGHT: float = 1.0
#: Kilometre-equivalent charged for leaving a customer out of the plan.
UNASSIGNED_PENALTY_KM: float = 100.0
PRIORITY_UNASSIGNED_PENALTY_KM: float = 400.0

#: Local search safety valves — a field user waits for this call synchronously.
DEFAULT_TIME_LIMIT_S: float = 8.0
MAX_LOCAL_SEARCH_PASSES: int = 12
OR_OPT_MAX_SEGMENT: int = 3
#: Hard cap on stops in one route; beyond this a day is not physically drivable.
MAX_STOPS_PER_ROUTE: int = 120

EPS: float = 1e-9


# ===========================================================================
# Problem / solution model
# ===========================================================================
@dataclass(slots=True)
class VrpStop:
    """One customer to visit."""

    customer_id: int
    lat: float
    lng: float
    demand_volume: float = 0.0
    demand_weight: float = 0.0
    service_minutes: float = 10.0
    #: Opening time as minutes since midnight; the van waits if it arrives early.
    ready_minutes: int | None = None
    #: Closing time as minutes since midnight; arriving later is infeasible.
    due_minutes: int | None = None
    priority: bool = False


@dataclass(slots=True)
class VrpVehicle:
    """One van, with its own depot geometry and shift."""

    vehicle_id: int
    capacity_volume: float = 0.0
    capacity_weight: float = 0.0
    start_lat: float = 0.0
    start_lng: float = 0.0
    end_lat: float = 0.0
    end_lng: float = 0.0
    start_minutes: int = 480          # 08:00
    max_minutes: int = 540            # 9 hours
    #: Optional cap on stops for this van (0 = no cap).  Set it to spread work
    #: evenly when the planner wants every van used, not just the cheapest plan.
    max_stops: int = 0


@dataclass(slots=True)
class VrpProblem:
    """A day's routing question."""

    depot: tuple[float, float]
    stops: list[VrpStop] = field(default_factory=list)
    vehicles: list[VrpVehicle] = field(default_factory=list)
    avg_speed_kmh: float = DEFAULT_SPEED_KMH
    detour_factor: float = DEFAULT_DETOUR_FACTOR


@dataclass(slots=True)
class VrpRoute:
    """One vehicle's ordered plan.  ``stop_ids`` holds customer ids."""

    vehicle_id: int
    stop_ids: list[int] = field(default_factory=list)
    distance_km: float = 0.0
    duration_min: float = 0.0
    load_volume: float = 0.0
    load_weight: float = 0.0
    #: Arrival clock (minutes since midnight) per entry in ``stop_ids``.
    arrival_minutes: list[float] = field(default_factory=list)


@dataclass(slots=True)
class VrpSolution:
    routes: list[VrpRoute] = field(default_factory=list)
    unassigned: list[int] = field(default_factory=list)
    total_distance_km: float = 0.0
    objective: float = 0.0
    solver_name: str = SOLVER_NAME
    seconds: float = 0.0


# ===========================================================================
# Internal evaluation
# ===========================================================================
@dataclass(slots=True)
class _Eval:
    """Cost and feasibility of one candidate sequence on one vehicle."""

    feasible: bool = True
    distance_km: float = 0.0
    duration_min: float = 0.0
    load_volume: float = 0.0
    load_weight: float = 0.0
    cost: float = 0.0
    arrivals: list[float] = field(default_factory=list)
    reason: str = ""


class _Model:
    """Pre-computed geometry shared by every move the search evaluates."""

    __slots__ = (
        "problem", "stops", "n", "speed", "factor", "d", "depot_d",
        "vehicles", "_legs", "deadline", "evaluations",
    )

    def __init__(
        self,
        problem: VrpProblem,
        extra_vehicles: list[VrpVehicle] | None = None,
        deadline: float | None = None,
    ) -> None:
        self.problem = problem
        self.stops = problem.stops
        self.n = len(problem.stops)
        self.speed = problem.avg_speed_kmh if problem.avg_speed_kmh > 0 else DEFAULT_SPEED_KMH
        self.factor = max(1.0, problem.detour_factor)
        self.deadline = deadline if deadline is not None else float("inf")
        self.evaluations = 0

        points = [(s.lat, s.lng) for s in problem.stops]
        self.d = build_matrix(points, detour_factor=self.factor)
        self.depot_d = [pair_km(problem.depot, p, self.factor) for p in points]

        self.vehicles = list(problem.vehicles) + list(extra_vehicles or [])
        self._legs: list[tuple[list[float], list[float]]] = []
        for v in self.vehicles:
            start = [pair_km((v.start_lat, v.start_lng), p, self.factor) for p in points]
            end = [pair_km((v.end_lat, v.end_lng), p, self.factor) for p in points]
            self._legs.append((start, end))

    # -- helpers ---------------------------------------------------------
    def minutes(self, km: float) -> float:
        return km / self.speed * 60.0

    def out_of_time(self) -> bool:
        return time.perf_counter() > self.deadline

    def evaluate(self, seq: list[int], k: int) -> _Eval:
        """
        Walk *seq* on vehicle *k*, returning distance, clock and feasibility.

        The clock model is: drive, wait until opening if early, serve, drive on.
        A closing-time violation or a capacity/shift overrun makes the whole
        sequence infeasible — the search then simply never selects it.
        """
        self.evaluations += 1
        v = self.vehicles[k]
        start_legs, end_legs = self._legs[k]

        ev = _Eval(arrivals=[])
        if not seq:
            return ev
        if len(seq) > MAX_STOPS_PER_ROUTE or (0 < v.max_stops < len(seq)):
            ev.feasible = False
            ev.reason = "stop_count"
            return ev

        clock = float(v.start_minutes)
        distance = 0.0
        volume = 0.0
        weight = 0.0
        priority_cost = 0.0
        prev = -1

        for node in seq:
            stop = self.stops[node]
            leg = start_legs[node] if prev < 0 else self.d[prev][node]
            distance += leg
            clock += self.minutes(leg)
            if stop.ready_minutes is not None and clock < stop.ready_minutes:
                clock = float(stop.ready_minutes)
            if stop.due_minutes is not None and clock > stop.due_minutes + EPS:
                ev.feasible = False
                ev.reason = "time_window"
                return ev
            ev.arrivals.append(clock)
            if stop.priority:
                priority_cost += (clock - v.start_minutes) / 60.0
            clock += stop.service_minutes
            volume += stop.demand_volume
            weight += stop.demand_weight
            if v.capacity_volume > 0 and volume > v.capacity_volume + EPS:
                ev.feasible = False
                ev.reason = "volume"
                return ev
            if v.capacity_weight > 0 and weight > v.capacity_weight + EPS:
                ev.feasible = False
                ev.reason = "weight"
                return ev
            prev = node

        back = end_legs[prev]
        distance += back
        clock += self.minutes(back)

        ev.distance_km = distance
        ev.duration_min = clock - v.start_minutes
        ev.load_volume = volume
        ev.load_weight = weight
        if v.max_minutes > 0 and ev.duration_min > v.max_minutes + EPS:
            ev.feasible = False
            ev.reason = "shift"
            return ev
        ev.cost = distance + PRIORITY_DELAY_WEIGHT * priority_cost
        return ev


# ===========================================================================
# Construction — Clarke & Wright savings
# ===========================================================================
def _clarke_wright(model: _Model, ref_k: int) -> tuple[list[list[int]], list[int]]:
    """
    Build routes by merging the pair of route ends with the greatest saving.

    ``saving(i, j) = d(depot, i) + d(depot, j) - d(i, j)`` — how much shorter it
    is to serve i and j on one trip than on two separate out-and-back trips.

    Returns the constructed routes plus the stops no vehicle can serve even on
    a dedicated trip (a shop that shuts before any van could reach it, say).
    Those are reported, never silently dropped.
    """
    n = model.n
    routes: list[list[int]] = []
    route_of: dict[int, int] = {}
    impossible: list[int] = []

    for i in range(n):
        if not model.evaluate([i], ref_k).feasible:
            impossible.append(i)           # unservable even on its own
            continue
        route_of[i] = len(routes)
        routes.append([i])

    savings: list[tuple[float, int, int]] = []
    servable = sorted(route_of)
    for a in range(len(servable)):
        i = servable[a]
        for b in range(a + 1, len(servable)):
            j = servable[b]
            s = model.depot_d[i] + model.depot_d[j] - model.d[i][j]
            savings.append((-s, i, j))
    savings.sort()                          # deterministic: (-saving, i, j)

    for _, i, j in savings:
        if model.out_of_time():
            break
        ri = route_of.get(i)
        rj = route_of.get(j)
        if ri is None or rj is None or ri == rj:
            continue
        a = routes[ri]
        b = routes[rj]
        if len(a) + len(b) > MAX_STOPS_PER_ROUTE:
            continue

        candidates: list[list[int]] = []
        if a[-1] == i and b[0] == j:
            candidates.append(a + b)
        if b[-1] == j and a[0] == i:
            candidates.append(b + a)
        if a[-1] == i and b[-1] == j:
            candidates.append(a + b[::-1])
        if a[0] == i and b[0] == j:
            candidates.append(a[::-1] + b)
        if not candidates:
            continue

        for merged in candidates:
            if not model.evaluate(merged, ref_k).feasible:
                continue
            routes[ri] = merged
            routes[rj] = []
            for node in merged:
                route_of[node] = ri
            break

    return [r for r in routes if r], impossible


# ===========================================================================
# Local search
# ===========================================================================
def _two_opt(model: _Model, seq: list[int], k: int, current: _Eval) -> tuple[list[int], _Eval]:
    """Reverse a segment whenever doing so removes a crossing."""
    best = list(seq)
    best_ev = current
    n = len(best)
    if n < 4:
        return best, best_ev

    improved = True
    while improved and not model.out_of_time():
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                cand = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                ev = model.evaluate(cand, k)
                if ev.feasible and ev.cost < best_ev.cost - EPS:
                    best, best_ev = cand, ev
                    improved = True
            if model.out_of_time():
                break
    return best, best_ev


def _or_opt(model: _Model, seq: list[int], k: int, current: _Eval) -> tuple[list[int], _Eval]:
    """Relocate short chains of 1-3 consecutive stops elsewhere in the route."""
    best = list(seq)
    best_ev = current
    if len(best) < 3:
        return best, best_ev

    improved = True
    while improved and not model.out_of_time():
        improved = False
        n = len(best)
        for seg_len in range(1, min(OR_OPT_MAX_SEGMENT, n - 1) + 1):
            for i in range(0, n - seg_len + 1):
                segment = best[i : i + seg_len]
                rest = best[:i] + best[i + seg_len :]
                for pos in range(len(rest) + 1):
                    if pos == i:
                        continue
                    for chunk in (segment, segment[::-1]):
                        if seg_len == 1 and chunk is not segment:
                            continue
                        cand = rest[:pos] + chunk + rest[pos:]
                        ev = model.evaluate(cand, k)
                        if ev.feasible and ev.cost < best_ev.cost - EPS:
                            best, best_ev = cand, ev
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break
    return best, best_ev


def _local_search(model: _Model, seq: list[int], k: int) -> tuple[list[int], _Eval]:
    """Alternate 2-opt and Or-opt until neither finds an improvement."""
    best = list(seq)
    best_ev = model.evaluate(best, k)
    for _ in range(MAX_LOCAL_SEARCH_PASSES):
        if model.out_of_time():
            break
        before = best_ev.cost
        best, best_ev = _two_opt(model, best, k, best_ev)
        best, best_ev = _or_opt(model, best, k, best_ev)
        if best_ev.cost >= before - EPS:
            break
    return best, best_ev


# ===========================================================================
# Assignment
# ===========================================================================
def _least_valuable(model: _Model, seq: list[int]) -> int:
    """Index of the stop to drop first when a route will not fit a vehicle."""
    for idx in range(len(seq) - 1, -1, -1):
        if not model.stops[seq[idx]].priority:
            return idx
    return len(seq) - 1


def _route_sort_key(model: _Model, seq: list[int]) -> tuple[int, float, float, int, int]:
    priority_count = sum(1 for node in seq if model.stops[node].priority)
    volume = sum(model.stops[node].demand_volume for node in seq)
    weight = sum(model.stops[node].demand_weight for node in seq)
    return (-priority_count, -volume, -weight, -len(seq), seq[0])


def _vehicle_fit_key(model: _Model, k: int) -> tuple[float, float, int, int]:
    v = model.vehicles[k]
    # 0-capacity means "unlimited"; sort those last so real vans are used first.
    vol = v.capacity_volume if v.capacity_volume > 0 else float("inf")
    wgt = v.capacity_weight if v.capacity_weight > 0 else float("inf")
    return (vol, wgt, v.max_minutes, k)


def _assign(
    model: _Model,
    routes: list[list[int]],
    fleet: list[int],
) -> tuple[list[tuple[int, list[int]]], list[int]]:
    """
    Best-fit-decreasing placement of constructed routes onto real vehicles.

    Heaviest and most priority-laden routes are placed first, each into the
    *smallest* van that can still take it, so the remaining capacity stays as
    useful as possible for what follows.
    """
    assigned: list[tuple[int, list[int]]] = []
    pool: list[int] = []
    free = sorted(fleet, key=lambda k: _vehicle_fit_key(model, k))

    for seq in sorted(routes, key=lambda r: _route_sort_key(model, r)):
        if not free:
            pool.extend(seq)
            continue

        placed = False
        for k in free:
            if model.evaluate(seq, k).feasible:
                assigned.append((k, list(seq)))
                free.remove(k)
                placed = True
                break
        if placed:
            continue

        # Nothing takes the whole route — shrink it until something does.
        work = list(seq)
        dropped: list[int] = []
        while work:
            hit = next((k for k in free if model.evaluate(work, k).feasible), None)
            if hit is not None:
                assigned.append((hit, work))
                free.remove(hit)
                break
            dropped.append(work.pop(_least_valuable(model, work)))
        pool.extend(dropped)

    return assigned, pool


def _insert_pool(
    model: _Model,
    assigned: list[tuple[int, list[int]]],
    pool: list[int],
) -> list[int]:
    """Cheapest-insertion of leftover customers; priority accounts go first."""
    unassigned: list[int] = []
    order = sorted(pool, key=lambda node: (not model.stops[node].priority, node))

    for node in order:
        best_key: tuple[float, int, int] | None = None
        best_route: list[int] | None = None
        best_slot = -1
        for slot, (k, seq) in enumerate(assigned):
            if len(seq) >= MAX_STOPS_PER_ROUTE:
                continue
            base = model.evaluate(seq, k)
            for pos in range(len(seq) + 1):
                cand = seq[:pos] + [node] + seq[pos:]
                ev = model.evaluate(cand, k)
                if not ev.feasible:
                    continue
                key = (ev.cost - base.cost, slot, pos)
                if best_key is None or key < best_key:
                    best_key = key
                    best_route = cand
                    best_slot = slot
        if best_route is None:
            unassigned.append(node)
        else:
            assigned[best_slot] = (assigned[best_slot][0], best_route)

    return unassigned


# ===========================================================================
# Entry point
# ===========================================================================
def _reference_vehicle(problem: VrpProblem) -> VrpVehicle:
    """
    Synthetic van used only during construction.

    It is as capable as the best real van, so the savings phase is never the
    thing that fragments a route — the assignment phase applies the real,
    per-vehicle limits.
    """
    lat, lng = problem.depot
    if not problem.vehicles:
        return VrpVehicle(
            vehicle_id=-1,
            start_lat=lat, start_lng=lng, end_lat=lat, end_lng=lng,
        )
    stop_caps = [v.max_stops for v in problem.vehicles]
    return VrpVehicle(
        vehicle_id=-1,
        capacity_volume=max(v.capacity_volume for v in problem.vehicles),
        capacity_weight=max(v.capacity_weight for v in problem.vehicles),
        start_lat=lat,
        start_lng=lng,
        end_lat=lat,
        end_lng=lng,
        start_minutes=min(v.start_minutes for v in problem.vehicles),
        max_minutes=max(v.max_minutes for v in problem.vehicles),
        # Unlimited if any real van is unlimited, otherwise the loosest cap —
        # construction must never be stricter than the fleet it feeds.
        max_stops=0 if any(cap <= 0 for cap in stop_caps) else max(stop_caps),
    )


def _objective(model: _Model, total_km: float, unassigned: list[int]) -> float:
    penalty = 0.0
    for node in unassigned:
        penalty += (
            PRIORITY_UNASSIGNED_PENALTY_KM
            if model.stops[node].priority
            else UNASSIGNED_PENALTY_KM
        )
    return total_km + penalty


def solve(problem: VrpProblem, *, time_limit_s: float = DEFAULT_TIME_LIMIT_S) -> VrpSolution:
    """Solve *problem* with savings construction plus 2-opt / Or-opt polishing."""
    started = time.perf_counter()

    if not problem.stops:
        return VrpSolution(solver_name=SOLVER_NAME, seconds=0.0)

    ref = _reference_vehicle(problem)
    model = _Model(
        problem,
        extra_vehicles=[ref],
        deadline=started + max(0.5, time_limit_s),
    )
    ref_k = len(problem.vehicles)

    if not problem.vehicles:
        return VrpSolution(
            unassigned=[s.customer_id for s in problem.stops],
            objective=_objective(model, 0.0, list(range(model.n))),
            solver_name=SOLVER_NAME,
            seconds=time.perf_counter() - started,
        )

    constructed, impossible = _clarke_wright(model, ref_k)
    assigned, pool = _assign(model, constructed, list(range(len(problem.vehicles))))
    unassigned_nodes = _insert_pool(model, assigned, pool) + impossible

    routes: list[VrpRoute] = []
    total_km = 0.0
    for k, seq in assigned:
        if not seq:
            continue
        seq, ev = _local_search(model, seq, k)
        if not ev.feasible:                 # defensive: keep only valid plans
            unassigned_nodes.extend(seq)
            continue
        vehicle = model.vehicles[k]
        routes.append(
            VrpRoute(
                vehicle_id=vehicle.vehicle_id,
                stop_ids=[model.stops[node].customer_id for node in seq],
                distance_km=round(ev.distance_km, 3),
                duration_min=round(ev.duration_min, 1),
                load_volume=round(ev.load_volume, 3),
                load_weight=round(ev.load_weight, 3),
                arrival_minutes=[round(a, 1) for a in ev.arrivals],
            )
        )
        total_km += ev.distance_km

    routes.sort(key=lambda r: r.vehicle_id)
    unassigned_nodes.sort()

    return VrpSolution(
        routes=routes,
        unassigned=[model.stops[node].customer_id for node in unassigned_nodes],
        total_distance_km=round(total_km, 3),
        objective=round(_objective(model, total_km, unassigned_nodes), 3),
        solver_name=SOLVER_NAME,
        seconds=round(time.perf_counter() - started, 3),
    )
