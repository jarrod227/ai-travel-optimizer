"""Plan scoring.

Implements the "maximize / penalize" list from the spec as one transparent
weighted sum. Every component is stored in ``ScoreBreakdown.components`` so a
caller (or a test) can inspect *why* a plan scored the way it did rather than
just trusting a single number.

Rating policy (per spec): ratings influence quality but never dominate.
Differences smaller than ~0.2 stars are treated as roughly equivalent unless
review confidence is much higher, and both rating and review count matter.
Missing ratings must already have been normalized to a neutral value with low
confidence upstream (see planner.enrich_places) - this module just consumes
``place.rating`` / ``place.metadata_confidence`` as given.
"""

from __future__ import annotations

import datetime as dt

from models import NEAR_CLOSING_NOTE, DayPlan, Place, Plan, PriorityLevel, ScoreBreakdown, TripRequest

NEUTRAL_RATING = 3.5
RATING_EQUIVALENCE_BAND = 0.2
MAX_STOPS_PER_DAY_BEFORE_PENALTY = 5
CROSS_CITY_TRAVEL_MINUTES = 90

# Component weights. Grouped to mirror the spec's maximize/penalize lists.
WEIGHTS = {
    "must_visit_coverage": 30.0,
    "high_priority_coverage": 15.0,
    "restaurant_preference": 10.0,
    "poi_rating": 8.0,
    "review_confidence": 4.0,
    "geographic_coherence": 10.0,
    "meal_timing": 8.0,
    "reservation_feasibility": 5.0,
    "buffer_adequacy": 8.0,
    "explainability": 4.0,
    # penalties (already negative in WEIGHTS, applied to a 0..1 penalty magnitude)
    "travel_time_penalty": -8.0,
    "backtracking_penalty": -6.0,
    "rushed_meal_penalty": -6.0,
    "closing_time_risk_penalty": -5.0,
    "overpacking_penalty": -8.0,
    "low_priority_detour_penalty": -4.0,
    "missing_metadata_penalty": -3.0,
    "unresolved_place_penalty": -4.0,
    "crowd_risk_penalty": -3.0,
    "cross_city_penalty": -6.0,
}


def _bucketed_rating(rating: float) -> float:
    """Collapse differences smaller than the equivalence band."""

    return round(rating / RATING_EQUIVALENCE_BAND) * RATING_EQUIVALENCE_BAND


def rating_quality_score(place: Place) -> float:
    """0..1 score. Both rating and review count matter, but count only
    matters as a confidence multiplier - a 4.9 with 3 reviews should not
    outrank a 4.7 with 50,000 reviews by much."""

    rating = place.rating if place.rating is not None else NEUTRAL_RATING
    bucketed = _bucketed_rating(rating)
    base = max(0.0, min(1.0, (bucketed - 3.0) / 2.0))  # 3.0 stars -> 0, 5.0 stars -> 1
    count_confidence = 1.0
    if place.rating_count is not None:
        count_confidence = min(1.0, 0.3 + (place.rating_count / 5000.0))
    confidence = min(place.metadata_confidence, count_confidence)
    # Low confidence pulls the score halfway toward neutral, never to zero.
    return base * (0.5 + 0.5 * confidence)


def _all_scheduled_places(days: list[DayPlan]) -> list[Place]:
    return [s.place for d in days for s in d.stops if not s.is_meal or s.place.category.value in ("restaurant", "cafe")]


def _coverage(target: list[Place], scheduled_names: set[str]) -> float:
    if not target:
        return 1.0
    hit = sum(1 for p in target if p.name in scheduled_names)
    return hit / len(target)


def _meal_timing_score(days: list[DayPlan]) -> float:
    if not days:
        return 1.0
    scores = []
    for day in days:
        for stop in day.stops:
            if not stop.is_meal:
                continue
            # A meal scheduled with generous room before the venue's closing
            # time (tracked via stop.note, populated by planner) scores higher.
            scores.append(0.5 if NEAR_CLOSING_NOTE in stop.note else 1.0)
    return sum(scores) / len(scores) if scores else 1.0


def _buffer_score(days: list[DayPlan]) -> float:
    if not days:
        return 0.0
    ratios = [d.buffer_ratio for d in days]
    avg = sum(ratios) / len(ratios)
    # Ideal band is 15-25%; score falls off outside it in either direction.
    if 0.15 <= avg <= 0.25:
        return 1.0
    distance = min(abs(avg - 0.15), abs(avg - 0.25))
    return max(0.0, 1.0 - distance * 2.0)


def _overpacking_penalty(days: list[DayPlan]) -> float:
    if not days:
        return 0.0
    over = [max(0, len([s for s in d.stops if not s.is_meal]) - MAX_STOPS_PER_DAY_BEFORE_PENALTY) for d in days]
    return min(1.0, sum(over) / (len(days) * 2))


def _travel_time_penalty(days: list[DayPlan]) -> float:
    if not days:
        return 0.0
    minutes = [d.total_travel_minutes for d in days]
    avg = sum(minutes) / len(minutes)
    return min(1.0, avg / 180.0)  # 3+ hrs/day of pure transit maxes the penalty


def _cross_city_penalty(days: list[DayPlan]) -> float:
    if not days:
        return 0.0
    flagged = sum(1 for d in days if d.total_travel_minutes >= CROSS_CITY_TRAVEL_MINUTES)
    return min(1.0, flagged / len(days))


def _missing_metadata_penalty(scheduled: list[Place]) -> float:
    if not scheduled:
        return 0.0
    low_confidence = sum(1 for p in scheduled if p.metadata_confidence < 0.5)
    return min(1.0, low_confidence / len(scheduled))


def _crowd_risk_penalty(scheduled: list[Place]) -> float:
    if not scheduled:
        return 0.0
    at_risk = sum(1 for p in scheduled if "long_queue" in p.tags)
    return min(1.0, at_risk / len(scheduled))


def score_plan(plan: Plan, trip_request: TripRequest, unresolved_count: int = 0) -> ScoreBreakdown:
    scheduled = _all_scheduled_places(plan.days)
    scheduled_names = {p.name for p in scheduled}

    must_visit_cov = _coverage(trip_request.must_visit_places, scheduled_names)
    high_priority_cov = _coverage(trip_request.high_priority_places, scheduled_names)
    must_eat_cov = _coverage(trip_request.must_eat_places, scheduled_names)
    candidate_cov = _coverage(trip_request.candidate_restaurants, scheduled_names)
    restaurant_pref = (must_eat_cov + candidate_cov) / 2

    rating_scores = [rating_quality_score(p) for p in scheduled] or [0.0]
    review_confidences = [p.metadata_confidence for p in scheduled] or [0.0]

    components = {
        "must_visit_coverage": must_visit_cov,
        "high_priority_coverage": high_priority_cov,
        "restaurant_preference": restaurant_pref,
        "poi_rating": sum(rating_scores) / len(rating_scores),
        "review_confidence": sum(review_confidences) / len(review_confidences),
        "geographic_coherence": max(0.0, 1.0 - _travel_time_penalty(plan.days)),
        "meal_timing": _meal_timing_score(plan.days),
        "reservation_feasibility": _reservation_feasibility(scheduled),
        "buffer_adequacy": _buffer_score(plan.days),
        "explainability": 1.0 if plan.why_it_works else 0.5,
        "travel_time_penalty": _travel_time_penalty(plan.days),
        "backtracking_penalty": 0.0,  # populated by planner if it detects reversal patterns
        "rushed_meal_penalty": 1.0 - _meal_timing_score(plan.days),
        "closing_time_risk_penalty": 1.0 - _meal_timing_score(plan.days),
        "overpacking_penalty": _overpacking_penalty(plan.days),
        "low_priority_detour_penalty": 0.0,
        "missing_metadata_penalty": _missing_metadata_penalty(scheduled),
        "unresolved_place_penalty": min(1.0, unresolved_count / max(1, len(scheduled) + unresolved_count)),
        "crowd_risk_penalty": _crowd_risk_penalty(scheduled),
        "cross_city_penalty": _cross_city_penalty(plan.days),
    }

    total = sum(WEIGHTS[name] * value for name, value in components.items())
    explanation = [
        f"must-visit coverage {must_visit_cov:.0%}",
        f"high-priority coverage {high_priority_cov:.0%}",
        f"average buffer {sum(d.buffer_ratio for d in plan.days) / len(plan.days):.0%}" if plan.days else "no days",
    ]
    return ScoreBreakdown(total=total, components=components, explanation=explanation)


def _reservation_feasibility(scheduled: list[Place]) -> float:
    needing = [p for p in scheduled if p.reservation_required]
    if not needing:
        return 1.0
    known_slot = sum(1 for p in needing if p.reservation_slot is not None)
    return known_slot / len(needing)
