"""Deterministic offline provider for tests and local development.

``MockProvider`` implements the same interfaces as ``AMapProvider`` (see
providers.py) but never touches the network. It ships a small built-in
dataset of real Beijing coordinates (plus a Tokyo set for exercising the
international / non-China flow) so clustering/planning logic can be
exercised end-to-end without an API key.

Travel times are derived from haversine distance with mode-specific average
speeds and overhead - good enough to make clustering/feasibility decisions
deterministic in tests, not meant to be geographically precise.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

from models import OpeningHours, DayHours, RatingSource, TransportMode
from providers import (
    Cache,
    GeocodeResult,
    MapProvider,
    POIDataProvider,
    POIDetails,
    ReviewDataProvider,
    TravelEstimate,
)


@dataclass
class _MockRecord:
    canonical_name: str
    aliases: tuple[str, ...]
    lat: float
    lng: float
    address: str
    rating: Optional[float]
    rating_count: Optional[int]
    avg_cost_rmb: Optional[float]
    opening_hours: OpeningHours


def _hours(open_h: int, open_m: int, close_h: int, close_m: int, closed_weekday: Optional[int] = None,
           last_entry_offset: int = 60) -> OpeningHours:
    by_weekday = {wd: DayHours(dt.time(open_h, open_m), dt.time(close_h, close_m)) for wd in range(7)}
    if closed_weekday is not None:
        by_weekday[closed_weekday] = DayHours(None, None)
    return OpeningHours(by_weekday=by_weekday, last_entry_offset_minutes=last_entry_offset, default_open=True)


# A small, realistic-enough Beijing dataset covering two natural geographic
# clusters (central Beijing, and the Summer Palace / 798 area) plus one
# genuine outlier (Mutianyu Great Wall, ~70km out) used to exercise
# "far but must-visit gets its own day" logic.
_PLACES: list[_MockRecord] = [
    _MockRecord(
        "故宫 Forbidden City", ("故宫", "forbidden city", "the forbidden city"),
        39.9163, 116.3972, "景山前街4号", 4.8, 210000, 60.0,
        _hours(8, 30, 17, 0, closed_weekday=0, last_entry_offset=60),
    ),
    _MockRecord(
        "景山公园 Jingshan Park", ("景山公园", "jingshan park"),
        39.9279, 116.3971, "景山西街44号", 4.7, 45000, 2.0,
        _hours(6, 30, 21, 0, last_entry_offset=30),
    ),
    _MockRecord(
        "北海公园 Beihai Park", ("北海公园", "beihai park"),
        39.9257, 116.3900, "文津街1号", 4.6, 38000, 10.0,
        _hours(6, 30, 20, 0, last_entry_offset=30),
    ),
    _MockRecord(
        "南锣鼓巷 Nanluoguxiang", ("南锣鼓巷", "nanluoguxiang"),
        39.9366, 116.4032, "南锣鼓巷", None, None, None,
        _hours(0, 0, 23, 59),
    ),
    _MockRecord(
        "天坛 Temple of Heaven", ("天坛", "temple of heaven"),
        39.8822, 116.4066, "天坛路甲1号", 4.7, 90000, 35.0,
        _hours(8, 0, 17, 30, closed_weekday=0, last_entry_offset=60),
    ),
    _MockRecord(
        "四季民福烤鸭店(故宫店)", ("四季民福", "sijimingfu", "sijimingfu roast duck"),
        39.9175, 116.4010, "北池子大街", 4.6, 12000, 120.0,
        _hours(10, 30, 14, 0, last_entry_offset=0),
    ),
    _MockRecord(
        "文宇奶酪店", ("文宇奶酪店", "wenyu cheese shop"),
        39.9370, 116.4040, "南锣鼓巷福祥胡同", 4.4, 8000, 25.0,
        _hours(10, 0, 22, 0),
    ),
    _MockRecord(
        "烤肉季(什刹海店)", ("烤肉季", "kaorouji"),
        39.9410, 116.3850, "什刹海前海东沿", 4.3, 5000, 150.0,
        _hours(11, 0, 21, 0),
    ),
    _MockRecord(
        "颐和园 Summer Palace", ("颐和园", "summer palace"),
        39.9998, 116.2755, "新建宫门路19号", 4.8, 160000, 30.0,
        _hours(6, 30, 18, 0, last_entry_offset=60),
    ),
    _MockRecord(
        "798艺术区 798 Art District", ("798艺术区", "798 art district", "798"),
        39.9843, 116.4980, "酒仙桥路4号", 4.5, 30000, 0.0,
        _hours(9, 0, 18, 0),
    ),
    _MockRecord(
        "颐和园附近餐厅", ("颐和园附近餐厅", "restaurant near summer palace"),
        39.9970, 116.2790, "苏州街", 4.2, 3000, 90.0,
        _hours(11, 0, 21, 30),
    ),
    _MockRecord(
        "慕田峪长城 Mutianyu Great Wall", ("慕田峪长城", "mutianyu", "mutianyu great wall"),
        40.4319, 116.5704, "怀柔区渤海镇", 4.7, 60000, 100.0,
        _hours(7, 30, 17, 0, last_entry_offset=60),
    ),
    # ---- Tokyo (international flow demo/tests; coordinates fall outside the
    # mainland-China bounding box so CompositeMapProvider-style routing and
    # the rest of the pipeline can be exercised offline for a non-China trip)
    _MockRecord(
        "浅草寺 Senso-ji", ("浅草寺", "senso-ji", "sensoji", "asakusa temple"),
        35.7148, 139.7967, "东京都台东区浅草2-3-1", 4.7, 140000, 0.0,
        _hours(6, 0, 17, 0, last_entry_offset=30),
    ),
    _MockRecord(
        "明治神宫 Meiji Shrine", ("明治神宫", "meiji shrine", "meiji jingu"),
        35.6764, 139.6993, "东京都涩谷区代代木神园町1-1", 4.6, 75000, 0.0,
        _hours(5, 0, 18, 0, last_entry_offset=30),
    ),
    _MockRecord(
        "东京塔 Tokyo Tower", ("东京塔", "tokyo tower"),
        35.6586, 139.7454, "东京都港区芝公园4-2-8", 4.5, 90000, 180.0,
        _hours(9, 0, 22, 30, last_entry_offset=60),
    ),
    _MockRecord(
        "涩谷十字路口 Shibuya Crossing", ("涩谷十字路口", "涩谷", "shibuya crossing", "shibuya"),
        35.6595, 139.7005, "东京都涩谷区道玄坂", 4.4, 50000, 0.0,
        _hours(0, 0, 23, 59),
    ),
    _MockRecord(
        "teamLab Planets", ("teamlab planets", "teamlab", "丰洲teamlab"),
        35.6494, 139.7898, "东京都江东区丰洲6-1-16", 4.6, 40000, 230.0,
        _hours(9, 0, 20, 0, last_entry_offset=60),
    ),
    _MockRecord(
        "新宿御苑 Shinjuku Gyoen", ("新宿御苑", "shinjuku gyoen"),
        35.6852, 139.7100, "东京都新宿区内藤町11", 4.6, 60000, 25.0,
        _hours(9, 0, 17, 30, closed_weekday=0, last_entry_offset=60),
    ),
    _MockRecord(
        "一兰拉面(涩谷店) Ichiran Shibuya", ("一兰拉面", "ichiran", "ichiran shibuya"),
        35.6590, 139.7010, "东京都涩谷区神南1-22-7", 4.3, 30000, 60.0,
        _hours(0, 0, 23, 59),
    ),
    _MockRecord(
        "筑地场外市场 Tsukiji Outer Market", ("筑地场外市场", "筑地市场", "tsukiji outer market", "tsukiji"),
        35.6654, 139.7707, "东京都中央区筑地4丁目", 4.4, 55000, 100.0,
        _hours(5, 0, 14, 0, last_entry_offset=0),
    ),
    _MockRecord(
        "新宿酒店 Shinjuku Hotel", ("新宿酒店", "shinjuku hotel", "hotel shinjuku"),
        35.6896, 139.7006, "东京都新宿区西新宿", None, None, None,
        _hours(0, 0, 23, 59),
    ),
]

_HOTEL_DEFAULT = _MockRecord(
    "王府井酒店 Hotel", ("hotel", "王府井酒店", "start", "hotel start"),
    39.9139, 116.4110, "王府井大街", None, None, None, _hours(0, 0, 23, 59),
)


def _find_record(name: str) -> Optional[_MockRecord]:
    from extract_places import normalize_place_name

    key = normalize_place_name(name)
    if not key:
        return None

    # Exact alias match wins outright. Falling straight through to substring
    # matching would let a short alias like "颐和园" hijack the lookup for
    # "颐和园附近餐厅" (a different record whose name merely contains it) -
    # among substring matches, prefer the longest/most specific alias.
    best: Optional[tuple[int, _MockRecord]] = None
    for record in [*_PLACES, _HOTEL_DEFAULT]:
        for candidate in (record.canonical_name, *record.aliases):
            ck = normalize_place_name(candidate)
            if key == ck:
                return record
            if key in ck or ck in key:
                specificity = len(ck)
                if best is None or specificity > best[0]:
                    best = (specificity, record)
    return best[1] if best else None


_MODE_SPEED_KMH = {
    TransportMode.WALKING: 4.5,
    TransportMode.DRIVING: 22.0,
    TransportMode.TAXI: 22.0,
    TransportMode.SUBWAY: 26.0,
    TransportMode.TRANSIT: 24.0,
    TransportMode.MIXED: 24.0,
}
_MODE_OVERHEAD_MIN = {
    TransportMode.WALKING: 0.0,
    TransportMode.DRIVING: 3.0,
    TransportMode.TAXI: 6.0,
    TransportMode.SUBWAY: 10.0,
    TransportMode.TRANSIT: 10.0,
    TransportMode.MIXED: 8.0,
}
_DETOUR_FACTOR = 1.3


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = a
    lat2, lng2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


class MockProvider(MapProvider, POIDataProvider, ReviewDataProvider):
    """Fully offline stand-in for AMapProvider. Deterministic and free."""

    def __init__(self, cache: Optional[Cache] = None) -> None:
        self.cache = cache or Cache()

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[GeocodeResult]:
        record = _find_record(address)
        if record is None:
            return None
        return GeocodeResult(lat=record.lat, lng=record.lng, formatted_address=record.address)

    def search_poi(self, keyword: str, city: Optional[str] = None) -> list[POIDetails]:
        record = _find_record(keyword)
        if record is None:
            return []
        return [self._to_poi_details(record)]

    def get_rating(self, name: str, city: Optional[str] = None) -> Optional[POIDetails]:
        record = _find_record(name)
        if record is None:
            return None
        return self._to_poi_details(record)

    @staticmethod
    def _to_poi_details(record: _MockRecord) -> POIDetails:
        return POIDetails(
            name=record.canonical_name,
            lat=record.lat,
            lng=record.lng,
            address=record.address,
            rating=record.rating,
            rating_count=record.rating_count,
            rating_source=RatingSource.MOCK if record.rating is not None else RatingSource.UNKNOWN,
            avg_cost_rmb=record.avg_cost_rmb,
        )

    def opening_hours_for(self, name: str) -> Optional[OpeningHours]:
        record = _find_record(name)
        return record.opening_hours if record else None

    def travel_time(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: TransportMode,
        city: Optional[str] = None,
    ) -> TravelEstimate:
        def compute() -> dict:
            straight_km = haversine_km(origin, destination)
            road_km = straight_km * _DETOUR_FACTOR
            speed = _MODE_SPEED_KMH[mode]
            overhead = _MODE_OVERHEAD_MIN[mode]
            duration = (road_km / speed) * 60 + overhead
            transfers = 0
            if mode in (TransportMode.SUBWAY, TransportMode.TRANSIT, TransportMode.MIXED):
                transfers = max(0, math.floor(road_km / 8))
                duration += transfers * 5
            cost = None
            if mode in (TransportMode.TAXI, TransportMode.DRIVING):
                cost = round(12 + road_km * 2.8, 1)
            elif mode in (TransportMode.SUBWAY, TransportMode.TRANSIT):
                cost = 5.0 if road_km <= 12 else 7.0
            return {
                "duration_minutes": round(duration, 1),
                "distance_km": round(road_km, 2),
                "mode": mode.value,
                "transfers": transfers,
                "cost_rmb": cost,
            }

        raw = self.cache.get_or_compute(
            "mock.route", (round(origin[0], 5), round(origin[1], 5), round(destination[0], 5),
                            round(destination[1], 5), mode.value), compute,
        )
        raw = dict(raw)
        raw["mode"] = TransportMode(raw["mode"])
        return TravelEstimate(**raw)

    def weather(self, city: str, date: Optional[str] = None) -> Optional[dict]:
        return {"city": city, "date": date, "forecast": "sunny", "high_c": 28, "low_c": 18, "source": "mock"}
