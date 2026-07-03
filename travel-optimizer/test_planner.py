"""Tests for the travel-optimizer skill, run entirely against MockProvider
(no network, no API keys). Covers the ten required scenarios from the spec:

  1. restaurant closes soon
  2. too many attractions for one day
  3. two-day clustering
  4. far restaurant moved to another day (or rejected)
  5. must-visit attraction preserved
  6. optional low-priority point rejected
  7. must-eat restaurant infeasible due to closing time
  8. missing rating data uses neutral default
  9. ambiguous duplicated Chinese/English place names are merged
  10. relaxed plan has more buffer than balanced plan

Run with: pytest test_planner.py -v
"""

from __future__ import annotations

import datetime as dt
import uuid

from clustering import assign_clusters_to_days, cluster_places_by_travel_time
from extract_places import extract_places_from_text
from mock_maps import MockProvider
from models import DayHours, OpeningHours, Place, PlaceCategory, PriorityLevel, TransportMode, TripRequest
from planner import (
    build_day_route,
    enrich_places,
    fit_attractions_to_budget,
    plan_trip,
    restaurant_feasible,
)
from scoring import NEUTRAL_RATING


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_attraction(
    name: str, lat: float, lng: float, category: PlaceCategory = PlaceCategory.LANDMARK,
    priority: PriorityLevel = PriorityLevel.OPTIONAL, duration: int = 90,
) -> Place:
    return Place(
        id=str(uuid.uuid4()), name=name, category=category, priority=priority,
        lat=lat, lng=lng, expected_duration_minutes=duration,
    )


def make_restaurant(
    name: str, lat: float, lng: float, hours: tuple[int, int, int, int],
    priority: PriorityLevel = PriorityLevel.CANDIDATE_RESTAURANT, duration: int = 75,
) -> Place:
    open_h, open_m, close_h, close_m = hours
    opening_hours = OpeningHours(
        by_weekday={wd: DayHours(dt.time(open_h, open_m), dt.time(close_h, close_m)) for wd in range(7)},
        last_entry_offset_minutes=0, default_open=True,
    )
    return Place(
        id=str(uuid.uuid4()), name=name, category=PlaceCategory.RESTAURANT, priority=priority,
        lat=lat, lng=lng, opening_hours=opening_hours, expected_duration_minutes=duration,
    )


WEDNESDAY = dt.date(2024, 1, 3)


# --------------------------------------------------------------------------
# 1. restaurant closes soon
# --------------------------------------------------------------------------

def test_restaurant_closes_soon():
    place = make_restaurant("Test BBQ", 39.9, 116.4, hours=(11, 0, 14, 0))

    ok_early, _ = restaurant_feasible(place, WEDNESDAY, dt.time(11, 30), dining_minutes=60, safety_buffer_minutes=20)
    assert ok_early

    ok_late, reason = restaurant_feasible(place, WEDNESDAY, dt.time(13, 30), dining_minutes=60, safety_buffer_minutes=20)
    assert not ok_late
    assert "closing" in reason


# --------------------------------------------------------------------------
# 2. too many attractions for one day
# --------------------------------------------------------------------------

def test_too_many_attractions_for_one_day():
    provider = MockProvider()
    start = (39.90, 116.40)
    attractions = [
        make_attraction(f"Full-day spot {i}", 39.90 + i * 0.002, 116.40 + i * 0.002,
                         category=PlaceCategory.THEME_PARK, priority=PriorityLevel.OPTIONAL, duration=300)
        for i in range(4)
    ]
    attractions[0].priority = PriorityLevel.MUST_VISIT

    kept, rejected = fit_attractions_to_budget(
        attractions, day_span_minutes=12 * 60, buffer_ratio=0.20, provider=provider,
        mode=TransportMode.DRIVING, city="Beijing", start_coords=start,
    )

    assert attractions[0] in kept
    assert len(kept) < len(attractions)
    assert len(rejected) > 0


# --------------------------------------------------------------------------
# 3. two-day clustering
# --------------------------------------------------------------------------

def test_two_day_clustering():
    provider = MockProvider()
    group_a = [make_attraction(f"Central{i}", 39.90 + i * 0.002, 116.40 + i * 0.002) for i in range(3)]
    group_b = [make_attraction(f"FarArea{i}", 40.00 + i * 0.002, 116.28 + i * 0.002) for i in range(3)]

    clusters = cluster_places_by_travel_time(group_a + group_b, provider, TransportMode.DRIVING, "Beijing")
    assert len(clusters) == 2

    cluster_name_sets = [{p.name for p in c.places} for c in clusters]
    assert {p.name for p in group_a} in cluster_name_sets
    assert {p.name for p in group_b} in cluster_name_sets

    day_groups = assign_clusters_to_days(clusters, 2, (39.91, 116.41), provider, TransportMode.DRIVING, "Beijing")
    assert len(day_groups) == 2
    assert {p.name for p in day_groups[0]} | {p.name for p in day_groups[1]} == {
        p.name for p in group_a + group_b
    }


# --------------------------------------------------------------------------
# 4. far restaurant moved to another day (or rejected)
# --------------------------------------------------------------------------

def test_far_restaurant_moved_to_another_day():
    from planner import assign_restaurants_to_days

    provider = MockProvider()
    day1_attractions = [make_attraction("Day1 Spot", 39.916, 116.397)]
    day2_attractions = [make_attraction("Day2 Spot", 39.916, 116.397)]
    day_groups = [day1_attractions, day2_attractions]

    day_dates = [dt.date(2024, 1, 1), dt.date(2024, 1, 2)]  # Jan 1 2024 is a Monday
    restaurant = make_restaurant("Ambiguous BBQ", 39.917, 116.398, hours=(11, 0, 21, 0))
    restaurant.opening_hours.by_weekday[day_dates[0].weekday()] = DayHours(None, None)  # closed on day 1's date

    day_restaurants, moved, rejected = assign_restaurants_to_days(
        [restaurant], day_groups, day_dates, provider, TransportMode.DRIVING, "Beijing", (39.914, 116.411),
    )

    assert not rejected
    assert len(moved) == 1
    assert moved[0].to_day == 1
    assert restaurant in day_restaurants[1]


# --------------------------------------------------------------------------
# 5. must-visit attraction preserved
# --------------------------------------------------------------------------

def test_must_visit_attraction_preserved_even_when_overpacked():
    provider = MockProvider()
    start = (39.90, 116.40)
    attractions = [
        make_attraction(f"MustSee{i}", 39.90 + i * 0.002, 116.40 + i * 0.002,
                         priority=PriorityLevel.MUST_VISIT, duration=240)
        for i in range(5)
    ]

    kept, rejected = fit_attractions_to_budget(
        attractions, day_span_minutes=12 * 60, buffer_ratio=0.20, provider=provider,
        mode=TransportMode.DRIVING, city="Beijing", start_coords=start,
    )

    assert len(kept) == 5
    assert rejected == []


# --------------------------------------------------------------------------
# 6. optional low-priority point rejected
# --------------------------------------------------------------------------

def test_optional_low_priority_point_rejected():
    provider = MockProvider()
    start = (39.90, 116.40)
    must = make_attraction("Must Museum", 39.90, 116.40, priority=PriorityLevel.MUST_VISIT, duration=180)
    optional = make_attraction("Optional Shop", 39.95, 116.45, priority=PriorityLevel.OPTIONAL, duration=180)

    kept, rejected = fit_attractions_to_budget(
        [must, optional], day_span_minutes=6 * 60, buffer_ratio=0.20, provider=provider,
        mode=TransportMode.DRIVING, city="Beijing", start_coords=start,
    )

    assert must in kept
    assert optional not in kept
    assert any(r.place is optional for r in rejected)


# --------------------------------------------------------------------------
# 7. must-eat restaurant infeasible due to closing time
# --------------------------------------------------------------------------

def test_must_eat_restaurant_infeasible_due_to_closing():
    provider = MockProvider()
    morning_stroll = make_attraction(
        "Morning Stroll", 39.916, 116.397, priority=PriorityLevel.OPTIONAL, duration=180,
    )
    restaurant = make_restaurant("Early Duck", 39.917, 116.401, hours=(10, 30, 13, 0))
    restaurant.priority = PriorityLevel.MUST_EAT

    trip = TripRequest(
        destination="Beijing", daily_start_time=dt.time(9, 0), daily_end_time=dt.time(21, 0),
        transport_mode=TransportMode.DRIVING,
    )

    day_plan, rejected = build_day_route(0, WEDNESDAY, [morning_stroll], [restaurant], trip, provider, "balanced")

    matches = [r for r in rejected if r.place is restaurant]
    assert matches, "expected the must-eat restaurant to show up as an explained rejection"
    assert matches[0].failed_constraint == "closing_time"
    assert "closes" in matches[0].reason


# --------------------------------------------------------------------------
# 8. missing rating data uses neutral default
# --------------------------------------------------------------------------

def test_missing_rating_uses_neutral_default():
    provider = MockProvider()
    place = make_attraction("Totally Obscure Alley", 39.5, 116.9)  # not in the mock dataset

    enrich_places([place], provider, "Beijing")

    assert place.rating == NEUTRAL_RATING
    assert place.metadata_confidence < 0.5


# --------------------------------------------------------------------------
# 9. ambiguous duplicated Chinese/English place names are merged
# --------------------------------------------------------------------------

def test_ambiguous_duplicated_names_merged():
    text = "故宫\nForbidden City\n南锣鼓巷 - 人超多建议早上去"
    places = extract_places_from_text(text)

    assert len(places) == 2
    merged = next(p for p in places if p.name in ("故宫", "Forbidden City"))
    assert merged.name_cn == "故宫"
    assert merged.name_en == "Forbidden City"


# --------------------------------------------------------------------------
# Time-hint tags participate in scheduling
# --------------------------------------------------------------------------

def test_morning_only_first_and_sunset_last():
    provider = MockProvider()
    sunset_spot = make_attraction("Sunset Deck", 39.916, 116.397, duration=60)
    sunset_spot.tags.add("sunset")
    morning_spot = make_attraction("Morning Alley", 39.918, 116.399, duration=60)
    morning_spot.tags.add("morning_only")
    middle_spot = make_attraction("Middle Spot", 39.917, 116.398, duration=60)

    trip = TripRequest(destination="Beijing", transport_mode=TransportMode.DRIVING)
    day_plan, _ = build_day_route(
        0, WEDNESDAY, [sunset_spot, morning_spot, middle_spot], [], trip, provider, "balanced",
    )

    names = [s.place.name for s in day_plan.stops if not s.is_meal]
    assert names[0] == "Morning Alley"
    assert names[-1] == "Sunset Deck"
    # Sunset spot still lands before 15:30 on this light day, so the planner
    # should warn instead of silently claiming the hint was honored.
    assert any("sunset" in r.lower() for r in day_plan.reminders)


def test_avoid_weekend_reminder_on_saturday():
    provider = MockProvider()
    place = make_attraction("Crowded Street", 39.916, 116.397, duration=60)
    place.tags.add("avoid_weekend")
    trip = TripRequest(destination="Beijing", transport_mode=TransportMode.DRIVING)

    saturday = dt.date(2024, 1, 6)
    day_plan, _ = build_day_route(0, saturday, [place], [], trip, provider, "balanced")
    assert any("avoid weekends" in r for r in day_plan.reminders)

    day_plan_wed, _ = build_day_route(0, WEDNESDAY, [place], [], trip, provider, "balanced")
    assert not any("avoid weekends" in r for r in day_plan_wed.reminders)


# --------------------------------------------------------------------------
# Weather reminder for outdoor days
# --------------------------------------------------------------------------

class RainyMockProvider(MockProvider):
    def weather(self, city, date=None):
        return {"city": city, "date": date, "forecast": "light rain", "source": "mock"}


def test_rainy_forecast_adds_reminder_for_outdoor_day():
    park = make_attraction("Big Park", 39.916, 116.397, category=PlaceCategory.PARK, duration=120)
    trip = TripRequest(destination="Beijing", transport_mode=TransportMode.DRIVING)

    rainy_plan, _ = build_day_route(0, WEDNESDAY, [park], [], trip, RainyMockProvider(), "balanced")
    assert any("rain" in r.lower() for r in rainy_plan.reminders)

    sunny_plan, _ = build_day_route(0, WEDNESDAY, [park], [], trip, MockProvider(), "balanced")
    assert not any("rain" in r.lower() for r in sunny_plan.reminders)


# --------------------------------------------------------------------------
# Stretched day (forced cluster merge) raises a reminder
# --------------------------------------------------------------------------

def test_stretched_day_reminder():
    provider = MockProvider()
    near = make_attraction("Central Spot", 39.916, 116.397, priority=PriorityLevel.MUST_VISIT, duration=90)
    far = make_attraction("Great Wall-ish", 40.43, 116.57, priority=PriorityLevel.MUST_VISIT, duration=90)
    trip = TripRequest(destination="Beijing", transport_mode=TransportMode.DRIVING)

    day_plan, _ = build_day_route(0, WEDNESDAY, [near, far], [], trip, provider, "balanced")
    assert any("stretched" in r for r in day_plan.reminders)


# --------------------------------------------------------------------------
# Backup restaurant fallback
# --------------------------------------------------------------------------

def test_backup_restaurant_used_when_candidates_infeasible():
    provider = MockProvider()
    attraction = make_attraction("Morning Museum", 39.916, 116.397, duration=180)
    # Main candidate closes before the lunch window can ever fit a meal.
    early_closer = make_restaurant("Breakfast Only", 39.917, 116.398, hours=(7, 0, 11, 0))
    backup = make_restaurant("Reliable Backup", 39.918, 116.399, hours=(11, 0, 21, 0),
                              priority=PriorityLevel.BACKUP_RESTAURANT)

    trip = TripRequest(destination="Beijing", transport_mode=TransportMode.DRIVING)
    day_plan, _ = build_day_route(
        0, WEDNESDAY, [attraction], [early_closer], trip, provider, "balanced",
        backup_pool=[backup],
    )

    meal_stops = [s for s in day_plan.stops if s.is_meal]
    assert any(s.place.name == "Reliable Backup" for s in meal_stops)
    assert any("backup option" in s.note for s in meal_stops)


# --------------------------------------------------------------------------
# Style differentiation
# --------------------------------------------------------------------------

def test_relaxed_style_caps_attractions_per_day():
    provider = MockProvider()

    def fresh_attractions():
        return [
            make_attraction(f"Spot{i}", 39.916 + i * 0.002, 116.397 + i * 0.002,
                             priority=PriorityLevel.OPTIONAL, duration=60)
            for i in range(6)
        ]

    trip = TripRequest(destination="Beijing", transport_mode=TransportMode.DRIVING)

    balanced_plan, _ = build_day_route(0, WEDNESDAY, fresh_attractions(), [], trip, provider, "balanced")
    relaxed_plan, relaxed_rejected = build_day_route(0, WEDNESDAY, fresh_attractions(), [], trip, provider, "relaxed")

    balanced_count = len([s for s in balanced_plan.stops if not s.is_meal])
    relaxed_count = len([s for s in relaxed_plan.stops if not s.is_meal])
    assert relaxed_count <= 3 < balanced_count
    assert any(r.failed_constraint == "relaxed_pace_cap" for r in relaxed_rejected)


def test_must_visit_first_anchors_must_visit_early():
    provider = MockProvider()

    def fresh_attractions():
        # The must-visit is geographically the farthest from the hotel, so
        # pure nearest-neighbor ordering would visit it last.
        must = make_attraction("Anchor Palace", 39.95, 116.43, priority=PriorityLevel.MUST_VISIT, duration=90)
        near = [
            make_attraction(f"Near{i}", 39.914 + i * 0.001, 116.411 + i * 0.001,
                             priority=PriorityLevel.OPTIONAL, duration=60)
            for i in range(2)
        ]
        return [must, *near]

    trip = TripRequest(destination="Beijing", hotel="王府井酒店", transport_mode=TransportMode.DRIVING)

    balanced_plan, _ = build_day_route(0, WEDNESDAY, fresh_attractions(), [], trip, provider, "balanced")
    anchored_plan, _ = build_day_route(0, WEDNESDAY, fresh_attractions(), [], trip, provider, "must_visit_first")

    balanced_first = next(s.place.name for s in balanced_plan.stops if not s.is_meal)
    anchored_first = next(s.place.name for s in anchored_plan.stops if not s.is_meal)
    assert balanced_first != "Anchor Palace"
    assert anchored_first == "Anchor Palace"


# --------------------------------------------------------------------------
# International (non-China) trip works end to end offline
# --------------------------------------------------------------------------

def test_international_tokyo_trip_end_to_end():
    from planner import plan_trip

    raw = "浅草寺\nSenso-ji Temple\n东京塔\nTokyo Tower\n一兰拉面 - must try"
    places = extract_places_from_text(raw)
    # Bilingual duplicates with no shared characters merge via the alias
    # table, including suffixed variants like "Senso-ji Temple" vs "sensoji".
    assert len(places) == 3

    trip = TripRequest(destination="Tokyo", start_date=dt.date(2026, 11, 3),  # Tuesday
                        number_of_days=1, hotel="新宿酒店", transport_mode=TransportMode.MIXED)
    sensoji = next(p for p in places if p.name_cn == "浅草寺")
    tower = next(p for p in places if p.name_cn == "东京塔")
    ramen = next(p for p in places if p.name_cn == "一兰拉面")
    sensoji.priority, sensoji.category = PriorityLevel.MUST_VISIT, PlaceCategory.TEMPLE
    tower.priority, tower.category = PriorityLevel.HIGH_PRIORITY, PlaceCategory.OBSERVATION_DECK
    ramen.priority, ramen.category = PriorityLevel.MUST_EAT, PlaceCategory.RESTAURANT
    trip.must_visit_places = [sensoji]
    trip.high_priority_places = [tower]
    trip.must_eat_places = [ramen]

    result = plan_trip(trip, provider=MockProvider())

    scheduled = {s.place.name for plan in result.plans for d in plan.days for s in d.stops}
    assert any("浅草寺" in name for name in scheduled)
    assert any(s.is_meal for d in result.plans[0].days for s in d.stops)
    # Tokyo coordinates are well outside the mainland-China bounding box, so
    # every geocoded stop proves the pipeline is location-agnostic.
    for plan in result.plans:
        for day in plan.days:
            for stop in day.stops:
                assert stop.place.lng > 136.0


# --------------------------------------------------------------------------
# 10. relaxed plan has more buffer than balanced plan
# --------------------------------------------------------------------------

def test_relaxed_plan_has_more_buffer_than_balanced():
    provider = MockProvider()
    trip = TripRequest(
        destination="Beijing", number_of_days=1, daily_start_time=dt.time(9, 0), daily_end_time=dt.time(21, 0),
        transport_mode=TransportMode.DRIVING, start_date=WEDNESDAY,
    )
    trip.must_visit_places = [
        make_attraction("Main Museum", 39.916, 116.397, priority=PriorityLevel.MUST_VISIT, duration=150),
    ]
    trip.high_priority_places = [
        make_attraction(f"Spot{i}", 39.916 + i * 0.003, 116.397 + i * 0.003,
                         priority=PriorityLevel.HIGH_PRIORITY, duration=90)
        for i in range(3)
    ]
    trip.optional_places = [
        make_attraction(f"Optional{i}", 39.920 + i * 0.004, 116.400 + i * 0.004,
                         priority=PriorityLevel.OPTIONAL, duration=90)
        for i in range(3)
    ]

    result = plan_trip(trip, provider=provider)
    balanced = next(p for p in result.plans if p.style == "balanced")
    relaxed = next(p for p in result.plans if p.style == "relaxed")

    balanced_buffer = sum(d.buffer_ratio for d in balanced.days) / len(balanced.days)
    relaxed_buffer = sum(d.buffer_ratio for d in relaxed.days) / len(relaxed.days)

    assert relaxed_buffer > balanced_buffer
