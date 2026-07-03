"""Core orchestration: raw input -> enriched places -> clusters -> day routes
-> scored multi-style plans.

The public entry point is :func:`plan_trip`. Everything else is exposed as
plain functions so tests (and an agent driving this skill) can exercise each
stage independently - e.g. call ``restaurant_feasible`` directly to explain
why one restaurant doesn't fit, without running the whole pipeline.

Design notes / assumptions (documented per project requirements):
  * Route ordering uses nearest-neighbor + a bounded 2-opt pass. Day-sized
    stop counts (< ~10) make this fast and close enough to optimal; this is
    not meant to be a general TSP solver.
  * Meals are inserted opportunistically into the attraction order once the
    walking clock enters the lunch/dinner window - see ``build_day_route``.
  * "Never reject must-visit/must-eat" is honored everywhere except a hard
    constraint failure (closed that day, after last entry, or a closing-time
    overrun with no earlier slot possible) - those are reported as explicit
    rejections with an explanation, per spec.
  * If the caller doesn't supply explicit dates, a synthetic Monday-free
    reference date is used so weekday-based closures don't collide by
    accident; this is clearly a placeholder and callers that care about real
    weekday closures should always pass ``start_date``.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from clustering import Cluster, assign_clusters_to_days, cluster_places_by_travel_time
from extract_places import extract_places_from_text, merge_duplicate_places, normalize_place_name
from models import (
    DayPlan,
    ItineraryStop,
    MovedPlace,
    Place,
    PlaceCategory,
    Plan,
    PlanningResult,
    PriorityLevel,
    RatingSource,
    RejectedPlace,
    TransportMode,
    TripRequest,
)
from providers import GeocodeResult, MapProvider, POIDataProvider
from scoring import NEUTRAL_RATING, score_plan

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DURATION_RANGES_MINUTES: dict[PlaceCategory, tuple[int, int]] = {
    PlaceCategory.MUSEUM: (120, 240),
    PlaceCategory.PARK: (180, 360),
    PlaceCategory.LANDMARK: (30, 90),
    PlaceCategory.OLD_STREET: (60, 180),
    PlaceCategory.TEMPLE: (45, 120),
    PlaceCategory.OBSERVATION_DECK: (45, 90),
    PlaceCategory.THEME_PARK: (360, 600),
    PlaceCategory.SHOPPING: (60, 180),
    PlaceCategory.RESTAURANT: (60, 90),
    PlaceCategory.CAFE: (45, 90),
    PlaceCategory.HOTEL: (0, 0),
    PlaceCategory.UNKNOWN: (60, 120),
}

LUNCH_WINDOW = (dt.time(11, 30), dt.time(13, 30))
LUNCH_HARD_CUTOFF = dt.time(14, 30)
LUNCH_EARLIEST_FORCED = dt.time(10, 0)  # used when a long attraction would otherwise blow through the window
DINNER_WINDOW = (dt.time(17, 30), dt.time(20, 0))
DINNER_HARD_CUTOFF = dt.time(21, 0)
DINNER_EARLIEST_FORCED = dt.time(16, 0)

STYLE_PARAMS: dict[str, dict] = {
    "balanced": {"buffer_ratio": 0.20, "safety_buffer_min": 20},
    "must_visit_first": {"buffer_ratio": 0.15, "safety_buffer_min": 20},
    "relaxed": {"buffer_ratio": 0.32, "safety_buffer_min": 30},
}

MAX_REASONABLE_RESTAURANT_MINUTES = 40.0
_REFERENCE_MONDAY_FREE_DATE = dt.date(2024, 1, 3)  # a Wednesday, used only as a placeholder


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def _time_to_minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _minutes_to_time(minutes: float) -> dt.time:
    total = int(round(minutes)) % (24 * 60)
    return dt.time(total // 60, total % 60)


def _add_minutes(t: dt.time, minutes: float) -> dt.time:
    return _minutes_to_time(_time_to_minutes(t) + minutes)


# --------------------------------------------------------------------------
# TripRequest construction from free text
# --------------------------------------------------------------------------

def build_trip_request_from_text(raw_text: str, **overrides) -> TripRequest:
    """Convenience path for callers that only have raw copied text. Places
    are heuristically routed into optional_places / candidate_restaurants;
    callers who know which places are must-visit/must-eat should instead
    build a ``TripRequest`` directly and only use ``extract_places_from_text``
    for the raw dump."""

    trip = TripRequest(**overrides)
    for place in extract_places_from_text(raw_text):
        if place.category in (PlaceCategory.RESTAURANT, PlaceCategory.CAFE):
            trip.candidate_restaurants.append(place)
        else:
            trip.optional_places.append(place)
    return trip


def apply_user_override(place: Place, trip_request: TripRequest) -> None:
    override = trip_request.user_overrides.get(normalize_place_name(place.name))
    if not override:
        return
    place.user_override.update(override)
    if "priority" in override:
        try:
            place.priority = PriorityLevel(override["priority"])
        except ValueError:
            pass
    if override.get("notes"):
        place.notes.append(override["notes"])
    if "reservation_required" in override:
        place.reservation_required = bool(override["reservation_required"])
    if override.get("reservation_slot"):
        place.reservation_slot = override["reservation_slot"]


# --------------------------------------------------------------------------
# Enrichment (geocoding, POI metadata, ratings, opening hours)
# --------------------------------------------------------------------------

def _resolve_coords(name: Optional[str], provider: MapProvider, city: str) -> Optional[tuple[float, float]]:
    if not name:
        return None
    result = provider.geocode(name, city)
    return (result.lat, result.lng) if result else None


def enrich_places(places: list[Place], provider: MapProvider, city: str) -> None:
    """Geocode, fetch POI metadata, and fill in a neutral low-confidence
    rating when none is available - mutates places in place. Every provider
    call for a given place name happens at most once per session."""

    poi_provider = provider if isinstance(provider, POIDataProvider) else None

    for place in places:
        lookup_name = place.name_en or place.name_cn or place.name

        if not place.is_geocoded:
            geocode_result: Optional[GeocodeResult] = provider.geocode(lookup_name, city)
            if geocode_result is None and poi_provider is not None:
                pois = poi_provider.search_poi(lookup_name, city)
                if pois:
                    geocode_result = GeocodeResult(pois[0].lat, pois[0].lng, pois[0].address or "")
            if geocode_result is not None:
                place.lat, place.lng = geocode_result.lat, geocode_result.lng
                place.address = place.address or geocode_result.formatted_address
            else:
                place.metadata_confidence = min(place.metadata_confidence, 0.3)

        if poi_provider is not None:
            pois = poi_provider.search_poi(lookup_name, city)
            if pois:
                details = pois[0]
                place.rating = place.rating if place.rating is not None else details.rating
                place.rating_count = place.rating_count if place.rating_count is not None else details.rating_count
                if details.rating is not None:
                    place.rating_source = details.rating_source
                place.avg_cost_rmb = place.avg_cost_rmb if place.avg_cost_rmb is not None else details.avg_cost_rmb

        if place.opening_hours is None and hasattr(provider, "opening_hours_for"):
            place.opening_hours = provider.opening_hours_for(lookup_name)  # type: ignore[attr-defined]

        if place.rating is None:
            place.rating = NEUTRAL_RATING
            place.rating_source = RatingSource.UNKNOWN
            place.metadata_confidence = min(place.metadata_confidence, 0.3)


def estimate_duration_minutes(place: Place, trip_request: TripRequest) -> int:
    override = place.user_override.get("expected_duration_minutes")
    if override:
        return int(override)

    lo, hi = DURATION_RANGES_MINUTES.get(place.category, DURATION_RANGES_MINUTES[PlaceCategory.UNKNOWN])
    base = (lo + hi) / 2

    if "long_queue" in place.tags:
        base *= 1.2
    if any("relax" in g.lower() for g in trip_request.user_goals):
        base *= 1.15
    if place.reservation_required:
        base += 15  # check-in / ticket process overhead

    return round(base)


# --------------------------------------------------------------------------
# Feasibility rules
# --------------------------------------------------------------------------

def restaurant_feasible(
    place: Place, date: dt.date, arrival_time: dt.time, dining_minutes: float, safety_buffer_minutes: int = 20,
) -> tuple[bool, str]:
    """A restaurant is feasible only if arrival + dining + safety buffer
    fits before closing - being merely "open at arrival" is not enough."""

    if place.opening_hours is None:
        return True, ""
    if not place.opening_hours.is_open_on(date):
        return False, f"{place.name} is closed on {date.strftime('%A')}s"
    closing = place.opening_hours.closing_time_on(date)
    assert closing is not None
    end_minutes = _time_to_minutes(arrival_time) + dining_minutes + safety_buffer_minutes
    if end_minutes <= _time_to_minutes(closing):
        return True, ""
    return False, (
        f"arrival at {arrival_time.strftime('%H:%M')} + {int(dining_minutes)}min dining + "
        f"{safety_buffer_minutes}min safety buffer would end after closing time {closing.strftime('%H:%M')}"
    )


def attraction_feasible(place: Place, date: dt.date, arrival_time: dt.time) -> tuple[bool, str]:
    if place.opening_hours is not None:
        if not place.opening_hours.is_open_on(date):
            return False, f"{place.name} is closed on {date.strftime('%A')}s"
        last_entry = place.opening_hours.last_entry_time_on(date)
        if last_entry is not None and _time_to_minutes(arrival_time) > _time_to_minutes(last_entry):
            return False, (
                f"arrival at {arrival_time.strftime('%H:%M')} is after the last-entry time "
                f"{last_entry.strftime('%H:%M')}"
            )
    if place.reservation_required and place.reservation_slot is None:
        return False, f"{place.name} requires a reservation and no confirmed time slot is known"
    return True, ""


# --------------------------------------------------------------------------
# Route ordering (nearest neighbor + 2-opt)
# --------------------------------------------------------------------------

def _route_length(
    start_coords: tuple[float, float], order: list[Place], provider: MapProvider, mode: TransportMode, city: str,
) -> float:
    total = 0.0
    current = start_coords
    for place in order:
        total += provider.travel_time(current, (place.lat, place.lng), mode, city).duration_minutes
        current = (place.lat, place.lng)
    return total


def _order_stops(
    start_coords: tuple[float, float], places: list[Place], provider: MapProvider, mode: TransportMode, city: str,
) -> list[Place]:
    remaining = list(places)
    order: list[Place] = []
    current = start_coords
    while remaining:
        nxt = min(remaining, key=lambda p: provider.travel_time(current, (p.lat, p.lng), mode, city).duration_minutes)
        order.append(nxt)
        current = (nxt.lat, nxt.lng)
        remaining.remove(nxt)

    n = len(order)
    if n < 3:
        return order
    improved = True
    best, best_len = order, _route_length(start_coords, order, provider, mode, city)
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                candidate = best[:i] + list(reversed(best[i : j + 1])) + best[j + 1 :]
                cand_len = _route_length(start_coords, candidate, provider, mode, city)
                if cand_len < best_len - 1e-6:
                    best, best_len = candidate, cand_len
                    improved = True
    return best


# --------------------------------------------------------------------------
# Fitting attractions to the daily time budget
# --------------------------------------------------------------------------

_DROPPABLE_RANK = {
    PriorityLevel.REJECTABLE: 0,
    PriorityLevel.OPTIONAL: 1,
    PriorityLevel.HIGH_PRIORITY: 2,
}


def _estimate_route_minutes(
    start_coords: tuple[float, float], places: list[Place], provider: MapProvider, mode: TransportMode, city: str,
) -> float:
    order = _order_stops(start_coords, places, provider, mode, city)
    travel = _route_length(start_coords, order, provider, mode, city)
    duration = sum(p.expected_duration_minutes or 60 for p in places)
    return travel + duration


def fit_attractions_to_budget(
    attractions: list[Place],
    day_span_minutes: float,
    buffer_ratio: float,
    provider: MapProvider,
    mode: TransportMode,
    city: str,
    start_coords: tuple[float, float],
    meal_reserve_minutes: float = 150.0,
) -> tuple[list[Place], list[RejectedPlace]]:
    """Drop the lowest-priority, longest stops until the day's estimated
    active time (travel + durations) fits inside the target buffer. Never
    drops MUST_VISIT or MUST_EAT."""

    available = max(60.0, day_span_minutes * (1 - buffer_ratio) - meal_reserve_minutes)
    current = list(attractions)
    rejected: list[RejectedPlace] = []

    while current:
        needed = _estimate_route_minutes(start_coords, current, provider, mode, city)
        if needed <= available:
            break
        droppable = [p for p in current if p.priority in _DROPPABLE_RANK]
        if not droppable:
            break
        droppable.sort(key=lambda p: (_DROPPABLE_RANK[p.priority], -(p.expected_duration_minutes or 0)))
        worst = droppable[0]
        current.remove(worst)
        rejected.append(
            RejectedPlace(
                worst,
                reason=(
                    f"Day schedule is already full; keeping '{worst.name}' would force removal of "
                    "higher-priority stops or exceed the schedule buffer."
                ),
                failed_constraint="daily_time_budget",
                could_fit_another_day=True,
                alternative_suggestion="Consider a future day, a shorter visit, or swap with a lower-effort stop.",
            )
        )
    return current, rejected


# --------------------------------------------------------------------------
# Restaurant-to-day assignment
# --------------------------------------------------------------------------

def _group_centroid(
    places: list[Place], hotel_coords: Optional[tuple[float, float]]
) -> Optional[tuple[float, float]]:
    coords = [(p.lat, p.lng) for p in places if p.is_geocoded]
    if not coords:
        return hotel_coords
    return (sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords))


def assign_restaurants_to_days(
    restaurants: list[Place],
    day_attraction_groups: list[list[Place]],
    day_dates: list[dt.date],
    provider: MapProvider,
    mode: TransportMode,
    city: str,
    hotel_coords: Optional[tuple[float, float]],
) -> tuple[list[list[Place]], list[MovedPlace], list[RejectedPlace]]:
    """Assign each restaurant to whichever day it's geographically closest to
    AND open on. If the closest day fails (too far or closed that day) but a
    farther-but-still-reasonable day works, the restaurant is moved there
    instead of being rejected outright."""

    centroids = [_group_centroid(g, hotel_coords) for g in day_attraction_groups]
    day_restaurants: list[list[Place]] = [[] for _ in day_attraction_groups]
    moved: list[MovedPlace] = []
    rejected: list[RejectedPlace] = []

    for restaurant in restaurants:
        r_coords = (restaurant.lat, restaurant.lng)
        ranked = sorted(
            range(len(day_attraction_groups)),
            key=lambda i: (
                provider.travel_time(centroids[i], r_coords, mode, city).duration_minutes
                if centroids[i] is not None
                else float("inf")
            ),
        )
        if not ranked:
            continue

        first_choice = ranked[0]
        chosen: Optional[int] = None
        for i in ranked:
            centroid = centroids[i]
            minutes = (
                provider.travel_time(centroid, r_coords, mode, city).duration_minutes
                if centroid is not None
                else float("inf")
            )
            if minutes > MAX_REASONABLE_RESTAURANT_MINUTES:
                break
            if restaurant.opening_hours is not None and not restaurant.opening_hours.is_open_on(day_dates[i]):
                continue
            chosen = i
            break

        if chosen is None:
            if restaurant.priority == PriorityLevel.MUST_EAT:
                chosen = first_choice
            else:
                nearest_minutes = provider.travel_time(
                    centroids[first_choice], r_coords, mode, city
                ).duration_minutes if centroids[first_choice] is not None else float("inf")
                rejected.append(
                    RejectedPlace(
                        restaurant,
                        reason=(
                            f"{restaurant.name} is too far from every day's route "
                            f"(closest day is about {nearest_minutes:.0f} min away) or closed whenever it's near."
                        ),
                        failed_constraint="geographic_or_hours_fit",
                        could_fit_another_day=False,
                        alternative_suggestion="Use one of the backup restaurants near the route instead.",
                    )
                )
                continue

        if chosen != first_choice:
            moved.append(
                MovedPlace(
                    restaurant,
                    from_day=first_choice,
                    to_day=chosen,
                    reason=(
                        f"{restaurant.name} is geographically closest to Day {first_choice + 1}, but doesn't fit "
                        f"that day (too far or closed) - moved to Day {chosen + 1}, where it fits."
                    ),
                )
            )
        day_restaurants[chosen].append(restaurant)

    return day_restaurants, moved, rejected


# --------------------------------------------------------------------------
# Day route construction
# --------------------------------------------------------------------------

def _restaurant_rank(place: Place) -> tuple[int, float]:
    tier = 0 if place.priority == PriorityLevel.MUST_EAT else 1
    return (tier, -(place.rating or 0.0))


def _resolve_date(trip_request: TripRequest, day_index: int) -> dt.date:
    if trip_request.start_date:
        return trip_request.start_date + dt.timedelta(days=day_index)
    return _REFERENCE_MONDAY_FREE_DATE + dt.timedelta(days=day_index)


def _theme_for(attractions: list[Place], day_index: int) -> str:
    if not attractions:
        return f"Day {day_index + 1}: Flex / rest day"
    ranked = sorted(attractions, key=lambda p: (p.priority != PriorityLevel.MUST_VISIT, p.priority != PriorityLevel.HIGH_PRIORITY))
    names = " & ".join(p.name for p in ranked[:2])
    return f"Day {day_index + 1}: {names}"


def build_day_route(
    day_index: int,
    date: dt.date,
    attractions: list[Place],
    restaurants: list[Place],
    trip_request: TripRequest,
    provider: MapProvider,
    style: str,
) -> tuple[DayPlan, list[RejectedPlace]]:
    params = STYLE_PARAMS[style]
    mode = trip_request.transport_mode
    city = trip_request.destination

    start_coords = _resolve_coords(trip_request.hotel or trip_request.start_location, provider, city)
    end_coords = _resolve_coords(trip_request.end_location, provider, city) or start_coords
    if start_coords is None:
        anchor = _group_centroid(attractions or restaurants, None)
        start_coords = anchor or (0.0, 0.0)
    if end_coords is None:
        end_coords = start_coords

    day_span_minutes = _time_to_minutes(trip_request.daily_end_time) - _time_to_minutes(trip_request.daily_start_time)

    trimmed_attractions, rejected = fit_attractions_to_budget(
        attractions, day_span_minutes, params["buffer_ratio"], provider, mode, city, start_coords,
    )
    ordered_attractions = _order_stops(start_coords, trimmed_attractions, provider, mode, city)

    stops: list[ItineraryStop] = []
    reminders: list[str] = []
    meal_pool = sorted(restaurants, key=_restaurant_rank)
    lunch_done = dinner_done = False

    current_time = trip_request.daily_start_time
    current_coords = start_coords

    def try_insert_meal(window_start: dt.time, hard_cutoff: dt.time, label: str) -> bool:
        nonlocal current_time, current_coords, lunch_done, dinner_done
        if _time_to_minutes(current_time) < _time_to_minutes(window_start):
            return False
        if _time_to_minutes(current_time) > _time_to_minutes(hard_cutoff):
            return False
        for candidate in list(meal_pool):
            travel_est = provider.travel_time(current_coords, (candidate.lat, candidate.lng), mode, city)
            arrival = _add_minutes(current_time, travel_est.duration_minutes)
            dining_minutes = candidate.expected_duration_minutes or 75
            feasible, _ = restaurant_feasible(candidate, date, arrival, dining_minutes, params["safety_buffer_min"])
            if not feasible:
                continue
            closing = candidate.opening_hours.closing_time_on(date) if candidate.opening_hours else None
            near_closing = ""
            if closing is not None:
                slack = _time_to_minutes(closing) - (_time_to_minutes(arrival) + dining_minutes)
                if slack < 30:
                    near_closing = "arrival is close to closing time"
            departure = _add_minutes(arrival, dining_minutes)
            stops.append(
                ItineraryStop(candidate, arrival, departure, round(travel_est.duration_minutes), mode,
                              is_meal=True, note=near_closing)
            )
            current_time, current_coords = departure, (candidate.lat, candidate.lng)
            meal_pool.remove(candidate)
            if label == "lunch":
                lunch_done = True
            else:
                dinner_done = True
            return True
        return False

    for place in ordered_attractions:
        # Look ahead: if this attraction is long enough to carry us straight
        # through a meal window (e.g. a 4-hour park visit spanning lunch),
        # try to grab that meal now rather than losing it entirely.
        lookahead_travel = provider.travel_time(current_coords, (place.lat, place.lng), mode, city).duration_minutes
        arrival_at_place = _add_minutes(current_time, lookahead_travel)
        lookahead_departure = _add_minutes(arrival_at_place, place.expected_duration_minutes or 60)
        lunch_would_be_skipped = not lunch_done and _time_to_minutes(lookahead_departure) > _time_to_minutes(LUNCH_HARD_CUTOFF)
        dinner_would_be_skipped = not dinner_done and _time_to_minutes(lookahead_departure) > _time_to_minutes(DINNER_HARD_CUTOFF)

        if not lunch_done:
            try_insert_meal(LUNCH_WINDOW[0], LUNCH_HARD_CUTOFF, "lunch")
        if not dinner_done:
            try_insert_meal(DINNER_WINDOW[0], DINNER_HARD_CUTOFF, "dinner")

        if (lunch_would_be_skipped and not lunch_done) or (dinner_would_be_skipped and not dinner_done):
            # Too early to eat from here, but this attraction would carry us
            # straight through the window - jump to its neighborhood first
            # (as if we walked straight there) and look for a meal nearby
            # before actually starting the visit.
            current_time, current_coords = arrival_at_place, (place.lat, place.lng)
            if lunch_would_be_skipped and not lunch_done:
                try_insert_meal(LUNCH_EARLIEST_FORCED, LUNCH_HARD_CUTOFF, "lunch")
            if dinner_would_be_skipped and not dinner_done:
                try_insert_meal(DINNER_EARLIEST_FORCED, DINNER_HARD_CUTOFF, "dinner")

        travel_est = provider.travel_time(current_coords, (place.lat, place.lng), mode, city)
        arrival = _add_minutes(current_time, travel_est.duration_minutes)
        feasible, reason = attraction_feasible(place, date, arrival)
        if not feasible:
            if place.priority == PriorityLevel.MUST_VISIT:
                reminders.append(
                    f"Risk: {place.name} - {reason}. Consider an earlier start or moving it to another day."
                )
            else:
                rejected.append(
                    RejectedPlace(
                        place, reason=reason, failed_constraint="opening_hours_or_entry",
                        could_fit_another_day=True, alternative_suggestion="Try an earlier time slot or another day.",
                    )
                )
                continue

        duration = place.expected_duration_minutes or 60
        departure = _add_minutes(arrival, duration)
        stops.append(ItineraryStop(place, arrival, departure, round(travel_est.duration_minutes), mode))
        current_time, current_coords = departure, (place.lat, place.lng)

    if not lunch_done:
        try_insert_meal(LUNCH_WINDOW[0], LUNCH_HARD_CUTOFF, "lunch")
    if not dinner_done:
        try_insert_meal(DINNER_WINDOW[0], DINNER_HARD_CUTOFF, "dinner")

    return_travel = provider.travel_time(current_coords, end_coords, mode, city).duration_minutes if stops else 0.0
    total_travel = sum(s.travel_minutes_from_prev for s in stops) + round(return_travel)

    if stops:
        end_time = _add_minutes(stops[-1].departure, return_travel)
    else:
        end_time = current_time

    scheduled_minutes = max(0, _time_to_minutes(end_time) - _time_to_minutes(trip_request.daily_start_time))
    buffer_ratio = max(0.0, (day_span_minutes - scheduled_minutes) / day_span_minutes) if day_span_minutes else 0.0

    if not lunch_done:
        reminders.append("No feasible lunch stop found in the target window (11:30-13:30); add a flexible option.")
    if not dinner_done:
        reminders.append("No feasible dinner stop found in the target window (17:30-20:00); add a flexible option.")

    leftover_must_eat = [p for p in meal_pool if p.priority == PriorityLevel.MUST_EAT]
    for place in leftover_must_eat:
        rejected.append(
            RejectedPlace(
                place,
                reason=_explain_restaurant_infeasibility(place, date, params["safety_buffer_min"]),
                failed_constraint="closing_time",
                could_fit_another_day=True,
                alternative_suggestion=_backup_suggestion(trip_request),
            )
        )

    day_plan = DayPlan(
        day_index=day_index, date=date, theme=_theme_for(trimmed_attractions, day_index), stops=stops,
        total_travel_minutes=total_travel, buffer_ratio=buffer_ratio, reminders=reminders,
    )
    return day_plan, rejected


def _explain_restaurant_infeasibility(place: Place, date: dt.date, safety_buffer_minutes: int) -> str:
    if place.opening_hours is None:
        return f"{place.name} could not be fit into this day's schedule."
    if not place.opening_hours.is_open_on(date):
        return f"{place.name} is closed on {date.strftime('%A')}s, which conflicts with this day's schedule."
    closing = place.opening_hours.closing_time_on(date)
    dining = place.expected_duration_minutes or 75
    return (
        f"{place.name} closes at {closing.strftime('%H:%M') if closing else 'unknown time'}; even an early arrival "
        f"plus {dining}min dining and a {safety_buffer_minutes}min safety buffer does not fit before closing given "
        "this day's route."
    )


def _backup_suggestion(trip_request: TripRequest) -> str:
    if trip_request.backup_restaurants:
        return f"Consider backup option: {trip_request.backup_restaurants[0].name}."
    return "No backup restaurant was provided - consider adjusting the route to arrive earlier."


# --------------------------------------------------------------------------
# Plan assembly (3 styles) + scoring
# --------------------------------------------------------------------------

def _why_it_works(style: str, plan: Plan) -> list[str]:
    lines = []
    total_stops = sum(len([s for s in d.stops if not s.is_meal]) for d in plan.days)
    lines.append(f"Covers {total_stops} attraction stop(s) across {len(plan.days)} day(s).")
    if style == "balanced":
        lines.append("Balances attractions, food, and travel time for an efficient but unhurried trip.")
    elif style == "must_visit_first":
        lines.append("Anchors every day around your must-visit and must-eat picks first.")
    elif style == "relaxed":
        lines.append("Keeps a larger schedule buffer each day to reduce the risk of delays.")
    if plan.days:
        avg_buffer = sum(d.buffer_ratio for d in plan.days) / len(plan.days)
        lines.append(f"Average schedule buffer: {avg_buffer:.0%}.")
    return lines


def _risks(plan: Plan, rejected: list[RejectedPlace]) -> list[str]:
    risks = [r for d in plan.days for r in d.reminders]
    if rejected:
        risks.append(f"{len(rejected)} place(s) did not fit this plan's schedule (see rejected list).")
    return risks


def _build_plan(
    style: str,
    title: str,
    trip_request: TripRequest,
    day_attraction_groups: list[list[Place]],
    day_restaurant_groups: list[list[Place]],
    day_dates: list[dt.date],
    provider: MapProvider,
) -> tuple[Plan, list[RejectedPlace]]:
    days: list[DayPlan] = []
    rejected_total: list[RejectedPlace] = []
    for i, (attractions, restaurants) in enumerate(zip(day_attraction_groups, day_restaurant_groups)):
        day_plan, rejected = build_day_route(i, day_dates[i], attractions, restaurants, trip_request, provider, style)
        days.append(day_plan)
        rejected_total.extend(rejected)

    plan = Plan(style=style, title=title, days=days)
    plan.why_it_works = _why_it_works(style, plan)
    plan.risks = _risks(plan, rejected_total)
    plan.score = score_plan(plan, trip_request)
    return plan, rejected_total


def plan_trip(raw_input: TripRequest | str, provider: Optional[MapProvider] = None) -> PlanningResult:
    """Main entry point: normalize input, enrich, cluster, build routes for
    three plan styles, and package rejected/moved/reminder metadata."""

    from mock_maps import MockProvider  # local import: keeps mock_maps optional for AMap-only deployments

    trip_request = raw_input if isinstance(raw_input, TripRequest) else build_trip_request_from_text(raw_input)
    provider = provider or MockProvider()
    city = trip_request.destination

    attraction_pool = merge_duplicate_places(
        [*trip_request.must_visit_places, *trip_request.high_priority_places, *trip_request.optional_places]
    )
    restaurant_pool = merge_duplicate_places(
        [*trip_request.must_eat_places, *trip_request.candidate_restaurants]
    )
    backups = list(trip_request.backup_restaurants)

    for place in [*attraction_pool, *restaurant_pool, *backups]:
        apply_user_override(place, trip_request)

    enrich_places([*attraction_pool, *restaurant_pool, *backups], provider, city)

    for place in [*attraction_pool, *restaurant_pool, *backups]:
        place.expected_duration_minutes = estimate_duration_minutes(place, trip_request)

    hotel_coords = _resolve_coords(trip_request.hotel or trip_request.start_location, provider, city)

    unresolved = [p for p in [*attraction_pool, *restaurant_pool] if not p.is_geocoded]
    geocoded_attractions = [p for p in attraction_pool if p.is_geocoded]
    geocoded_restaurants = [p for p in restaurant_pool if p.is_geocoded]

    number_of_days = max(1, trip_request.effective_days())
    day_dates = [_resolve_date(trip_request, i) for i in range(number_of_days)]

    clusters: list[Cluster] = cluster_places_by_travel_time(
        geocoded_attractions, provider, trip_request.transport_mode, city
    )
    day_attraction_groups = assign_clusters_to_days(
        clusters, number_of_days, hotel_coords, provider, trip_request.transport_mode, city
    )

    day_restaurant_groups, moved, restaurant_rejected = assign_restaurants_to_days(
        geocoded_restaurants, day_attraction_groups, day_dates, provider, trip_request.transport_mode, city,
        hotel_coords,
    )

    balanced_plan, balanced_rejected = _build_plan(
        "balanced", "Best Balanced Plan", trip_request, day_attraction_groups, day_restaurant_groups, day_dates,
        provider,
    )
    must_visit_plan, _ = _build_plan(
        "must_visit_first", "Must-Visit First Plan", trip_request, day_attraction_groups, day_restaurant_groups,
        day_dates, provider,
    )
    relaxed_plan, _ = _build_plan(
        "relaxed", "Relaxed / Low-Risk Plan", trip_request, day_attraction_groups, day_restaurant_groups, day_dates,
        provider,
    )

    all_places = [*attraction_pool, *restaurant_pool, *backups]
    low_confidence = [p for p in all_places if p.metadata_confidence < 0.5]
    reservation_reminders = [
        f"{p.name} requires a reservation"
        + (f" (slot: {p.reservation_slot.strftime('%H:%M')})" if p.reservation_slot else " - confirm availability before your trip")
        for p in all_places
        if p.reservation_required
    ]

    rejected_places = [
        *balanced_rejected,
        *restaurant_rejected,
        *(
            RejectedPlace(
                p, reason="Location could not be resolved to coordinates.", failed_constraint="geocoding",
                could_fit_another_day=False, alternative_suggestion="Provide an address or confirm the place name.",
            )
            for p in unresolved
        ),
    ]

    return PlanningResult(
        trip_request=trip_request,
        plans=[balanced_plan, must_visit_plan, relaxed_plan],
        rejected_places=rejected_places,
        moved_places=moved,
        reservation_reminders=reservation_reminders,
        low_confidence_places=low_confidence,
        backup_options=backups,
    )
