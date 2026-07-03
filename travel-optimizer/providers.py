"""Provider abstraction for maps / POI data / reviews.

Design goal: the planner never talks to a specific vendor. It only talks to
the ``MapProvider`` / ``POIDataProvider`` / ``ReviewDataProvider`` interfaces
below, so swapping AMap for Google Maps/Mapbox later (or running fully
offline against ``MockProvider`` in mock_maps.py) requires no changes to
clustering.py, planner.py, or scoring.py.

V1 ships:
  * ``AMapProvider``  - real implementation for China-first planning (AMap /
    高德地图 Web Service API). Requires ``AMAP_API_KEY`` env var. Never call
    this without a key configured; it raises ``ProviderConfigError`` instead
    of silently failing.
  * ``GoogleMapsProvider`` - real implementation for everywhere else
    (Geocoding, Places Text Search, Directions, Distance Matrix). Requires
    ``GOOGLE_MAPS_API_KEY`` env var.
  * ``CompositeMapProvider`` / ``build_default_provider`` - routes each call
    to AMap or Google Maps based on whether the place looks like it's in
    mainland China, with automatic fallback to the other provider if the
    preferred one is unavailable or returns nothing. See "Provider
    auto-selection" below.
  * ``MapboxProvider`` - an unimplemented placeholder documenting the same
    interface for a future third option.
  * ``DianpingReviewProvider`` / ``MeituanReviewProvider`` - unimplemented
    placeholders. We do not scrape these platforms; a real implementation
    would need to sit on top of an official partner API.

Caching: every provider method that hits a network API should be wrapped by
``Cache.get_or_compute`` so a planning session never requests the same
(place, mode) pair twice.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from models import RatingSource, TransportMode


class ProviderError(RuntimeError):
    """Raised when a provider call fails (network error, bad response, ...)."""


class ProviderConfigError(ProviderError):
    """Raised when a provider is used without required configuration (API key)."""


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

class Cache:
    """Tiny in-memory (+ optional on-disk) cache keyed by an arbitrary tuple.

    Keeping this generic lets every provider share one caching strategy:
    geocoding, POI lookups, ratings, opening hours, and travel times are all
    idempotent for the lifetime of a planning session, so we never want to
    issue the same request twice.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._store: dict[str, Any] = {}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._store = json.load(f)

    @staticmethod
    def _key(namespace: str, *parts: Any) -> str:
        return namespace + "::" + "|".join(str(p) for p in parts)

    def get_or_compute(self, namespace: str, parts: tuple, compute: Callable[[], Any]) -> Any:
        key = self._key(namespace, *parts)
        if key in self._store:
            return self._store[key]
        value = compute()
        self._store[key] = value
        if self._path:
            self._flush()
        return value

    def _flush(self) -> None:
        assert self._path is not None
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._store, f, ensure_ascii=False)


# --------------------------------------------------------------------------
# Shared value types returned by providers
# --------------------------------------------------------------------------

@dataclass
class GeocodeResult:
    lat: float
    lng: float
    formatted_address: str


@dataclass
class TravelEstimate:
    duration_minutes: float
    distance_km: float
    mode: TransportMode
    transfers: int = 0
    cost_rmb: Optional[float] = None


@dataclass
class POIDetails:
    name: str
    lat: float
    lng: float
    address: Optional[str] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    rating_source: RatingSource = RatingSource.UNKNOWN
    avg_cost_rmb: Optional[float] = None
    opening_hours_text: Optional[str] = None


# --------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------

class MapProvider(ABC):
    """Geocoding + travel-time/route estimation."""

    @abstractmethod
    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        ...

    @abstractmethod
    def travel_time(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> TravelEstimate:
        ...

    def distance_matrix(
        self,
        origins: list[tuple[float, float]],
        destinations: list[tuple[float, float]],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> list[list[TravelEstimate]]:
        """Default implementation: pairwise fallback. Providers with a real
        batch endpoint (e.g. AMap's distance API) should override this.
        """

        return [
            [self.travel_time(o, d, mode, city) for d in destinations]
            for o in origins
        ]

    def weather(self, city: str, date: Optional[str] = None) -> Optional[dict]:
        """Optional. Returns None if the provider doesn't support weather."""

        return None


class POIDataProvider(ABC):
    """POI search + metadata (rating, cost, opening hours when available)."""

    @abstractmethod
    def search_poi(self, keyword: str, city: Optional[str] = None) -> list[POIDetails]:
        ...


class ReviewDataProvider(ABC):
    """Optional third-party review overlay (never required in V1)."""

    @abstractmethod
    def get_rating(self, name: str, city: Optional[str] = None) -> Optional[POIDetails]:
        ...


# --------------------------------------------------------------------------
# AMap (高德地图) - preferred provider for China trips
# --------------------------------------------------------------------------

_AMAP_MODE_PATH = {
    TransportMode.WALKING: "walking",
    TransportMode.DRIVING: "driving",
    TransportMode.TAXI: "driving",
    TransportMode.TRANSIT: "transit/integrated",
    TransportMode.SUBWAY: "transit/integrated",
    # AMap's integrated transit routing already mixes walking + transit legs,
    # which is the closest real-world match for our MIXED mode.
    TransportMode.MIXED: "transit/integrated",
}


class AMapProvider(MapProvider, POIDataProvider):
    """China-first map/POI provider backed by the AMap (高德) Web Service API.

    Requires the ``AMAP_API_KEY`` environment variable. Never hard-code the
    key in source. This class makes real HTTP calls (stdlib ``urllib``, no
    hard dependency on ``requests``) and is therefore not exercised by the
    offline test suite (see mock_maps.MockProvider for that).
    """

    BASE_URL = "https://restapi.amap.com/v3"

    def __init__(self, api_key: Optional[str] = None, cache: Optional[Cache] = None, timeout: float = 5.0) -> None:
        self.api_key = api_key or os.environ.get("AMAP_API_KEY")
        if not self.api_key:
            raise ProviderConfigError(
                "AMapProvider requires AMAP_API_KEY to be set as an environment "
                "variable (never hard-code API keys in source)."
            )
        self.cache = cache or Cache()
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "key": self.api_key, "output": "JSON"}
        url = f"{self.BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface as ProviderError
            raise ProviderError(f"AMap request failed: {exc}") from exc
        if payload.get("status") != "1":
            raise ProviderError(f"AMap API error: {payload.get('info', 'unknown error')}")
        return payload

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        def compute() -> Optional[dict]:
            payload = self._get("geocode/geo", {"address": address, "city": city or ""})
            geocodes = payload.get("geocodes") or []
            if not geocodes:
                return None
            best = geocodes[0]
            lng_str, lat_str = best["location"].split(",")
            return {
                "lat": float(lat_str),
                "lng": float(lng_str),
                "formatted_address": best.get("formatted_address", address),
            }

        raw = self.cache.get_or_compute("amap.geocode", (address, city), compute)
        return GeocodeResult(**raw) if raw else None

    def search_poi(self, keyword: str, city: Optional[str] = None) -> list[POIDetails]:
        def compute() -> list[dict]:
            payload = self._get("place/text", {"keywords": keyword, "city": city or ""})
            results = []
            for poi in payload.get("pois", []):
                lng_str, lat_str = poi["location"].split(",")
                rating = poi.get("biz_ext", {}).get("rating")
                cost = poi.get("biz_ext", {}).get("cost")
                results.append(
                    {
                        "name": poi.get("name", keyword),
                        "lat": float(lat_str),
                        "lng": float(lng_str),
                        "address": poi.get("address"),
                        "rating": float(rating) if rating else None,
                        "rating_count": None,
                        "rating_source": RatingSource.AMAP.value,
                        "avg_cost_rmb": float(cost) if cost else None,
                        "opening_hours_text": poi.get("business_hours") or None,
                    }
                )
            return results

        raw_list = self.cache.get_or_compute("amap.poi", (keyword, city), compute)
        out = []
        for raw in raw_list:
            raw = dict(raw)
            raw["rating_source"] = RatingSource(raw["rating_source"])
            out.append(POIDetails(**raw))
        return out

    def travel_time(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> TravelEstimate:
        path = _AMAP_MODE_PATH.get(mode, "walking")

        def compute() -> dict:
            o_lat, o_lng = origin
            d_lat, d_lng = destination
            payload = self._get(
                f"direction/{path}",
                {"origin": f"{o_lng},{o_lat}", "destination": f"{d_lng},{d_lat}", "city": city or ""},
            )
            route = payload["route"]
            path_info = (route.get("paths") or route.get("transits") or [{}])[0]
            duration_s = float(path_info.get("duration", 0))
            distance_m = float(path_info.get("distance", 0))
            transfers = len(path_info.get("segments", [])) if "transits" in route else 0
            return {
                "duration_minutes": duration_s / 60.0,
                "distance_km": distance_m / 1000.0,
                "mode": mode.value,
                "transfers": transfers,
                "cost_rmb": None,
            }

        raw = self.cache.get_or_compute("amap.route", (origin, destination, mode.value, city), compute)
        raw = dict(raw)
        raw["mode"] = TransportMode(raw["mode"])
        return TravelEstimate(**raw)

    def weather(self, city: str, date: Optional[str] = None) -> Optional[dict]:
        def compute() -> Optional[dict]:
            payload = self._get("weather/weatherInfo", {"city": city, "extensions": "all"})
            return payload.get("forecasts")

        return self.cache.get_or_compute("amap.weather", (city, date), compute)


# --------------------------------------------------------------------------
# Google Maps - preferred provider for non-China destinations
# --------------------------------------------------------------------------

_GOOGLE_MODE = {
    TransportMode.WALKING: "walking",
    TransportMode.DRIVING: "driving",
    TransportMode.TAXI: "driving",
    TransportMode.TRANSIT: "transit",
    TransportMode.SUBWAY: "transit",
    # Google's Directions API has no single "mixed" mode; transit routing
    # already blends walking + transit legs, same rationale as AMapProvider.
    TransportMode.MIXED: "transit",
}


class GoogleMapsProvider(MapProvider, POIDataProvider):
    """Non-China map/POI provider backed by the Google Maps Platform REST
    APIs (Geocoding, Places Text Search, Directions, Distance Matrix).

    Requires the ``GOOGLE_MAPS_API_KEY`` environment variable. Never
    hard-code the key in source. Real HTTP calls via stdlib ``urllib`` (no
    ``requests`` dependency), each wrapped in ``Cache`` - not exercised by
    the offline test suite (see mock_maps.MockProvider for that).

    Google's Places API returns a 0-4 ``price_level``, not a currency
    amount, so ``avg_cost_rmb`` is intentionally left unset here rather than
    mapped to a fake RMB figure - a real integration should surface
    ``price_level`` in its own currency-agnostic field instead.
    """

    BASE_URL = "https://maps.googleapis.com/maps/api"

    def __init__(self, api_key: Optional[str] = None, cache: Optional[Cache] = None, timeout: float = 5.0) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            raise ProviderConfigError(
                "GoogleMapsProvider requires GOOGLE_MAPS_API_KEY to be set as an environment "
                "variable (never hard-code API keys in source)."
            )
        self.cache = cache or Cache()
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "key": self.api_key}
        url = f"{self.BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface as ProviderError
            raise ProviderError(f"Google Maps request failed: {exc}") from exc
        status = payload.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            raise ProviderError(f"Google Maps API error: {status} - {payload.get('error_message', '')}")
        return payload

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        def compute() -> Optional[dict]:
            query = f"{address}, {city}" if city else address
            payload = self._get("geocode/json", {"address": query})
            results = payload.get("results") or []
            if not results:
                return None
            best = results[0]
            location = best["geometry"]["location"]
            return {
                "lat": float(location["lat"]),
                "lng": float(location["lng"]),
                "formatted_address": best.get("formatted_address", address),
            }

        raw = self.cache.get_or_compute("google.geocode", (address, city), compute)
        return GeocodeResult(**raw) if raw else None

    def search_poi(self, keyword: str, city: Optional[str] = None) -> list[POIDetails]:
        def compute() -> list[dict]:
            query = f"{keyword} in {city}" if city else keyword
            payload = self._get("place/textsearch/json", {"query": query})
            results = []
            for poi in payload.get("results", []):
                location = poi["geometry"]["location"]
                results.append(
                    {
                        "name": poi.get("name", keyword),
                        "lat": float(location["lat"]),
                        "lng": float(location["lng"]),
                        "address": poi.get("formatted_address"),
                        "rating": float(poi["rating"]) if poi.get("rating") is not None else None,
                        "rating_count": poi.get("user_ratings_total"),
                        "rating_source": RatingSource.GOOGLE.value,
                        "avg_cost_rmb": None,
                        "opening_hours_text": None,
                    }
                )
            return results

        raw_list = self.cache.get_or_compute("google.poi", (keyword, city), compute)
        out = []
        for raw in raw_list:
            raw = dict(raw)
            raw["rating_source"] = RatingSource(raw["rating_source"])
            out.append(POIDetails(**raw))
        return out

    def travel_time(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> TravelEstimate:
        google_mode = _GOOGLE_MODE.get(mode, "walking")

        def compute() -> dict:
            o_lat, o_lng = origin
            d_lat, d_lng = destination
            payload = self._get(
                "directions/json",
                {"origin": f"{o_lat},{o_lng}", "destination": f"{d_lat},{d_lng}", "mode": google_mode},
            )
            routes = payload.get("routes") or []
            if not routes:
                raise ProviderError("Google Maps returned no route for this origin/destination/mode")
            leg = routes[0]["legs"][0]
            transfers = 0
            if google_mode == "transit":
                transit_steps = [s for s in leg.get("steps", []) if s.get("travel_mode") == "TRANSIT"]
                transfers = max(0, len(transit_steps) - 1)
            return {
                "duration_minutes": leg["duration"]["value"] / 60.0,
                "distance_km": leg["distance"]["value"] / 1000.0,
                "mode": mode.value,
                "transfers": transfers,
                "cost_rmb": None,
            }

        raw = self.cache.get_or_compute("google.route", (origin, destination, mode.value, city), compute)
        raw = dict(raw)
        raw["mode"] = TransportMode(raw["mode"])
        return TravelEstimate(**raw)

    def distance_matrix(
        self,
        origins: list[tuple[float, float]],
        destinations: list[tuple[float, float]],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> list[list[TravelEstimate]]:
        """Batch version using Google's real Distance Matrix API - one
        request covers the whole origins x destinations grid instead of the
        pairwise fallback in the base ``MapProvider`` class."""

        google_mode = _GOOGLE_MODE.get(mode, "walking")

        def compute() -> list[list[Optional[dict]]]:
            payload = self._get(
                "distancematrix/json",
                {
                    "origins": "|".join(f"{lat},{lng}" for lat, lng in origins),
                    "destinations": "|".join(f"{lat},{lng}" for lat, lng in destinations),
                    "mode": google_mode,
                },
            )
            grid: list[list[Optional[dict]]] = []
            for row in payload.get("rows", []):
                row_out = []
                for element in row.get("elements", []):
                    if element.get("status") != "OK":
                        row_out.append(None)
                        continue
                    row_out.append(
                        {
                            "duration_minutes": element["duration"]["value"] / 60.0,
                            "distance_km": element["distance"]["value"] / 1000.0,
                            "mode": mode.value,
                            "transfers": 0,
                            "cost_rmb": None,
                        }
                    )
                grid.append(row_out)
            return grid

        raw_grid = self.cache.get_or_compute(
            "google.matrix", (tuple(origins), tuple(destinations), mode.value, city), compute
        )
        result: list[list[TravelEstimate]] = []
        for r, row in enumerate(raw_grid):
            out_row = []
            for c, cell in enumerate(row):
                if cell is None:
                    # Fall back to a direct pairwise call for the one cell
                    # Google couldn't route (e.g. no transit link) rather
                    # than dropping it from the grid.
                    out_row.append(self.travel_time(origins[r], destinations[c], mode, city))
                    continue
                cell = dict(cell)
                cell["mode"] = TransportMode(cell["mode"])
                out_row.append(TravelEstimate(**cell))
            result.append(out_row)
        return result

    def weather(self, city: str, date: Optional[str] = None) -> Optional[dict]:
        # Google Maps Platform has no bundled weather API (unlike AMap) - a
        # real integration would need a separate weather provider/key.
        return None


class MapboxProvider(MapProvider):
    """Placeholder adapter; not implemented in V1."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("MAPBOX_API_KEY")

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        raise NotImplementedError("MapboxProvider is a placeholder for a future release")

    def travel_time(self, origin, destination, mode, city=None) -> TravelEstimate:
        raise NotImplementedError("MapboxProvider is a placeholder for a future release")


# --------------------------------------------------------------------------
# Provider auto-selection: AMap for China, Google Maps for everywhere else
# --------------------------------------------------------------------------

# Mainland China's bounding box (deliberately loose - this only decides which
# provider to *try first*; CompositeMapProvider falls back to the other one
# automatically, so being imprecise near the border costs a wasted call, not
# a wrong answer).
_CHINA_LAT_RANGE = (17.0, 54.0)
_CHINA_LNG_RANGE = (72.0, 136.0)

# Name hints for the "which provider should we try first" guess, used before
# any coordinates are known (e.g. the very first geocode call for a
# destination/hotel string). Deliberately short and mainland-focused - it's
# a first guess, not a classifier; a wrong guess just costs one extra
# fallback call, so this list doesn't need to be exhaustive.
_CHINA_NAME_HINTS = {
    "china", "中国", "prc",
    "beijing", "北京", "shanghai", "上海", "guangzhou", "广州", "shenzhen", "深圳",
    "chengdu", "成都", "hangzhou", "杭州", "xian", "西安", "chongqing", "重庆",
    "nanjing", "南京", "suzhou", "苏州", "wuhan", "武汉", "xiamen", "厦门",
    "guilin", "桂林", "qingdao", "青岛", "tianjin", "天津", "harbin", "哈尔滨",
}


def _in_china_bbox(coords: tuple[float, float]) -> bool:
    lat, lng = coords
    return _CHINA_LAT_RANGE[0] <= lat <= _CHINA_LAT_RANGE[1] and _CHINA_LNG_RANGE[0] <= lng <= _CHINA_LNG_RANGE[1]


def looks_like_china(text: Optional[str]) -> bool:
    """Best-effort guess from a destination/city/address string. Only used
    to pick which provider to try *first* - never the only signal, since
    ``CompositeMapProvider`` falls back to the other provider regardless."""

    if not text:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in _CHINA_NAME_HINTS)


class CompositeMapProvider(MapProvider, POIDataProvider):
    """Routes each call to whichever of AMap / Google Maps is the better fit,
    with automatic fallback to the other one.

    - ``geocode`` / ``search_poi`` (no coordinates yet): guess from the
      ``city`` string via :func:`looks_like_china`.
    - ``travel_time`` (coordinates already known): use the actual
      coordinates via the mainland-China bounding box, which is more
      reliable than any name heuristic.

    Either underlying provider may be omitted (e.g. only one API key is
    configured) - calls simply go to whichever one exists.
    """

    def __init__(self, amap: Optional[AMapProvider] = None, google: Optional[GoogleMapsProvider] = None) -> None:
        if amap is None and google is None:
            raise ProviderConfigError("CompositeMapProvider needs at least one of amap= / google= configured")
        self._amap = amap
        self._google = google

    def _ordered_by_city(self, city: Optional[str]) -> list[MapProvider]:
        prefer_amap = looks_like_china(city)
        primary, secondary = (self._amap, self._google) if prefer_amap else (self._google, self._amap)
        return [p for p in (primary, secondary) if p is not None]

    def _ordered_by_coords(self, *coords: tuple[float, float]) -> list[MapProvider]:
        prefer_amap = all(_in_china_bbox(c) for c in coords)
        primary, secondary = (self._amap, self._google) if prefer_amap else (self._google, self._amap)
        return [p for p in (primary, secondary) if p is not None]

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        for provider in self._ordered_by_city(city):
            try:
                result = provider.geocode(address, city)
            except ProviderError:
                continue
            if result is not None:
                return result
        return None

    def search_poi(self, keyword: str, city: Optional[str] = None) -> list[POIDetails]:
        for provider in self._ordered_by_city(city):
            if not isinstance(provider, POIDataProvider):
                continue
            try:
                results = provider.search_poi(keyword, city)
            except ProviderError:
                continue
            if results:
                return results
        return []

    def travel_time(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> TravelEstimate:
        last_error: Optional[ProviderError] = None
        for provider in self._ordered_by_coords(origin, destination):
            try:
                return provider.travel_time(origin, destination, mode, city)
            except ProviderError as exc:
                last_error = exc
        raise last_error or ProviderError("No map provider is configured")

    def weather(self, city: str, date: Optional[str] = None) -> Optional[dict]:
        for provider in self._ordered_by_city(city):
            try:
                result = provider.weather(city, date)
            except ProviderError:
                continue
            if result:
                return result
        return None


def build_default_provider() -> MapProvider:
    """Convenience factory: build whichever of AMap/Google Maps have API
    keys configured, wrapped in a ``CompositeMapProvider`` so China
    destinations prefer AMap and everything else prefers Google Maps, each
    falling back to the other automatically. Routing is decided per call
    (from the ``city``/coordinates passed in), not from a fixed destination
    at construction time - one instance works across an entire multi-city
    trip if needed.

    Raises ``ProviderConfigError`` if neither ``AMAP_API_KEY`` nor
    ``GOOGLE_MAPS_API_KEY`` is set - callers that want a safe offline
    default should catch this and fall back to
    ``mock_maps.MockProvider()`` instead.
    """

    amap: Optional[AMapProvider] = None
    google: Optional[GoogleMapsProvider] = None
    try:
        amap = AMapProvider()
    except ProviderConfigError:
        pass
    try:
        google = GoogleMapsProvider()
    except ProviderConfigError:
        pass
    if amap is None and google is None:
        raise ProviderConfigError(
            "build_default_provider requires AMAP_API_KEY and/or GOOGLE_MAPS_API_KEY to be set."
        )
    return CompositeMapProvider(amap=amap, google=google)


class DianpingReviewProvider(ReviewDataProvider):
    """Placeholder. We do not scrape Dianping. A real implementation would
    need to be built on an official/partner API with a valid data license.
    """

    def get_rating(self, name: str, city: Optional[str] = None) -> Optional[POIDetails]:
        raise NotImplementedError(
            "DianpingReviewProvider requires an official partner API; scraping is out of scope."
        )


class MeituanReviewProvider(ReviewDataProvider):
    """Placeholder. We do not scrape Meituan. See DianpingReviewProvider."""

    def get_rating(self, name: str, city: Optional[str] = None) -> Optional[POIDetails]:
        raise NotImplementedError(
            "MeituanReviewProvider requires an official partner API; scraping is out of scope."
        )


@dataclass
class ProviderBundle:
    """Convenience grouping so planner.py only needs to pass one object around."""

    map_provider: MapProvider
    poi_provider: Optional[POIDataProvider] = None
    review_provider: Optional[ReviewDataProvider] = None

    def __post_init__(self) -> None:
        if self.poi_provider is None and isinstance(self.map_provider, POIDataProvider):
            self.poi_provider = self.map_provider
