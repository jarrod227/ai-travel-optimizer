#!/usr/bin/env python3
"""Runnable end-to-end example: raw Xiaohongshu-style text -> normalized
TripRequest -> multi-style itineraries -> formatted text + map exports.

Usage (from the travel-optimizer/ directory):
    python examples/run_example.py

Uses MockProvider only - no API key, no network calls. Swap in
``AMapProvider()`` (with ``AMAP_API_KEY`` set) for a real run.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from map_export import to_leaflet_html  # noqa: E402
from mock_maps import MockProvider  # noqa: E402
from models import Place, PlaceCategory, Plan, PlanningResult, PriorityLevel, TransportMode, TripRequest  # noqa: E402
from planner import plan_trip  # noqa: E402


def place(name: str, category: PlaceCategory, priority: PriorityLevel, **kwargs) -> Place:
    return Place(id=str(uuid.uuid4()), name=name, category=category, priority=priority, **kwargs)


def build_sample_trip_request() -> TripRequest:
    """Hand-classified version of examples/xiaohongshu_beijing_sample.md -
    i.e. what an agent should produce after reading the raw text and the
    user's stated goals (first-timer, relaxed pace, photo spots, no
    shopping, skip long queues)."""

    trip = TripRequest(
        destination="Beijing",
        start_date=dt.date(2026, 10, 1),
        number_of_days=3,
        hotel="王府井酒店",
        daily_start_time=dt.time(9, 0),
        daily_end_time=dt.time(21, 0),
        transport_mode=TransportMode.MIXED,
        user_goals=[
            "first time in Beijing, wants classic attractions",
            "wants relaxed trip",
            "wants photo spots",
            "does not like shopping",
            "willing to skip viral restaurants if they waste too much time",
        ],
        soft_constraints=["avoid_shopping_districts", "avoid_long_queues"],
    )

    trip.must_visit_places = [
        place("故宫", PlaceCategory.MUSEUM, PriorityLevel.MUST_VISIT, reservation_required=True),
        place("颐和园", PlaceCategory.PARK, PriorityLevel.MUST_VISIT),
    ]
    trip.high_priority_places = [
        place("景山公园", PlaceCategory.LANDMARK, PriorityLevel.HIGH_PRIORITY),
        place("北海公园", PlaceCategory.PARK, PriorityLevel.HIGH_PRIORITY),
        place("天坛", PlaceCategory.TEMPLE, PriorityLevel.HIGH_PRIORITY),
    ]
    trip.optional_places = [
        place("南锣鼓巷", PlaceCategory.OLD_STREET, PriorityLevel.OPTIONAL, tags={"long_queue"}),
        place("798艺术区", PlaceCategory.OLD_STREET, PriorityLevel.OPTIONAL),
    ]
    trip.must_eat_places = [
        place("四季民福烤鸭店(故宫店)", PlaceCategory.RESTAURANT, PriorityLevel.MUST_EAT, reservation_required=True),
    ]
    trip.candidate_restaurants = [
        place("文宇奶酪店", PlaceCategory.CAFE, PriorityLevel.CANDIDATE_RESTAURANT),
        place("烤肉季(什刹海店)", PlaceCategory.RESTAURANT, PriorityLevel.CANDIDATE_RESTAURANT),
        place("颐和园附近餐厅", PlaceCategory.RESTAURANT, PriorityLevel.CANDIDATE_RESTAURANT),
    ]
    trip.backup_restaurants = [
        place("烤肉季(什刹海店)", PlaceCategory.RESTAURANT, PriorityLevel.BACKUP_RESTAURANT),
    ]
    return trip


def format_plan(plan: Plan) -> str:
    lines = [f"## {plan.title} (score: {plan.score.total:.1f})", ""]
    for day in plan.days:
        lines.append(f"**{day.theme}**")
        for stop in day.stops:
            marker = " (meal)" if stop.is_meal else ""
            lines.append(
                f"{stop.arrival.strftime('%H:%M')}-{stop.departure.strftime('%H:%M')} "
                f"{stop.place.name}{marker}"
            )
        if day.reminders:
            for reminder in day.reminders:
                lines.append(f"  ⚠ {reminder}")
        lines.append(f"  buffer: {day.buffer_ratio:.0%}")
        lines.append("")
    lines.append("Why this plan works:")
    lines.extend(f"- {line}" for line in plan.why_it_works)
    if plan.risks:
        lines.append("")
        lines.append("Risks / reminders:")
        lines.extend(f"- {r}" for r in plan.risks)
    lines.append("")
    return "\n".join(lines)


def format_result(result: PlanningResult) -> str:
    sections = [f"# {result.trip_request.destination} itinerary options", ""]
    for plan in result.plans:
        sections.append(format_plan(plan))

    sections.append("## Rejected places")
    if result.rejected_places:
        for r in result.rejected_places:
            sections.append(f"- **{r.place.name}**: {r.reason}")
    else:
        sections.append("- (none)")
    sections.append("")

    sections.append("## Moved to another day")
    if result.moved_places:
        for m in result.moved_places:
            sections.append(f"- **{m.place.name}**: {m.reason}")
    else:
        sections.append("- (none)")
    sections.append("")

    sections.append("## Reservation reminders")
    if result.reservation_reminders:
        sections.extend(f"- {r}" for r in result.reservation_reminders)
    else:
        sections.append("- (none)")
    sections.append("")

    sections.append("## Backup options")
    sections.extend(f"- {b.name}" for b in result.backup_options)
    sections.append("")

    sections.append("## Low-confidence metadata")
    if result.low_confidence_places:
        sections.extend(f"- {p.name}" for p in result.low_confidence_places)
    else:
        sections.append("- (none)")

    return "\n".join(sections)


def main() -> None:
    provider = MockProvider()
    trip_request = build_sample_trip_request()
    result = plan_trip(trip_request, provider=provider)

    print(format_result(result))

    out_path = os.path.join(os.path.dirname(__file__), "sample_map.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(to_leaflet_html(result, plan_style="balanced"))
    print(f"\n(Leaflet preview written to {out_path})", file=sys.stderr)


if __name__ == "__main__":
    main()
