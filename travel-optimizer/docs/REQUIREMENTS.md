# Original requirements (archived)

This is a snapshot of the original request that this package was built
from, kept for traceability. `SKILL.md` (agent-facing instructions) and
`README.md` (developer docs) are the distilled, executable version of this
spec - if the two ever disagree, treat this file as "what was originally
asked for" and the other two as "what actually got built," and reconcile
by updating `SKILL.md`/`README.md` (this file is a historical record, not
meant to be edited to match the code after the fact).

---

Build a Codex Agent Skill package named travel-optimizer.

## Goal

Create a China-first AI travel planning skill that turns messy user travel
inputs — especially Xiaohongshu screenshots, copied notes, restaurant
lists, attraction lists, and natural-language constraints — into feasible,
optimized, multi-day travel itineraries.

## Core scenario

Users often collect many Xiaohongshu-style recommendations before
traveling. These recommendations include restaurants, cafes, scenic spots,
shopping areas, photo spots, and itinerary suggestions. Many are
unrealistic because they are too far apart, overcrowded, require
reservation, close too early, or do not fit the user's actual travel days.
This skill should extract useful places, validate them, cluster them by
geography and travel time, then generate multiple feasible itinerary
options with clear explanations.

Do not build a full web app yet. Build a reusable Codex Agent Skill/package
first.

## Package structure

- SKILL.md
- README.md
- examples/
- optional Python helpers:
  - models.py
  - extract_places.py
  - providers.py
  - mock_maps.py
  - clustering.py
  - planner.py
  - scoring.py
  - test_planner.py

## User input style

The skill must support flexible natural input, not only rigid templates.

Users may provide:

1. Xiaohongshu screenshots or copied text
2. Attraction lists
3. Restaurant/cafe lists
4. Hotel/start/end location
5. Travel dates or number of days
6. Transportation mode: walking, taxi, driving, subway, public transit
7. Must-visit places
8. Must-eat restaurants
9. Optional places
10. User goals such as:

- first time in Beijing, wants classic attractions
- wants relaxed trip
- wants food-focused trip
- wants photo spots
- does not like shopping
- willing to skip viral restaurants if they waste too much time

Internally normalize user input into a TripRequest model:

- destination
- start_date / end_date / number_of_days
- hotel / start_location / end_location
- daily_start_time / daily_end_time
- transport_mode
- user_goals
- preferences
- hard_constraints
- soft_constraints
- must_visit_places
- high_priority_places
- optional_places
- must_eat_places
- candidate_restaurants
- backup_restaurants
- user_overrides for expected duration, priority, notes, reservation, etc.

## Core workflow

1. Extract places from screenshots/text.
2. Normalize place names.
3. Deduplicate repeated places.
4. Separate attractions, restaurants, cafes, shopping areas, hotels, and
   unknown places.
5. Extract useful notes:
   - reservation required
   - morning only
   - sunset
   - closed on certain days
   - long queue
   - far from city center
   - must try
   - avoid weekends
6. Geocode places.
7. Query travel times and POI metadata.
8. Run feasibility analysis.
9. Cluster locations by realistic travel time.
10. Assign clusters to travel days.
11. Optimize daily routes.
12. Insert lunch and dinner.
13. Score candidate plans.
14. Output multiple plans and rejected places with reasons.

## Map and POI provider requirements

Use a provider abstraction.

Create interfaces:

- MapProvider
- POIDataProvider
- ReviewDataProvider

Support providers:

- AMapProvider for China-first planning
- MockProvider for tests and offline development
- Optional placeholder adapters for Google Maps / Mapbox later

AMap should be the preferred provider for China trips.

AMapProvider should be designed to support:

- geocoding
- POI search
- coordinates
- travel time / route duration
- distance matrix or pairwise route estimates
- walking / driving / transit estimates
- opening hours when available
- POI rating when available
- average cost when available
- weather when useful

Do not hard-code Google Maps as the only option.

## Review and rating logic

- Use AMap ratings/cost/opening-hour fields when available.
- Design optional adapters for Meituan/Dianping review data, but do not
  require them in V1.
- Do not scrape Meituan or Dianping.
- If Meituan/Dianping API access is unavailable, use manual user scores,
  AMap ratings, or mock review data.
- Store rating source explicitly: amap, dianping, meituan, user, mock, or
  unknown.

## Feasibility rules

A place may be infeasible if:

- too far from all daily clusters
- closed on the travel day
- requires reservation and no available slot is known
- arrival is after last-entry time
- restaurant arrival + dining duration + safety buffer exceeds closing time
- queue/wait time makes the schedule unrealistic
- adding it would force removal of higher-priority locations
- it creates severe backtracking

## Priority rules

Never reject must-visit or must-eat places unless hard constraints make
them infeasible.

Priority levels:

- must_visit: must arrange unless impossible
- high_priority: strongly prefer
- optional: include if efficient
- rejectable: can be dropped
- must_eat: must arrange unless impossible
- candidate_restaurant: choose if it fits well
- backup_restaurant: use for fallback

If a must-visit or must-eat place cannot be arranged, explicitly explain:

- why it is infeasible
- what constraint failed
- whether another day/time could work
- what alternative is recommended

## Expected duration logic

Estimate attraction duration by category, with user override support.

Default examples:

- museum / gallery: 2–4 hours
- large park / scenic area: 3–6 hours
- landmark photo stop: 30–90 minutes
- old street / shopping district: 1–3 hours
- temple / church: 45–120 minutes
- observation deck: 45–90 minutes
- theme park: 6–10 hours
- restaurant meal: 60–90 minutes
- cafe: 45–90 minutes

Final duration should consider:

- category
- user interest level
- queue risk
- ticket/reservation process
- crowding
- safety buffer
- user-provided override

## Clustering requirements

After geocoding, first cluster places by realistic travel time, not only
straight-line distance.

For N travel days:

- try to create N daily clusters when possible
- allow one day to contain multiple nearby micro-clusters
- move far but high-priority points into their own day or half-day if
  needed
- do not force all user-provided places into the final plan

## Daily route optimization

For each day:

- start from hotel or prior user-defined start
- end at hotel or user-defined end
- select feasible attractions
- place lunch and dinner at reasonable times
- use only restaurants that are open and not too rushed
- preserve must-visit and must-eat locations where feasible
- avoid overpacking
- keep 15–25% schedule buffer
- penalize backtracking and long transfers

## Meal rules

- Lunch target window: roughly 11:30–13:30 unless user says otherwise
- Dinner target window: roughly 17:30–20:00 unless user says otherwise
- A restaurant is not feasible just because it is open at arrival.
- It is feasible only if:
  `arrival_time + expected_dining_duration + safety_buffer <= closing_time`
- Penalize arrival close to closing even if technically feasible.
- If a restaurant is too far for Day 1 but fits Day 2, move it to Day 2
  instead of rejecting it.
- If a restaurant is far from all clusters and not must-eat, reject it
  with explanation.

## Scoring

Create a clear scoring function.

Maximize:

- must-visit coverage
- high-priority attraction coverage
- restaurant preference
- POI rating
- review confidence
- geographic coherence
- reasonable meal timing
- reservation feasibility
- sufficient buffer
- route explainability

Penalize:

- total travel time
- backtracking
- rushed meals
- arrival near closing
- overpacked days
- low-priority detours
- missing metadata
- unresolved ambiguous places
- crowd/queue risk
- forced cross-city travel

## Output requirements

Do not output only one plan. Output at least three styles when possible:

1. Best Balanced Plan
   - highest overall score
   - balances attractions, food, time, and travel cost
2. Must-Visit First Plan
   - centered around user-marked must-visit/must-eat places
   - useful when the user says something like "I must go to the Forbidden
     City"
3. Relaxed / Low-Risk Plan
   - fewer places
   - larger buffers
   - lower chance of delays

For each plan output:

- day-by-day itinerary
- timestamps
- place names
- expected duration
- travel time between stops
- lunch and dinner
- score summary
- why this plan works
- risks and reminders

Also output:

- Rejected places and reasons
- Places moved to another day and reasons
- Reservation reminders
- Places with low-confidence metadata
- Backup options if a place closes or is too crowded

### Example output style

```
Day 1: Central Beijing / Classic Route
09:00 Depart hotel
09:30–12:30 Forbidden City
12:45–14:00 Lunch near Forbidden City
14:20–15:30 Jingshan Park
16:00–17:30 Beihai Park
18:00–19:30 Dinner

Rejected:
- Restaurant A: too far from today's route; adds 48 minutes of travel.
- Attraction B: arrival would be after last entry.
- Cafe C: optional and conflicts with higher-priority location.
```

## Testing requirements

Include tests for:

1. restaurant closes soon
2. too many attractions for one day
3. two-day clustering
4. far restaurant moved to another day or rejected
5. must-visit attraction preserved
6. optional low-priority point rejected
7. must-eat restaurant infeasible due to closing time
8. missing rating data uses neutral default
9. ambiguous duplicated Chinese/English place names are merged
10. relaxed plan has more buffer than balanced plan

## Implementation requirements

- Python 3.11+
- Type hints
- Simple readable code
- No hard dependency on paid APIs for V1
- Mock provider must allow local tests
- Clearly document assumptions
- Do not scrape websites
- Do not build UI yet
- Keep API keys out of code
- Use environment variables for future API keys

## Important design principle

This skill is not just a route sorter. It is a travel攻略 digestion and
optimization agent. It should help users decide:

- which viral recommendations are actually worth visiting
- which places are unrealistic
- which restaurants fit the route
- which must-visit places should anchor the plan
- why certain places are rejected
- how to handle closures, reservations, and timing risk

## Decision Hierarchy

When rules conflict, follow this priority order:

1. Hard constraints
2. User-defined must-visit / must-eat
3. Feasibility
4. User goals
5. Geographic coherence
6. Time efficiency
7. Ratings and reviews
8. Minor preferences

## Rating Policy

Ratings should influence itinerary quality but should never dominate
planning. User-defined priorities always take precedence over ratings.
Geographic efficiency, feasibility, opening hours, reservation
constraints, and overall travel experience should outweigh small rating
differences. Treat rating differences smaller than about 0.2 stars as
nearly equivalent unless review confidence is much higher. Consider both
rating and review count. Missing ratings should use a neutral score and
low confidence.

## Transportation Policy

Support walking, driving, taxi, subway, public transit, and mixed mode. Do
not assume driving is always optimal. For mixed mode, evaluate realistic
combinations of walking, transit, and taxi. When scoring routes, consider
travel time, walking distance, number of transfers, transportation cost
when available, and user transport preferences.

## Map Visualization

The planner should generate map-ready outputs. Do not generate maps using
image generation models. Use real coordinates, route polylines, GeoJSON,
AMap JS API, Leaflet, or another real map-rendering method. Keep
visualization separate from planning: Planner -> structured itinerary ->
map renderer. Support selected stops, rejected places, backup places,
must-visit/must-eat markers, stop order, and day layers. Optionally
generate a simple HTML map for local preview.

## Performance and API Usage

Minimize API requests. Cache geocoding, POI metadata, ratings, opening
hours, and travel-time results. Never query the same place repeatedly
during one planning session. Batch requests whenever supported. Run
optimization on cached place objects. Keep provider APIs interchangeable.

---

*This document is an unmodified archive of the original request. See
`SKILL.md` and `README.md` for the current, maintained description of
what was actually built.*
