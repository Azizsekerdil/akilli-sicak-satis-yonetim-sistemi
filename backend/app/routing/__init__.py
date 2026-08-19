"""
Route optimisation package.

:func:`optimize` is the only entry point the rest of the application uses.  It
prefers OR-Tools when that package is installed and silently falls back to the
built-in savings solver otherwise — optimisation is a field-critical feature,
so a missing optional dependency must never turn into an error the salesperson
sees.
"""

from __future__ import annotations

from app.core.logging_config import get_logger
from app.routing import solver as savings_solver
from app.routing.distance import (
    DEFAULT_DETOUR_FACTOR,
    DEFAULT_SPEED_KMH,
    bounding_box,
    build_matrix,
    centroid,
    minutes_for,
    pair_km,
    path_distance,
    time_matrix,
)
from app.routing.solver import (
    VrpProblem,
    VrpRoute,
    VrpSolution,
    VrpStop,
    VrpVehicle,
)

log = get_logger("app.routing")

__all__ = [
    "VrpProblem",
    "VrpRoute",
    "VrpSolution",
    "VrpStop",
    "VrpVehicle",
    "build_matrix",
    "time_matrix",
    "minutes_for",
    "pair_km",
    "path_distance",
    "centroid",
    "bounding_box",
    "optimize",
    "available_solvers",
    "DEFAULT_DETOUR_FACTOR",
    "DEFAULT_SPEED_KMH",
]


def available_solvers() -> list[str]:
    """Names of the solvers this installation can actually run."""
    names = [savings_solver.SOLVER_NAME]
    try:
        from app.routing import ortools_solver

        if ortools_solver.is_available():
            names.insert(0, ortools_solver.SOLVER_NAME)
    except Exception:  # pragma: no cover - only on a broken install
        pass
    return names


def optimize(
    problem: VrpProblem,
    *,
    prefer_exact: bool = True,
    time_limit_s: int = 10,
) -> VrpSolution:
    """
    Produce a routing plan, using the best solver available.

    Falls back to the dependency-free savings solver whenever OR-Tools is
    absent, errors, or returns nothing usable.  The solver that actually ran is
    recorded in ``VrpSolution.solver_name`` so the plan is reproducible.
    """
    if prefer_exact:
        try:
            from app.routing import ortools_solver

            if ortools_solver.is_available():
                solution = ortools_solver.solve(problem, time_limit_s=time_limit_s)
                if solution.routes or not problem.stops:
                    return solution
                log.warning("OR-Tools returned no routes; falling back to savings solver")
        except Exception as exc:  # never let an optional dependency break planning
            log.warning("OR-Tools optimisation failed (%s); using savings solver", exc)

    return savings_solver.solve(problem, time_limit_s=float(time_limit_s))
