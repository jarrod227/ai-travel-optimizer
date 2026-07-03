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
  * ``GoogleMapsProvider`` / ``MapboxProvider`` - unimplemented placeholders
    documenting the same interface for future markets.
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
# Placeholder adapters for future markets / providers
# --------------------------------------------------------------------------

class GoogleMapsProvider(MapProvider, POIDataProvider):
    """Placeholder for a future non-China rollout. Not implemented in V1.

    Kept here so the provider abstraction is visibly not AMap-only; wiring
    this up later should only require implementing the methods below using
    ``GOOGLE_MAPS_API_KEY``.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        raise NotImplementedError("GoogleMapsProvider is a placeholder for a future release")

    def travel_time(self, origin, destination, mode, city=None) -> TravelEstimate:
        raise NotImplementedError("GoogleMapsProvider is a placeholder for a future release")

    def search_poi(self, keyword: str, city: Optional[str] = None) -> list[POIDetails]:
        raise NotImplementedError("GoogleMapsProvider is a placeholder for a future release")


class MapboxProvider(MapProvider):
    """Placeholder adapter; not implemented in V1."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("MAPBOX_API_KEY")

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        raise NotImplementedError("MapboxProvider is a placeholder for a future release")

    def travel_time(self, origin, destination, mode, city=None) -> TravelEstimate:
        raise NotImplementedError("MapboxProvider is a placeholder for a future release")


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
