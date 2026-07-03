"""Turn messy Xiaohongshu screenshots / copied text into Place candidates.

This module is intentionally regex/heuristic based (no ML model, no network
calls) so it runs offline and deterministically. It is meant to get "close
enough" structured data that a human or an LLM agent driving this skill can
quickly correct, not to be a perfect NLP pipeline.
"""

from __future__ import annotations

import re
import uuid
from difflib import SequenceMatcher

from models import Place, PlaceCategory, PriorityLevel

# --------------------------------------------------------------------------
# Note/tag detection
# --------------------------------------------------------------------------

# Each tag maps to bilingual keyword fragments commonly seen in Xiaohongshu
# posts and casual user notes. Matching is substring-based on normalized text.
NOTE_KEYWORDS: dict[str, list[str]] = {
    "reservation_required": ["预约", "需预约", "reservation", "book ahead", "需要预定", "需要预约"],
    "morning_only": ["早上去", "建议早上", "morning only", "上午去", "clears by noon", "人少的时候是早上"],
    "sunset": ["日落", "夕阳", "sunset", "黄昏"],
    "closed_mondays": ["周一闭馆", "周一休息", "closed monday", "周一不开"],
    "long_queue": ["排队", "排长队", "长队", "queue", "wait in line", "人巨多", "人超多"],
    "far_from_center": ["远", "郊区", "far from city", "far from center", "车程较远", "偏远"],
    "must_try": ["必吃", "必点", "must try", "网红", "招牌", "必点菜"],
    "avoid_weekend": ["避开周末", "周末人多", "avoid weekend", "周末别去"],
}

CATEGORY_KEYWORDS: dict[PlaceCategory, list[str]] = {
    PlaceCategory.MUSEUM: ["博物馆", "美术馆", "museum", "gallery", "展览馆"],
    PlaceCategory.PARK: ["公园", "park", "风景区", "scenic area"],
    PlaceCategory.TEMPLE: ["寺", "庙", "temple", "church", "教堂", "观"],
    PlaceCategory.OLD_STREET: ["老街", "胡同", "古镇", "old street", "shopping district", "步行街"],
    PlaceCategory.OBSERVATION_DECK: ["观景台", "瞭望台", "observation deck", "塔"],
    PlaceCategory.THEME_PARK: ["主题公园", "theme park", "乐园", "环球影城", "迪士尼"],
    PlaceCategory.SHOPPING: ["商场", "购物中心", "mall", "shopping", "市场", "market"],
    PlaceCategory.CAFE: ["咖啡", "cafe", "coffee", "甜品店", "奶茶"],
    PlaceCategory.RESTAURANT: ["餐厅", "饭店", "restaurant", "小吃", "火锅", "烤鸭", "菜馆", "食堂"],
    PlaceCategory.HOTEL: ["酒店", "民宿", "hotel", "hostel"],
    PlaceCategory.LANDMARK: ["故宫", "landmark", "地标", "广场", "square"],
}

# A small alias table for well-known landmarks so common Chinese/English
# name pairs (as they appear mixed together in Xiaohongshu copy) are
# recognized as duplicates even though the strings themselves share no
# characters. Currently covers Beijing plus a few Tokyo examples - extend
# per city as new markets are added; for the long tail, an agent driving
# this skill should merge bilingual pairs itself during classification.
_ALIAS_GROUPS: list[set[str]] = [
    # Beijing
    {"故宫", "故宫博物院", "forbiddencity", "theforbiddencity"},
    {"天坛", "天坛公园", "templeofheaven"},
    {"颐和园", "summerpalace"},
    {"南锣鼓巷", "nanluoguxiang"},
    {"长城", "万里长城", "greatwall", "thegreatwall"},
    {"慕田峪长城", "mutianyu", "mutianyugreatwall"},
    {"北海公园", "beihaipark"},
    {"景山公园", "jingshanpark"},
    {"天安门", "天安门广场", "tiananmensquare", "tiananmen"},
    {"798艺术区", "798artdistrict", "798"},
    # Tokyo
    {"浅草寺", "sensoji", "asakusatemple"},
    {"明治神宫", "meijishrine", "meijijingu"},
    {"东京塔", "tokyotower"},
    {"涩谷十字路口", "涩谷", "shibuyacrossing", "shibuya"},
    {"新宿御苑", "shinjukugyoen"},
    {"筑地场外市场", "筑地市场", "tsukijioutermarket", "tsukiji"},
    {"一兰拉面", "ichiran"},
]


def _alias_key_hits(key: str) -> set[int]:
    """Indices of alias groups this normalized key belongs to. Besides exact
    membership, a key also hits a group when it contains (or is contained
    by) a member - so "sensojitemple" still matches the "sensoji" group.
    Substring hits require the shorter string to be reasonably long (3+
    chars for CJK members, 4+ otherwise) to avoid accidental matches."""

    hits: set[int] = set()
    for i, group in enumerate(_ALIAS_GROUPS):
        for member in group:
            if key == member:
                hits.add(i)
                break
            shorter = min(member, key, key=len)
            min_len = 3 if _looks_cjk(shorter) else 4
            if len(shorter) >= min_len and (member in key or key in member):
                hits.add(i)
                break
    return hits


def _alias_match(key_a: str, key_b: str) -> bool:
    return bool(_alias_key_hits(key_a) & _alias_key_hits(key_b))


BULLET_PREFIX_RE = re.compile(r"^[\s\-\*•‣◦⁃∙#\d\.\)．）]+")
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
PAREN_NOTE_RE = re.compile(r"[\(（]([^\)）]{1,40})[\)）]")


def _clean_line(line: str) -> str:
    line = BULLET_PREFIX_RE.sub("", line)
    line = EMOJI_RE.sub("", line)
    return line.strip(" \t、,，:：-")


def normalize_place_name(name: str) -> str:
    """Return a comparison key: strip whitespace/punctuation/case for de-duping."""

    key = EMOJI_RE.sub("", name)
    key = re.sub(r"[\s\-_/·・,，。.!！?？'\"()（）]+", "", key)
    return key.lower()


def _detect_tags(text: str) -> set[str]:
    tags: set[str] = set()
    lowered = text.lower()
    for tag, keywords in NOTE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lowered:
                tags.add(tag)
                break
    return tags


def _detect_category(text: str) -> PlaceCategory:
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return category
    return PlaceCategory.UNKNOWN


def _split_name_and_note(line: str) -> tuple[str, list[str]]:
    """Pull parenthetical asides out as notes, keep the rest as the name."""

    notes = PAREN_NOTE_RE.findall(line)
    name = PAREN_NOTE_RE.sub("", line).strip()
    # Common separators between a place name and a trailing free-text note,
    # e.g. "南锣鼓巷 - 人超多建议早上去" or "Cafe X: must try the latte".
    for sep in [" - ", "—", "：", ":"]:
        if sep in name:
            head, _, tail = name.partition(sep)
            if head.strip():
                notes.append(tail.strip())
                name = head.strip()
            break
    return name.strip(), [n for n in notes if n]


def extract_places_from_text(raw_text: str) -> list[Place]:
    """Parse free-form copied text (Xiaohongshu notes, lists, etc.) into Places.

    Each non-empty line is treated as one candidate place. Lines that are
    clearly headers/prose with no place-like content (too long, no nouns)
    are skipped heuristically by requiring the cleaned line be reasonably
    short (<= 60 chars) once notes are stripped out.
    """

    places: list[Place] = []
    for raw_line in raw_text.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        name, extra_notes = _split_name_and_note(line)
        if not name or len(name) > 60:
            continue

        combined_text_for_tags = " ".join([line, *extra_notes])
        tags = _detect_tags(combined_text_for_tags)
        category = _detect_category(line)

        place = Place(
            id=str(uuid.uuid4()),
            name=name,
            category=category,
            priority=PriorityLevel.OPTIONAL,
            notes=extra_notes,
            tags=tags,
            reservation_required="reservation_required" in tags,
            source_text=raw_line.strip(),
        )
        places.append(place)
    return merge_duplicate_places(places)


def _names_similar(a: str, b: str, threshold: float = 0.82) -> bool:
    key_a, key_b = normalize_place_name(a), normalize_place_name(b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    # One name containing the other handles "故宫" vs "故宫博物院" style overlap,
    # and bilingual pairs sharing a common romanized/CJK substring.
    if key_a in key_b or key_b in key_a:
        return True
    if _alias_match(key_a, key_b):
        return True
    return SequenceMatcher(None, key_a, key_b).ratio() >= threshold


def merge_duplicate_places(places: list[Place]) -> list[Place]:
    """Merge near-duplicate entries (including CN/EN pairs for the same spot).

    Two places are considered duplicates if their normalized names are
    similar AND (neither has coordinates, or their coordinates are close).
    Merging keeps the richer record and folds in notes/tags from both.
    """

    merged: list[Place] = []
    for place in places:
        match = None
        for existing in merged:
            if not _names_similar(existing.name, place.name):
                continue
            if existing.coordinates and place.coordinates:
                # ~150m: same-looking names that are clearly different locations
                # (e.g. two different "Old Street"s in different cities) should
                # not be merged just because the strings match loosely.
                lat_close = abs(existing.lat - place.lat) < 0.0015  # type: ignore[operator]
                lng_close = abs(existing.lng - place.lng) < 0.0015  # type: ignore[operator]
                if not (lat_close and lng_close):
                    continue
            match = existing
            break

        if match is None:
            if _looks_cjk(place.name) and not place.name_cn:
                place.name_cn = place.name
            if _looks_english(place.name) and not place.name_en:
                place.name_en = place.name
            merged.append(place)
            continue

        # Fold `place` into `match`, preferring whichever field is populated.
        match.notes = list(dict.fromkeys([*match.notes, *place.notes]))
        match.tags |= place.tags
        match.reservation_required = match.reservation_required or place.reservation_required
        if match.category == PlaceCategory.UNKNOWN and place.category != PlaceCategory.UNKNOWN:
            match.category = place.category
        if _looks_english(place.name) and not match.name_en:
            match.name_en = place.name
        if _looks_cjk(place.name) and not match.name_cn:
            match.name_cn = place.name
        if not match.lat and place.lat:
            match.lat, match.lng = place.lat, place.lng
        if match.source_text and place.source_text and place.source_text not in match.source_text:
            match.source_text = f"{match.source_text} | {place.source_text}"

    return merged


def _looks_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and not _looks_cjk(text)
