---
name: travel-optimizer
description: China-first AI travel planning skill. Turns messy travel inputs (Xiaohongshu screenshots/copied text, attraction and restaurant lists, natural-language constraints) into feasible, explainable, multi-day itineraries. Use whenever a user pastes travel recommendations, a place list, or asks for a day-by-day trip plan for a China destination (or any destination once geocoded).
---

# travel-optimizer

A China-first travel *攻略* (guide) digestion and optimization agent, not
just a route sorter. Its job is to help a user decide which recommendations
are actually worth visiting, which are unrealistic, and to produce
itineraries that are geographically coherent, time-feasible, and clearly
explained.

This skill is a Python package (`models.py`, `extract_places.py`,
`providers.py`, `mock_maps.py`, `clustering.py`, `planner.py`, `scoring.py`,
`map_export.py`) plus tests. Drive it by calling these functions - don't
re-implement the logic inline.

## When to use this skill

- The user pastes Xiaohongshu (小红书) screenshots, copied notes, or a list
  of attractions/restaurants and wants a trip plan.
- The user describes constraints in natural language ("first time in
  Beijing", "wants a relaxed trip", "must eat Peking duck", "don't like
  shopping", "hotel is near Wangfujing", "4 days").
- The user asks to re-optimize, re-cluster, or explain why a place was
  dropped from a plan.

## Workflow

1. **Extract.** Run `extract_places.extract_places_from_text(raw_text)` on
   any pasted screenshots/notes. This normalizes names, deduplicates
   (including common Chinese/English name pairs via a small alias table),
   and tags notes like `reservation_required`, `morning_only`, `sunset`,
   `closed_mondays`, `long_queue`, `far_from_center`, `must_try`,
   `avoid_weekend`.

2. **Classify into a `TripRequest` (models.py).** This is the step that
   needs judgment, not just parsing - read the user's stated goals and
   priorities and sort extracted places into:
   `must_visit_places`, `high_priority_places`, `optional_places`,
   `must_eat_places`, `candidate_restaurants`, `backup_restaurants`.
   Fill in `destination`, `start_date`/`number_of_days`, `hotel`,
   `daily_start_time`/`daily_end_time`, `transport_mode`, `user_goals`.
   Use `user_overrides` (keyed by `extract_places.normalize_place_name(name)`)
   for anything the user said explicitly about a specific place (expected
   duration, a confirmed reservation slot, "skip this if it's crowded",
   etc). See `examples/example_request.json` for a worked example next to
   `examples/xiaohongshu_beijing_sample.md`.

   Do not invent must-visit status. If the user didn't clearly prioritize a
   place, default it to `optional`. Never silently promote something to
   must-visit/must-eat - that tier means "never drop this unless a hard
   constraint makes it impossible."

3. **Pick a provider (providers.py / mock_maps.py).** Use `AMapProvider`
   (needs `AMAP_API_KEY` env var) for real China planning. Use
   `MockProvider` (mock_maps.py) when there's no key available, for a
   quick draft, or for anything test-related - it's fully offline and
   deterministic. Never hard-code an API key.

4. **Run the planner.** Call `planner.plan_trip(trip_request, provider=...)`.
   This one call does geocoding/enrichment, duration estimation,
   clustering, restaurant-to-day assignment, day-route building, and
   scoring for all three plan styles. It returns a `PlanningResult`
   (models.py) with `.plans` (balanced / must_visit_first / relaxed),
   `.rejected_places`, `.moved_places`, `.reservation_reminders`,
   `.low_confidence_places`, `.backup_options`.

5. **Present all three plans**, not just one - see Output format below.

6. **Optionally export a map.** `map_export.planning_result_to_geojson(result)`
   for a renderer-agnostic GeoJSON, or `map_export.to_leaflet_html(result)`
   for a quick local HTML preview (requires internet in the browser for the
   Leaflet/tile CDN - it's a dev convenience, not a production embed).

If you only need one stage (e.g. "why was this restaurant rejected?"),
call the underlying function directly instead of the whole pipeline:
`planner.restaurant_feasible`, `planner.attraction_feasible`,
`planner.fit_attractions_to_budget`, `clustering.cluster_places_by_travel_time`,
`planner.assign_restaurants_to_days`.

## Decision hierarchy (apply in this order when rules conflict)

1. Hard constraints
2. User-defined must-visit / must-eat
3. Feasibility (opening hours, last entry, closing-time math, reservations)
4. User goals (relaxed vs. packed, food-focused, photo spots, no shopping, ...)
5. Geographic coherence
6. Time efficiency
7. Ratings and reviews
8. Minor preferences

## Rating policy

Ratings inform quality but never dominate. A place's user-assigned priority
always outranks its star rating. Treat rating differences under ~0.2 stars
as equivalent unless one has much higher review confidence (`scoring.py`
buckets ratings to enforce this). Missing ratings get a neutral default
(`scoring.NEUTRAL_RATING`, currently 3.5) with low `metadata_confidence` -
never silently treat "unrated" as "bad" or "good."

## Transportation policy

Don't assume driving is optimal. `TransportMode` supports walking, driving,
taxi, subway, transit, and mixed. `MockProvider`/`AMapProvider` estimate
duration, distance, transfers, and cost per mode - pass the user's actual
preference through `TripRequest.transport_mode`, and mention transfer count
and walking distance in risk notes when they're high.

## Feasibility rules (never violate these silently)

A place is infeasible if: it's too far from every day's cluster; it's
closed on the travel day; it requires a reservation with no known slot;
arrival is after last-entry time; for restaurants, `arrival + dining
duration + safety buffer > closing time` (being merely "open at arrival"
is not sufficient); or including it would force dropping a higher-priority
place. Must-visit/must-eat places are only ever rejected for a **hard**
feasibility failure, and every rejection must say why, what constraint
failed, and what the alternative is (`models.RejectedPlace`).

## Output format (always produce this, whether from `plan_trip` or manually)

For each of the three plans (Best Balanced / Must-Visit First / Relaxed):
day-by-day stops with timestamps, expected duration, travel time between
stops, lunch/dinner placement, a score summary, a short "why this works",
and risks/reminders. Then, once, for the overall result: rejected places
with reasons, places moved to another day and why, reservation reminders,
low-confidence-metadata places, and backup options. `examples/run_example.py`
shows a working formatter (`format_result`) you can copy or adapt; its
output is captured in `examples/sample_output.md`.

## Assumptions worth restating to the user

- Duration estimates are category defaults (see `planner.DURATION_RANGES_MINUTES`)
  unless the user gave an override - always mention when a number is an estimate.
- Without an explicit `start_date`, weekday-based closures (e.g. "closed
  Mondays") can't be checked precisely - ask for dates if that matters.
- `MockProvider`'s dataset only covers a handful of well-known Beijing POIs;
  for anything else (or a different city) use `AMapProvider` with a real
  key, or expect `metadata_confidence` to come back low.
