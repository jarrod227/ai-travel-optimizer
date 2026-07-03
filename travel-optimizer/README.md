# travel-optimizer

A China-first AI travel planning skill/package. It turns messy travel
inputs - Xiaohongshu (小红书) screenshots, copied notes, restaurant lists,
attraction lists, natural-language constraints - into feasible, explainable,
multi-day itineraries.

This is a reusable Python package, not a web app. See `SKILL.md` for the
agent-facing instructions on *how* to drive it; this README is for
developers working on the code itself.

## Why this exists

Xiaohongshu-style trip planning tends to collect way more places than a
real trip can fit, without checking whether they're actually close enough
together, open at the right time, bookable, or worth the queue. This
package extracts, validates, clusters, and schedules those places into
itineraries that are honest about what doesn't fit and why.

## Package layout

| File | Responsibility |
|---|---|
| `models.py` | Core dataclasses: `Place`, `TripRequest`, `DayPlan`, `Plan`, `PlanningResult`, etc. No I/O. |
| `extract_places.py` | Free-text -> `Place` list. Note/tag detection, name normalization, CN/EN duplicate merging. |
| `providers.py` | `MapProvider` / `POIDataProvider` / `ReviewDataProvider` interfaces + `AMapProvider` (China) + `GoogleMapsProvider` (everywhere else) + `CompositeMapProvider`/`build_default_provider` (auto AMap-vs-Google routing with fallback) + a `MapboxProvider` placeholder + a shared `Cache`. |
| `mock_maps.py` | `MockProvider` - fully offline, deterministic stand-in for tests/dev. |
| `clustering.py` | Travel-time-aware geographic clustering, cluster-to-day assignment. |
| `planner.py` | Orchestration: enrichment, duration estimation, feasibility rules, day-route building, meal insertion, the three plan styles. |
| `scoring.py` | Transparent weighted scoring (maximize coverage/coherence/feasibility, penalize backtracking/rushed meals/overpacking/etc). |
| `map_export.py` | `PlanningResult` -> GeoJSON / a self-contained Leaflet HTML preview. Kept separate from planning logic. |
| `test_planner.py` | pytest suite for the planning pipeline, runs fully offline against `MockProvider`. |
| `test_providers.py` | pytest suite for provider auto-selection/fallback, runs offline against stub providers. |
| `examples/` | A worked raw-input -> normalized-request -> formatted-itinerary example. |

## Quick start

```python
import datetime as dt
from models import Place, PlaceCategory, PriorityLevel, TripRequest, TransportMode
from mock_maps import MockProvider
from planner import plan_trip

trip = TripRequest(
    destination="Beijing",
    start_date=dt.date(2026, 10, 1),
    number_of_days=3,
    hotel="王府井酒店",
    transport_mode=TransportMode.MIXED,
    user_goals=["first time in Beijing, wants classic attractions", "wants relaxed trip"],
)
trip.must_visit_places = [Place(id="1", name="故宫", category=PlaceCategory.MUSEUM, priority=PriorityLevel.MUST_VISIT)]
trip.must_eat_places = [Place(id="2", name="四季民福烤鸭店(故宫店)", category=PlaceCategory.RESTAURANT, priority=PriorityLevel.MUST_EAT)]

result = plan_trip(trip, provider=MockProvider())
for plan in result.plans:
    print(plan.title, plan.score.total)
```

Or start from raw copied text:

```python
from planner import build_trip_request_from_text, plan_trip
from mock_maps import MockProvider

trip = build_trip_request_from_text(open("examples/xiaohongshu_beijing_sample.md").read())
trip.number_of_days = 3
trip.hotel = "王府井酒店"
result = plan_trip(trip, provider=MockProvider())
```

`build_trip_request_from_text` is a convenience path only - it can't guess
which places are must-visit vs. optional. For a real plan, classify places
into the right `TripRequest` list yourself (or have the agent driving this
skill do it) using the user's stated goals. See `examples/example_request.json`
next to `examples/xiaohongshu_beijing_sample.md` for what that classification
step looks like, and run `python examples/run_example.py` for the full
pipeline end to end (output captured in `examples/sample_output.md`).

## Providers

The planner only ever talks to the `MapProvider` / `POIDataProvider` /
`ReviewDataProvider` interfaces in `providers.py` - never to a vendor SDK
directly. That's what makes `MockProvider` a drop-in replacement for tests
and `AMapProvider`/`GoogleMapsProvider` swappable for a future `MapboxProvider`
without touching `clustering.py`, `planner.py`, or `scoring.py`.

- **`AMapProvider`** (`providers.py`) - preferred for China trips. Needs the
  `AMAP_API_KEY` environment variable (never hard-code it). Uses AMap's
  Web Service REST API (`stdlib urllib`, no `requests` dependency) for
  geocoding, POI search, routing (walking/driving/transit), and weather.
  Every network call is wrapped in `Cache` so a planning session never
  requests the same `(place, mode)` pair twice.
- **`GoogleMapsProvider`** (`providers.py`) - preferred for everywhere else.
  Needs the `GOOGLE_MAPS_API_KEY` environment variable. Uses the Geocoding,
  Places Text Search, Directions, and Distance Matrix REST APIs (same
  `urllib` + `Cache` approach as AMap; Distance Matrix gives it a real
  batch route lookup, not just the pairwise fallback). Google's Places API
  reports a 0-4 `price_level`, not a currency amount, so `avg_cost_rmb` is
  intentionally left unset rather than mapped to a fake RMB figure. Google
  Maps Platform has no bundled weather API, so `weather()` returns `None`.
- **`CompositeMapProvider` + `providers.build_default_provider()`** - auto
  selection. `build_default_provider()` builds whichever of AMap/Google
  Maps have an API key configured and wraps them so China destinations
  prefer AMap and everything else prefers Google Maps, with automatic
  fallback to the other provider if the preferred one is unavailable or
  returns nothing. Geocoding/POI search decide from the `city` string
  (`providers.looks_like_china`); `travel_time` decides from the actual
  coordinates (a mainland-China bounding box), which is more reliable once
  coordinates are known. Raises `ProviderConfigError` if neither key is
  set - callers that want a safe offline default should catch that and
  fall back to `mock_maps.MockProvider()`.
- **`MockProvider`** (`mock_maps.py`) - fully offline. Ships a small,
  real-coordinate Beijing dataset (Forbidden City, Jingshan/Beihai/Temple
  of Heaven, Summer Palace, 798, a few restaurants, and Mutianyu Great Wall
  as a genuine ~70km outlier) so clustering/feasibility logic can be
  exercised end to end without a key.
- **`MapboxProvider`** - an unimplemented placeholder documenting the same
  interface for a future third option.
- **`DianpingReviewProvider` / `MeituanReviewProvider`** - unimplemented
  placeholders. **This package does not scrape Meituan/Dianping.** A real
  implementation would need an official partner API; until then, ratings
  come from AMap, Google, a user-provided score, or a neutral mock default
  with the source explicitly recorded (`RatingSource`: `amap` / `google` /
  `dianping` / `meituan` / `user` / `mock` / `unknown`).

```python
from providers import build_default_provider, ProviderConfigError
from mock_maps import MockProvider

try:
    provider = build_default_provider()  # needs AMAP_API_KEY and/or GOOGLE_MAPS_API_KEY
except ProviderConfigError:
    provider = MockProvider()  # safe offline fallback
```

## Rating policy

Ratings influence plan quality but never dominate it. `scoring.py` buckets
rating differences under ~0.2 stars as equivalent, and weighs review count
as a confidence multiplier rather than letting a 4.9-with-3-reviews beat a
4.7-with-50k-reviews. Missing ratings get `scoring.NEUTRAL_RATING` (3.5)
with low `metadata_confidence`, surfaced to the user via
`PlanningResult.low_confidence_places`.

## Feasibility rules

See `planner.restaurant_feasible` and `planner.attraction_feasible`. Key
rule: a restaurant being "open when you arrive" is not sufficient - it's
only feasible if `arrival + dining_duration + safety_buffer <= closing_time`.
Must-visit/must-eat places are only ever rejected for a genuine hard
constraint (closed that day, after last entry, or no way to fit the
closing-time math anywhere) - every rejection carries a `reason`,
`failed_constraint`, and `alternative_suggestion` (`models.RejectedPlace`).
When none of a day's candidate restaurants fit a meal window, the shared
backup-restaurant pool is tried before declaring the meal infeasible; a
backup used on one day stays consumed for the rest of that plan.

## Plan styles

The three output plans differ structurally, not just in score:

- **Best Balanced** - maximizes what fits within a ~20% schedule buffer.
- **Must-Visit First** - reorders each day so must-visit places take the
  morning slot (fresh legs, lower crowd risk), even at some geographic cost.
- **Relaxed / Low-Risk** - hard-caps each day at 3 attractions (must-visit
  places never count against being dropped) and keeps a ~32% buffer.

## Source-note hints and risk reminders

Tags extracted from the source text actively shape the schedule:
`morning_only` places are moved to the front of their day and `sunset`
places to the end (with a warning when the timing still can't be honored),
and `avoid_weekend` places trigger a crowd warning when the travel date is
a Saturday/Sunday. Each day also gets a reminder when its stops are
geographically stretched (worst point-to-point leg > 45 min, usually a
forced cluster merge) and when the provider's weather forecast mentions
rain/snow on a day with outdoor stops (a keyword heuristic over the raw
forecast payload - provider shapes differ).

## Map output

`map_export.py` converts a `PlanningResult` into a GeoJSON `FeatureCollection`
(day layers, stop order, meal flags, rejected/backup markers as separate
layers) or a self-contained Leaflet HTML preview
(`map_export.to_leaflet_html`). This is intentionally decoupled from
planning: `Planner -> structured itinerary -> map_export.py -> renderer`.
No image-generation models are used for maps. The Leaflet HTML preview
loads Leaflet + OpenStreetMap tiles from a CDN, so it needs internet access
in the browser - it's a local dev convenience, not a production embed.

## Running tests

```bash
pip install pytest
pytest -v
```

All tests run offline - no network access or API key required.

- `test_planner.py` - restaurant-closing-time math, overpacked-day
  trimming, two-day geographic clustering, moving a restaurant to a day
  where it actually fits (vs. rejecting it), must-visit places surviving
  budget trimming, optional places being explicitly dropped with a reason,
  must-eat infeasibility explanations, the neutral-rating default,
  Chinese/English duplicate-place merging, relaxed plans carrying more
  schedule buffer than balanced ones, time-hint-tag scheduling, weather/
  stretched-day reminders, backup-restaurant fallback, and plan-style
  differentiation.
- `test_providers.py` - `CompositeMapProvider` routing and fallback
  (China-vs-not by city string and by coordinates, falling back when the
  preferred provider errors or finds nothing) using small in-memory stub
  providers, not real AMap/Google Maps calls.

## Known limitations / assumptions (V1)

- Route ordering is nearest-neighbor + bounded 2-opt, not an exact TSP
  solver - fine for day-sized stop counts (< ~10), not meant for more.
- Meals are inserted opportunistically as the day's clock crosses the
  lunch/dinner window; a long attraction that would otherwise swallow the
  whole window triggers an early "grab food near here first" fallback,
  and if attractions finish before a window opens the planner idles (free
  time) until it does (see `planner.build_day_route`). This is still a
  heuristic, not a full schedule search.
- Without an explicit `start_date`, weekday-based closures use a
  placeholder reference date - pass real dates if that matters.
- Clustering avoids `numpy`/`scipy`/`sklearn` on purpose (simple, readable,
  no extra dependencies) - it's greedy agglomerative clustering by
  travel time, which is adequate at the place-counts a trip actually has.
- No UI. No paid-API hard dependency. No scraping of Meituan/Dianping.
