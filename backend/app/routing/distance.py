"""
Geographic distance and travel-time matrices.

The system deliberately has **no external routing server dependency**: a
distributor must be able to run the whole product on a laptop.  Straight-line
(haversine) distance inflated by a detour factor is within a few percent of
real road distance for the short, dense urban legs a van sales route is made
of, which is accurate enough to order stops correctly.
"""

from __future__ import annotations

from app.core.utils import haversine_km

#: Turkish urban road networks run roughly 1.3-1.4x the crow-flight distance.
DEFAULT_DETOUR_FACTOR: float = 1.35
#: Average door-to-door speed of a van in mixed city traffic.
DEFAULT_SPEED_KMH: float = 30.0

Point = tuple[float, float]


def pair_km(a: Point, b: Point, detour_factor: float = DEFAULT_DETOUR_FACTOR) -> float:
    """Road-approximate distance in kilometres between two ``(lat, lng)`` points."""
    return haversine_km(a[0], a[1], b[0], b[1]) * max(1.0, detour_factor)


def build_matrix(
    points: list[Point],
    *,
    detour_factor: float = DEFAULT_DETOUR_FACTOR,
) -> list[list[float]]:
    """
    Symmetric distance matrix in kilometres.

    Only the upper triangle is computed — haversine is symmetric, and halving
    the trigonometry matters once a region has a few hundred customers.
    """
    n = len(points)
    matrix: list[list[float]] = [[0.0] * n for _ in range(n)]
    factor = max(1.0, detour_factor)
    for i in range(n):
        lat_i, lng_i = points[i]
        row_i = matrix[i]
        for j in range(i + 1, n):
            lat_j, lng_j = points[j]
            km = haversine_km(lat_i, lng_i, lat_j, lng_j) * factor
            row_i[j] = km
            matrix[j][i] = km
    return matrix


def time_matrix(
    dist: list[list[float]],
    avg_speed_kmh: float = DEFAULT_SPEED_KMH,
) -> list[list[float]]:
    """Convert a kilometre matrix into a travel-time matrix in minutes."""
    speed = avg_speed_kmh if avg_speed_kmh > 0 else DEFAULT_SPEED_KMH
    return [[km / speed * 60.0 for km in row] for row in dist]


def minutes_for(km: float, avg_speed_kmh: float = DEFAULT_SPEED_KMH) -> float:
    """Travel time in minutes for a single leg."""
    speed = avg_speed_kmh if avg_speed_kmh > 0 else DEFAULT_SPEED_KMH
    return km / speed * 60.0


def path_distance(matrix: list[list[float]], order: list[int]) -> float:
    """Total length of a visiting order through a distance matrix."""
    return sum(matrix[order[i]][order[i + 1]] for i in range(len(order) - 1))


def centroid(points: list[Point]) -> Point | None:
    """
    Arithmetic centre of a set of points.

    Used as a last-resort depot when no warehouse has coordinates: optimising
    around the customer cloud's centre still yields a sane visiting order.
    """
    if not points:
        return None
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def nearest_index(points: list[Point], target: Point) -> int | None:
    """Index of the point closest to *target* (deterministic on ties: lowest index)."""
    best: int | None = None
    best_km = float("inf")
    for i, p in enumerate(points):
        km = haversine_km(p[0], p[1], target[0], target[1])
        if km < best_km:
            best_km = km
            best = i
    return best


def bounding_box(points: list[Point]) -> dict[str, float] | None:
    """South-west / north-east corners — lets the map screen fit its viewport."""
    if not points:
        return None
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return {
        "min_lat": min(lats),
        "min_lng": min(lngs),
        "max_lat": max(lats),
        "max_lng": max(lngs),
    }
