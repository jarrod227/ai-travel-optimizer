"""Map-ready outputs for a planning result: GeoJSON + an optional self
contained Leaflet HTML preview.

Kept strictly separate from planning: this module only reads the already
computed ``PlanningResult`` / ``Plan`` objects (see models.py) and turns them
into a map-rendering-friendly shape. No image-generation models, no
planning logic here - just structured geodata.

    Planner -> structured itinerary (models.py) -> map_export.py -> map renderer

The GeoJSON output is renderer-agnostic (works with Leaflet, Mapbox GL, the
AMap JS SDK, etc). ``to_leaflet_html`` additionally renders a quick local
preview using Leaflet via CDN, which requires internet access in the
browser viewing the file (there is no bundled offline tile set).
"""

from __future__ import annotations

import json
from typing import Optional

from models import Plan, PlanningResult

_DAY_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c"]


def plan_to_geojson(plan: Plan) -> dict:
    """One FeatureCollection per plan: each stop is a Point feature tagged
    with its day index (layer), stop order, and whether it's a meal - a
    renderer can filter/style by these properties to show day layers,
    ordering, and must-visit/must-eat markers."""

    features = []
    for day in plan.days:
        color = _DAY_COLORS[day.day_index % len(_DAY_COLORS)]
        coords_in_order = []
        for order, stop in enumerate(day.stops):
            place = stop.place
            if not place.is_geocoded:
                continue
            coords_in_order.append((place.lng, place.lat))
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [place.lng, place.lat]},
                    "properties": {
                        "name": place.name,
                        "day_index": day.day_index,
                        "stop_order": order,
                        "category": place.category.value,
                        "priority": place.priority.value,
                        "is_meal": stop.is_meal,
                        "arrival": stop.arrival.strftime("%H:%M"),
                        "departure": stop.departure.strftime("%H:%M"),
                        "color": color,
                        "layer": "itinerary",
                    },
                }
            )
        if len(coords_in_order) >= 2:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords_in_order},
                    "properties": {"day_index": day.day_index, "color": color, "layer": "route"},
                }
            )
    return {"type": "FeatureCollection", "features": features}


def planning_result_to_geojson(result: PlanningResult, plan_style: str = "balanced") -> dict:
    """Same as ``plan_to_geojson`` for the chosen plan, plus rejected/backup
    places as separate marker layers so a renderer can show them faded out
    or with a distinct icon."""

    plan = next((p for p in result.plans if p.style == plan_style), result.plans[0])
    geojson = plan_to_geojson(plan)

    for rejected in result.rejected_places:
        place = rejected.place
        if not place.is_geocoded:
            continue
        geojson["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [place.lng, place.lat]},
                "properties": {
                    "name": place.name,
                    "layer": "rejected",
                    "reason": rejected.reason,
                    "color": "#999999",
                },
            }
        )

    for backup in result.backup_options:
        if not backup.is_geocoded:
            continue
        geojson["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [backup.lng, backup.lat]},
                "properties": {"name": backup.name, "layer": "backup", "color": "#0aa1a1"},
            }
        )

    return geojson


def to_leaflet_html(result: PlanningResult, plan_style: str = "balanced", title: Optional[str] = None) -> str:
    """Render a simple, self-contained-except-for-CDN HTML preview. Requires
    network access in the browser (Leaflet + tiles load from CDN) - meant
    for local preview during development, not for production embedding."""

    geojson = planning_result_to_geojson(result, plan_style)
    all_coords = [f["geometry"]["coordinates"] for f in geojson["features"] if f["geometry"]["type"] == "Point"]
    if all_coords:
        center_lng = sum(c[0] for c in all_coords) / len(all_coords)
        center_lat = sum(c[1] for c in all_coords) / len(all_coords)
    else:
        center_lat, center_lng = 39.9, 116.4

    page_title = title or f"{result.trip_request.destination} itinerary ({plan_style})"
    geojson_json = json.dumps(geojson, ensure_ascii=False)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>{page_title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  html, body, #map {{ height: 100%; margin: 0; }}
  .legend {{ position: absolute; top: 10px; right: 10px; background: white; padding: 8px 12px;
             font-family: sans-serif; font-size: 13px; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.3); z-index: 1000; }}
</style>
</head>
<body>
<div id="map"></div>
<div class="legend">
  <div><b>{page_title}</b></div>
  <div style="color:#999999">&#9679; rejected</div>
  <div style="color:#0aa1a1">&#9679; backup</div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const map = L.map('map').setView([{center_lat}, {center_lng}], 12);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(map);

  const data = {geojson_json};
  L.geoJSON(data, {{
    pointToLayer: function (feature, latlng) {{
      const color = feature.properties.color || '#3388ff';
      return L.circleMarker(latlng, {{ radius: 7, color: color, fillColor: color, fillOpacity: 0.85 }});
    }},
    style: function (feature) {{
      return {{ color: feature.properties.color || '#3388ff', weight: 3 }};
    }},
    onEachFeature: function (feature, layer) {{
      const p = feature.properties;
      const label = p.name ? `<b>${{p.name}}</b>` : '';
      const extra = p.arrival ? `<br>${{p.arrival}}-${{p.departure}}` : '';
      const reason = p.reason ? `<br><i>${{p.reason}}</i>` : '';
      layer.bindPopup(label + extra + reason);
    }}
  }}).addTo(map);
</script>
</body>
</html>
"""
