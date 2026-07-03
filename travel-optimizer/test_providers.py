"""Tests for provider auto-selection (CompositeMapProvider / looks_like_china
/ build_default_provider). Uses small in-memory stub providers instead of
real AMap/Google Maps calls - GoogleMapsProvider and AMapProvider themselves
require network access and a real API key, so they're exercised manually,
not in this offline suite.

Run with: pytest test_providers.py -v
"""

from __future__ import annotations

from typing import Optional

import pytest

from models import TransportMode
from providers import (
    CompositeMapProvider,
    GeocodeResult,
    ProviderConfigError,
    ProviderError,
    TravelEstimate,
    build_default_provider,
    looks_like_china,
)


class StubProvider:
    """Minimal MapProvider/POIDataProvider stand-in that records every call
    it receives and returns a canned (possibly empty/erroring) result."""

    def __init__(self, name: str, geocode_result=None, poi_results=None, travel_estimate=None, raises=False):
        self.name = name
        self._geocode_result = geocode_result
        self._poi_results = poi_results or []
        self._travel_estimate = travel_estimate
        self._raises = raises
        self.calls: list[str] = []

    def geocode(self, address, city=None):
        self.calls.append(f"geocode:{address}")
        if self._raises:
            raise ProviderError(f"{self.name} is down")
        return self._geocode_result

    def search_poi(self, keyword, city=None):
        self.calls.append(f"search_poi:{keyword}")
        if self._raises:
            raise ProviderError(f"{self.name} is down")
        return self._poi_results

    def travel_time(self, origin, destination, mode, city=None):
        self.calls.append(f"travel_time:{origin}->{destination}")
        if self._raises:
            raise ProviderError(f"{self.name} is down")
        return self._travel_estimate

    def weather(self, city, date=None):
        return None


def _make_composite(amap: Optional[StubProvider], google: Optional[StubProvider]) -> CompositeMapProvider:
    return CompositeMapProvider(amap=amap, google=google)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# looks_like_china
# --------------------------------------------------------------------------

def test_looks_like_china_recognizes_common_cities():
    assert looks_like_china("Beijing")
    assert looks_like_china("北京")
    assert looks_like_china("Shanghai, China")


def test_looks_like_china_false_for_other_cities_and_none():
    assert not looks_like_china("Tokyo")
    assert not looks_like_china("Paris")
    assert not looks_like_china(None)
    assert not looks_like_china("")


# --------------------------------------------------------------------------
# CompositeMapProvider: city-string routing (geocode / search_poi)
# --------------------------------------------------------------------------

def test_geocode_prefers_amap_for_china_city():
    amap = StubProvider("amap", geocode_result=GeocodeResult(39.9, 116.4, "Beijing"))
    google = StubProvider("google", geocode_result=GeocodeResult(0.0, 0.0, "wrong"))
    composite = _make_composite(amap, google)

    result = composite.geocode("故宫", city="Beijing")

    assert result is not None and result.lat == 39.9
    assert amap.calls and not google.calls


def test_geocode_prefers_google_for_non_china_city():
    amap = StubProvider("amap", geocode_result=GeocodeResult(0.0, 0.0, "wrong"))
    google = StubProvider("google", geocode_result=GeocodeResult(35.6, 139.7, "Tokyo"))
    composite = _make_composite(amap, google)

    result = composite.geocode("Senso-ji", city="Tokyo")

    assert result is not None and result.lat == 35.6
    assert google.calls and not amap.calls


def test_geocode_falls_back_when_preferred_provider_finds_nothing():
    amap = StubProvider("amap", geocode_result=None)  # AMap has no data for this address
    google = StubProvider("google", geocode_result=GeocodeResult(1.0, 2.0, "found via google"))
    composite = _make_composite(amap, google)

    result = composite.geocode("some obscure place", city="Beijing")  # China-preferred, but AMap comes up empty

    assert result is not None and result.formatted_address == "found via google"
    assert amap.calls and google.calls  # both were tried


def test_geocode_falls_back_when_preferred_provider_errors():
    amap = StubProvider("amap", raises=True)
    google = StubProvider("google", geocode_result=GeocodeResult(1.0, 2.0, "via google"))
    composite = _make_composite(amap, google)

    result = composite.geocode("anything", city="Beijing")

    assert result is not None and result.formatted_address == "via google"


# --------------------------------------------------------------------------
# CompositeMapProvider: coordinate-based routing (travel_time)
# --------------------------------------------------------------------------

def test_travel_time_routes_by_coordinates_not_city_string():
    beijing_estimate = TravelEstimate(10.0, 2.0, TransportMode.DRIVING)
    tokyo_estimate = TravelEstimate(20.0, 5.0, TransportMode.DRIVING)
    amap = StubProvider("amap", travel_estimate=beijing_estimate)
    google = StubProvider("google", travel_estimate=tokyo_estimate)
    composite = _make_composite(amap, google)

    # Coordinates are in Tokyo even though no city string is passed - the
    # bounding-box check should still route to Google, not AMap.
    result = composite.travel_time((35.6, 139.7), (35.7, 139.8), TransportMode.DRIVING)

    assert result.duration_minutes == 20.0
    assert google.calls and not amap.calls


def test_travel_time_raises_when_both_providers_fail():
    amap = StubProvider("amap", raises=True)
    google = StubProvider("google", raises=True)
    composite = _make_composite(amap, google)

    with pytest.raises(ProviderError):
        composite.travel_time((39.9, 116.4), (39.95, 116.45), TransportMode.DRIVING)


# --------------------------------------------------------------------------
# Single-provider configurations still work
# --------------------------------------------------------------------------

def test_composite_works_with_only_one_provider_configured():
    google = StubProvider("google", geocode_result=GeocodeResult(1.0, 2.0, "solo google"))
    composite = _make_composite(amap=None, google=google)

    result = composite.geocode("anything", city="Beijing")  # China-preferred, but AMap isn't configured at all

    assert result is not None and result.formatted_address == "solo google"


def test_composite_requires_at_least_one_provider():
    with pytest.raises(ProviderConfigError):
        CompositeMapProvider(amap=None, google=None)


# --------------------------------------------------------------------------
# build_default_provider
# --------------------------------------------------------------------------

def test_build_default_provider_raises_without_any_api_key(monkeypatch):
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    with pytest.raises(ProviderConfigError):
        build_default_provider()


def test_build_default_provider_succeeds_with_one_key(monkeypatch):
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    provider = build_default_provider()

    assert isinstance(provider, CompositeMapProvider)
