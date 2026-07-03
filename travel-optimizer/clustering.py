"""Geography/travel-time aware clustering of places into travel days.

Clustering here is deliberately simple (no numpy/scipy/sklearn dependency):
greedy nearest-neighbor agglomeration to form geographic clusters, then a
merge/split pass to land on exactly ``number_of_days`` day-groups. This is
"good enough" for the place counts a travel itinerary deals with (tens, not
thousands) and keeps the algorithm easy to read and debug.

Key property that falls out of "always merge the closest pair first": a
genuinely far outlier (e.g. a Great Wall day trip) naturally stays its own
cluster/day as long as there are enough days to avoid forcing a merge. If a
merge becomes unavoidable, the caller (planner.py) is informed via
``Cluster.max_internal_travel_minutes`` so it can raise a reminder about the
extra travel time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models import Place
from providers import MapProvider
from models import TransportMode


def _coords(place: Place) -> tuple[float, float]:
    if not place.is_geocoded:
        raise ValueError(f"Place '{place.name}' has no coordinates; geocode before clustering")
    return place.lat, place.lng  # type: ignore[return-value]


def _centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    lat = sum(c[0] for c in coords) / len(coords)
    lng = sum(c[1] for c in coords) / len(coords)
    return lat, lng


@dataclass
class Cluster:
    places: list[Place] = field(default_factory=list)

    def centroid(self) -> tuple[float, float]:
        return _centroid([_coords(p) for p in self.places])


def _travel_minutes(
    a: tuple[float, float], b: tuple[float, float], provider: MapProvider, mode: TransportMode, city: str | None,
) -> float:
    if a == b:
        return 0.0
    return provider.travel_time(a, b, mode, city).duration_minutes


def cluster_places_by_travel_time(
    places: list[Place],
    provider: MapProvider,
    mode: TransportMode,
    city: str | None = None,
    max_cluster_minutes: float = 25.0,
) -> list[Cluster]:
    """Agglomerative clustering: repeatedly merge the two closest clusters
    (by centroid-to-centroid travel time) as long as the closest pair is
    within ``max_cluster_minutes``. Ungeocoded places are skipped (caller is
    responsible for flagging them separately)."""

    clusters = [Cluster([p]) for p in places if p.is_geocoded]

    while len(clusters) > 1:
        best_idx: tuple[int, int] | None = None
        best_minutes: float | None = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                minutes = _travel_minutes(clusters[i].centroid(), clusters[j].centroid(), provider, mode, city)
                if best_minutes is None or minutes < best_minutes:
                    best_minutes, best_idx = minutes, (i, j)

        if best_idx is None or best_minutes > max_cluster_minutes:
            break

        i, j = best_idx
        merged = Cluster(clusters[i].places + clusters[j].places)
        clusters = [c for k, c in enumerate(clusters) if k not in (i, j)] + [merged]

    return clusters


def _merge_down_to(
    clusters: list[Cluster], target_count: int, provider: MapProvider, mode: TransportMode, city: str | None,
) -> list[Cluster]:
    clusters = list(clusters)
    while len(clusters) > target_count:
        best_idx: tuple[int, int] | None = None
        best_minutes: float | None = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                minutes = _travel_minutes(clusters[i].centroid(), clusters[j].centroid(), provider, mode, city)
                if best_minutes is None or minutes < best_minutes:
                    best_minutes, best_idx = minutes, (i, j)
        assert best_idx is not None
        i, j = best_idx
        merged = Cluster(clusters[i].places + clusters[j].places)
        clusters = [c for k, c in enumerate(clusters) if k not in (i, j)] + [merged]
    return clusters


def _split_up_to(clusters: list[Cluster], target_count: int) -> list[Cluster]:
    clusters = list(clusters)
    while len(clusters) < target_count:
        # Split the largest splittable cluster along its widest geographic axis.
        splittable = [c for c in clusters if len(c.places) >= 2]
        if not splittable:
            break
        biggest = max(splittable, key=lambda c: len(c.places))
        coords = [_coords(p) for p in biggest.places]
        lat_spread = max(c[0] for c in coords) - min(c[0] for c in coords)
        lng_spread = max(c[1] for c in coords) - min(c[1] for c in coords)
        axis = 0 if lat_spread >= lng_spread else 1
        ordered = sorted(biggest.places, key=lambda p: _coords(p)[axis])
        mid = len(ordered) // 2
        left, right = Cluster(ordered[:mid]), Cluster(ordered[mid:])
        clusters = [c for c in clusters if c is not biggest] + [left, right]
    return clusters


def assign_clusters_to_days(
    clusters: list[Cluster],
    number_of_days: int,
    hotel_coords: tuple[float, float] | None,
    provider: MapProvider,
    mode: TransportMode,
    city: str | None = None,
) -> list[list[Place]]:
    """Reconcile however many geographic clusters we found with the number of
    travel days available, then order the resulting day-groups so the
    itinerary flows sensibly (closer-to-hotel clusters earlier).

    Per the spec, this never forces every place into the plan - clusters
    with a single very-low-value place may still get dropped later by the
    planner's feasibility/route-building step, not here.
    """

    if not clusters:
        return [[] for _ in range(number_of_days)]

    if len(clusters) > number_of_days:
        clusters = _merge_down_to(clusters, number_of_days, provider, mode, city)
    elif len(clusters) < number_of_days:
        clusters = _split_up_to(clusters, number_of_days)

    if hotel_coords is not None:
        clusters = sorted(clusters, key=lambda c: _travel_minutes(hotel_coords, c.centroid(), provider, mode, city))
    else:
        clusters = sorted(clusters, key=lambda c: -_priority_weight(c))

    groups = [c.places for c in clusters]
    while len(groups) < number_of_days:
        groups.append([])
    return groups[:number_of_days]


def max_internal_travel_minutes(
    places: list[Place], provider: MapProvider, mode: TransportMode, city: str | None = None,
) -> float:
    """Worst-case pairwise travel time within a day's place list - used to
    flag days that ended up geographically stretched after a forced merge."""

    geocoded = [p for p in places if p.is_geocoded]
    if len(geocoded) < 2:
        return 0.0
    worst = 0.0
    for i in range(len(geocoded)):
        for j in range(i + 1, len(geocoded)):
            worst = max(worst, _travel_minutes(_coords(geocoded[i]), _coords(geocoded[j]), provider, mode, city))
    return worst


def _priority_weight(cluster: Cluster) -> float:
    from models import PriorityLevel

    weight = {
        PriorityLevel.MUST_VISIT: 4,
        PriorityLevel.MUST_EAT: 4,
        PriorityLevel.HIGH_PRIORITY: 3,
        PriorityLevel.CANDIDATE_RESTAURANT: 2,
        PriorityLevel.OPTIONAL: 1,
        PriorityLevel.BACKUP_RESTAURANT: 0,
        PriorityLevel.REJECTABLE: 0,
    }
    return sum(weight.get(p.priority, 1) for p in cluster.places)
