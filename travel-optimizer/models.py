"""Core data models for the travel-optimizer skill.

Everything downstream (extraction, clustering, planning, scoring) speaks
these types. Keep this module free of provider/network logic so it can be
imported by tests and lightweight tools without side effects.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class TransportMode(StrEnum):
    WALKING = "walking"
    DRIVING = "driving"
    TAXI = "taxi"
    SUBWAY = "subway"
    TRANSIT = "transit"
    MIXED = "mixed"


class PlaceCategory(StrEnum):
    """Granular categories drive default-duration estimates (see planner.py)."""

    MUSEUM = "museum"
    PARK = "park"
    LANDMARK = "landmark"
    OLD_STREET = "old_street"
    TEMPLE = "temple"
    OBSERVATION_DECK = "observation_deck"
    THEME_PARK = "theme_park"
    SHOPPING = "shopping"
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    HOTEL = "hotel"
    UNKNOWN = "unknown"


class PriorityLevel(StrEnum):
    MUST_VISIT = "must_visit"
    HIGH_PRIORITY = "high_priority"
    OPTIONAL = "optional"
    REJECTABLE = "rejectable"
    MUST_EAT = "must_eat"
    CANDIDATE_RESTAURANT = "candidate_restaurant"
    BACKUP_RESTAURANT = "backup_restaurant"


class RatingSource(StrEnum):
    AMAP = "amap"
    GOOGLE = "google"
    DIANPING = "dianping"
    MEITUAN = "meituan"
    USER = "user"
    MOCK = "mock"
    UNKNOWN = "unknown"


# Marker string shared between planner (writes it into ItineraryStop.note)
# and scoring (reads it to penalize meals scheduled too close to closing).
NEAR_CLOSING_NOTE = "arrival is close to closing time"

# Notes/tags extracted from free text (Xiaohongshu-style copy, user remarks).
NOTE_TAGS = {
    "reservation_required",
    "morning_only",
    "sunset",
    "closed_mondays",
    "long_queue",
    "far_from_center",
    "must_try",
    "avoid_weekend",
}


# --------------------------------------------------------------------------
# Opening hours
# --------------------------------------------------------------------------

@dataclass
class DayHours:
    """Open/close time for a single weekday. ``None`` open/close means closed."""

    open_time: Optional[dt.time] = None
    close_time: Optional[dt.time] = None

    @property
    def is_closed(self) -> bool:
        return self.open_time is None or self.close_time is None


@dataclass
class OpeningHours:
    """Weekly schedule keyed by Python weekday index (Monday=0 ... Sunday=6).

    A missing entry for a weekday is treated as "open all day" only if
    ``default_open`` is True, otherwise as unknown (handled by caller with a
    low-confidence assumption). ``last_entry_offset_minutes`` models venues
    that stop admitting guests before the official closing time.
    """

    by_weekday: dict[int, DayHours] = field(default_factory=dict)
    last_entry_offset_minutes: int = 0
    default_open: bool = True

    def hours_on(self, date: dt.date) -> Optional[DayHours]:
        if date.weekday() in self.by_weekday:
            return self.by_weekday[date.weekday()]
        if self.default_open:
            return DayHours(dt.time(9, 0), dt.time(21, 0))
        return None

    def is_open_on(self, date: dt.date) -> bool:
        hours = self.hours_on(date)
        return hours is not None and not hours.is_closed

    def closing_time_on(self, date: dt.date) -> Optional[dt.time]:
        hours = self.hours_on(date)
        if hours is None or hours.is_closed:
            return None
        return hours.close_time

    def last_entry_time_on(self, date: dt.date) -> Optional[dt.time]:
        closing = self.closing_time_on(date)
        if closing is None:
            return None
        minutes = closing.hour * 60 + closing.minute - self.last_entry_offset_minutes
        minutes = max(minutes, 0)
        return dt.time(minutes // 60, minutes % 60)


# --------------------------------------------------------------------------
# Place
# --------------------------------------------------------------------------

@dataclass
class Place:
    """A single candidate location (attraction, restaurant, cafe, shop, ...)."""

    id: str
    name: str
    category: PlaceCategory = PlaceCategory.UNKNOWN
    priority: PriorityLevel = PriorityLevel.OPTIONAL

    name_cn: Optional[str] = None
    name_en: Optional[str] = None

    lat: Optional[float] = None
    lng: Optional[float] = None
    address: Optional[str] = None
    city: Optional[str] = None

    notes: list[str] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)

    opening_hours: Optional[OpeningHours] = None
    expected_duration_minutes: Optional[int] = None

    rating: Optional[float] = None
    rating_count: Optional[int] = None
    rating_source: RatingSource = RatingSource.UNKNOWN
    metadata_confidence: float = 1.0

    avg_cost_rmb: Optional[float] = None
    reservation_required: bool = False
    reservation_slot: Optional[dt.time] = None

    source_text: Optional[str] = None
    user_override: dict = field(default_factory=dict)

    @property
    def is_geocoded(self) -> bool:
        return self.lat is not None and self.lng is not None

    @property
    def coordinates(self) -> Optional[tuple[float, float]]:
        if self.is_geocoded:
            return (self.lat, self.lng)  # type: ignore[return-value]
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "priority": self.priority.value,
            "lat": self.lat,
            "lng": self.lng,
            "address": self.address,
            "notes": self.notes,
            "tags": sorted(self.tags),
            "rating": self.rating,
            "rating_source": self.rating_source.value,
            "avg_cost_rmb": self.avg_cost_rmb,
            "reservation_required": self.reservation_required,
            "expected_duration_minutes": self.expected_duration_minutes,
        }


# --------------------------------------------------------------------------
# Trip request (normalized user input)
# --------------------------------------------------------------------------

@dataclass
class TripRequest:
    """Normalized representation of everything the user told us."""

    destination: str = "Beijing"

    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    number_of_days: int = 1

    hotel: Optional[str] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None

    daily_start_time: dt.time = dt.time(9, 0)
    daily_end_time: dt.time = dt.time(21, 0)

    transport_mode: TransportMode = TransportMode.MIXED

    user_goals: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    hard_constraints: list[str] = field(default_factory=list)
    soft_constraints: list[str] = field(default_factory=list)

    must_visit_places: list[Place] = field(default_factory=list)
    high_priority_places: list[Place] = field(default_factory=list)
    optional_places: list[Place] = field(default_factory=list)

    must_eat_places: list[Place] = field(default_factory=list)
    candidate_restaurants: list[Place] = field(default_factory=list)
    backup_restaurants: list[Place] = field(default_factory=list)

    # place name (normalized) -> override dict, e.g.
    # {"expected_duration_minutes": 90, "priority": "must_visit", "notes": "...",
    #  "reservation_required": True}
    user_overrides: dict[str, dict] = field(default_factory=dict)

    def all_places(self) -> list[Place]:
        return [
            *self.must_visit_places,
            *self.high_priority_places,
            *self.optional_places,
            *self.must_eat_places,
            *self.candidate_restaurants,
            *self.backup_restaurants,
        ]

    def effective_days(self) -> int:
        if self.start_date and self.end_date:
            return (self.end_date - self.start_date).days + 1
        return self.number_of_days


# --------------------------------------------------------------------------
# Itinerary / plan outputs
# --------------------------------------------------------------------------

@dataclass
class ItineraryStop:
    place: Place
    arrival: dt.time
    departure: dt.time
    travel_minutes_from_prev: int
    transport_mode_used: TransportMode
    is_meal: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "place": self.place.name,
            "category": self.place.category.value,
            "arrival": self.arrival.strftime("%H:%M"),
            "departure": self.departure.strftime("%H:%M"),
            "travel_minutes_from_prev": self.travel_minutes_from_prev,
            "transport_mode": self.transport_mode_used.value,
            "is_meal": self.is_meal,
            "note": self.note,
        }


@dataclass
class RejectedPlace:
    place: Place
    reason: str
    failed_constraint: str
    could_fit_another_day: bool = False
    alternative_suggestion: Optional[str] = None


@dataclass
class MovedPlace:
    place: Place
    from_day: Optional[int]
    to_day: int
    reason: str


@dataclass
class DayPlan:
    day_index: int
    date: Optional[dt.date]
    theme: str
    stops: list[ItineraryStop] = field(default_factory=list)
    total_travel_minutes: int = 0
    buffer_ratio: float = 0.0
    reminders: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "day_index": self.day_index,
            "date": self.date.isoformat() if self.date else None,
            "theme": self.theme,
            "stops": [s.to_dict() for s in self.stops],
            "total_travel_minutes": self.total_travel_minutes,
            "buffer_ratio": round(self.buffer_ratio, 2),
            "reminders": self.reminders,
        }


@dataclass
class ScoreBreakdown:
    total: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    explanation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 2),
            "components": {k: round(v, 2) for k, v in self.components.items()},
            "explanation": self.explanation,
        }


@dataclass
class Plan:
    style: str  # "balanced" | "must_visit_first" | "relaxed"
    title: str
    days: list[DayPlan] = field(default_factory=list)
    score: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    why_it_works: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "style": self.style,
            "title": self.title,
            "days": [d.to_dict() for d in self.days],
            "score": self.score.to_dict(),
            "why_it_works": self.why_it_works,
            "risks": self.risks,
        }


@dataclass
class PlanningResult:
    trip_request: TripRequest
    plans: list[Plan]
    rejected_places: list[RejectedPlace] = field(default_factory=list)
    moved_places: list[MovedPlace] = field(default_factory=list)
    reservation_reminders: list[str] = field(default_factory=list)
    low_confidence_places: list[Place] = field(default_factory=list)
    backup_options: list[Place] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "destination": self.trip_request.destination,
            "plans": [p.to_dict() for p in self.plans],
            "rejected_places": [
                {
                    "place": r.place.name,
                    "reason": r.reason,
                    "failed_constraint": r.failed_constraint,
                    "could_fit_another_day": r.could_fit_another_day,
                    "alternative_suggestion": r.alternative_suggestion,
                }
                for r in self.rejected_places
            ],
            "moved_places": [
                {
                    "place": m.place.name,
                    "from_day": m.from_day,
                    "to_day": m.to_day,
                    "reason": m.reason,
                }
                for m in self.moved_places
            ],
            "reservation_reminders": self.reservation_reminders,
            "low_confidence_places": [p.name for p in self.low_confidence_places],
            "backup_options": [p.name for p in self.backup_options],
        }
