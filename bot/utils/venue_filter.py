# bot/utils/venue_filter.py
"""
Фильтр ложных срабатываний провайдеров (контуры 1 и 2).

Контур 1: конфликт названия с гастро-выдачей (стоп-слова нецелевых бизнесов при
отсутствии гастро-якорей — например, «TT Pilates Nha Trang», «CLB HOANG GIA»).
Контур 2: конфликт сырых категорий Geoapify и тегов OSM (переиспользованные узлы,
где остался старый amenity=restaurant, но добавлен sport=*/leisure=fitness_centre).
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from bot.utils.textnorm import fold

logger = logging.getLogger(__name__)

# Подмножество тегов OSM, которое сохраняется в кэше и проверяется в контуре 2.
_RELEVANT_OSM_KEYS = (
    "cuisine",
    "sport",
    "healthcare",
    "office",
    "shop",
    "tourism",
    "leisure",
    "amenity",
)

# Контур 1: стоп-слова нецелевых бизнесов (проверяются по целым словам после fold()).
NON_FOOD_TOKENS: frozenset[str] = frozenset({
    # Спорт / фитнес
    "pilates", "yoga", "gym", "fitness", "crossfit", "zumba", "boxing", "clb",
    "йога", "йоги",
    # Красота / здоровье
    "spa", "massage", "nail", "nails", "salon", "barber", "barbershop",
    "beauty", "aesthetic", "clinic", "dental", "dentist", "hospital",
    "pharmacy", "drugstore", "optician", "optical", "tattoo", "piercing",
    "спа", "салон",
    # Жильё
    "hotel", "hostel", "homestay", "resort", "villa", "motel",
    "apartment", "apartments", "condotel", "residence",
    "отель", "отеля",
    # Ритейл и сервисы
    "mart", "minimart", "supermarket", "grocery", "laundry", "tailor",
    "tour", "tours", "travel", "visa", "agency", "bank", "atm", "exchange",
    "school", "academy", "university", "kindergarten", "office", "coworking",
    "karaoke", "billiards", "billiard", "snooker", "bowling", "gaming",
    "garage", "repair", "carwash", "vet", "veterinary", "florist",
    "jewelry", "jewellery", "electronics", "furniture", "hardware",
    "bookstore", "stationery", "printing", "photography",
    "church", "pagoda", "temple", "museum", "cinema", "theater", "theatre",
})

# Кириллические основы (учёт падежей: «пилатеса», «фитнеса», «массажа» и т. п.).
NON_FOOD_CYR_PREFIXES: tuple[str, ...] = (
    "пилатес",
    "фитнес",
    "массаж",
    "клиник",
    "стоматолог",
    "аптек",
    "гостиниц",
    "хостел",
)

# Многословные стоп-фразы (Вьетнам / ЮВА + EN).
NON_FOOD_PHRASES: tuple[str, ...] = (
    "nha khoa",
    "phong kham",
    "nha thuoc",
    "giat la",
    "giat ui",
    "real estate",
    "bat dong san",
    "khach san",
    "nha nghi",
    "tham my",
    "cat toc",
    "lam dep",
    "the hinh",
    "fitness club",
    "health club",
    "sports club",
    "pilates studio",
    "yoga studio",
    "dance studio",
    "nail studio",
    "photo studio",
    "tattoo studio",
    "quan net",
)

# Гастро-якоря: при наличии хотя бы одного место не режется (например, «Yoga Cafe»).
FOOD_ANCHOR_TOKENS: frozenset[str] = frozenset({
    "cafe", "coffee", "coffeeshop", "espresso", "roastery", "roasters",
    "restaurant", "bistro", "brasserie", "trattoria", "osteria", "pizzeria",
    "bar", "pub", "tavern", "lounge", "taproom", "brewery", "izakaya",
    "kitchen", "diner", "eatery", "canteen", "cantina",
    "bakery", "patisserie", "boulangerie", "gelato", "gelateria",
    "tea", "teahouse", "boba", "pizza", "sushi", "ramen", "udon", "soba",
    "kebab", "doner", "shawarma", "taco", "tacos", "burger", "burgers",
    "steak", "steakhouse", "bbq", "grill", "seafood", "noodle", "noodles",
    "pasta", "curry", "biryani", "dumpling", "dumplings",
    "wine", "cocktail", "cocktails", "food", "eat", "eats",
    "vegan", "vegetarian", "chay",
    "pho", "bun", "com", "banh", "lau", "nuong", "chao", "xoi", "kem", "bia",
    "кафе", "кофейня", "ресторан", "бар", "паб", "бистро", "пекарня",
    "пиццерия", "пицца", "суши", "столовая", "шашлык", "гриль",
})

FOOD_ANCHOR_PHRASES: tuple[str, ...] = (
    "ca phe",
    "nha hang",
    "hai san",
    "tra sua",
    "tra chanh",
    "banh mi",
    "banh xeo",
    "com tam",
    "bun bo",
    "bun cha",
    "hu tieu",
    "quan an",
    "quan nhau",
    "quan oc",
    "quan pho",
    "quan bun",
    "quan com",
    "quan ca phe",
    "ice cream",
    "milk tea",
    "fast food",
    "street food",
    "hot pot",
    "dim sum",
)

# Контур 2: конфликтующие префиксы categories в Geoapify.
CONFLICT_GEOAPIFY_PREFIXES: tuple[str, ...] = (
    "sport.",
    "healthcare.",
    "accommodation.",
    "education.",
    "service.",
    "office.",
    "religion.",
    "childcare.",
    "pet.",
    "leisure.fitness",
    "entertainment.cinema",
    "entertainment.museum",
    "tourism.sights",
    "tourism.attraction",
    "commercial.health_and_beauty",
    "commercial.supermarket",
    "commercial.convenience",
    "commercial.clothing",
    "commercial.electronics",
    "commercial.houseware_and_hardware",
    "commercial.gift_and_souvenir",
    "commercial.books",
    "commercial.jewelry",
    "commercial.florist",
    "commercial.vehicle",
)

_FOOD_SHOPS: frozenset[str] = frozenset({
    "bakery", "coffee", "tea", "pastry", "confectionery",
    "ice_cream", "beverages", "alcohol", "wine", "deli",
    "chocolate", "cheese", "seafood", "food",
})

_CONFLICT_TOURISM: frozenset[str] = frozenset({
    "hotel", "hostel", "motel", "guest_house", "apartment",
    "resort", "museum", "gallery", "attraction", "information",
})

_CONFLICT_LEISURE: frozenset[str] = frozenset({
    "fitness_centre", "sports_centre", "stadium", "pitch",
    "swimming_pool", "sauna", "amusement_arcade", "bowling_alley",
    "escape_game", "dance",
})

_CONFLICT_AMENITY: frozenset[str] = frozenset({
    "clinic", "dentist", "doctors", "hospital", "pharmacy",
    "school", "college", "university", "kindergarten", "language_school",
    "driving_school", "bank", "atm", "bureau_de_change", "place_of_worship",
    "cinema", "theatre", "karaoke_box", "car_wash", "fuel", "parking",
    "post_office", "police", "townhall", "community_centre", "veterinary",
})


def extract_osm_tags(raw: Mapping[str, Any] | None) -> dict[str, str]:
    """Оставляет только теги OSM, нужные для контура 2 (не раздувает кэш Redis)."""
    if not raw:
        return {}
    return {k: str(raw[k]) for k in _RELEVANT_OSM_KEYS if raw.get(k)}


def _has_phrase(spaced: str, phrases: tuple[str, ...]) -> bool:
    return any(f" {p} " in spaced for p in phrases)


def has_food_anchor(name: str) -> bool:
    folded = fold(name)
    if not folded:
        return False
    if set(folded.split()) & FOOD_ANCHOR_TOKENS:
        return True
    return _has_phrase(f" {folded} ", FOOD_ANCHOR_PHRASES)


def has_non_food_signal(name: str) -> bool:
    folded = fold(name)
    if not folded:
        return False
    words = folded.split()
    if set(words) & NON_FOOD_TOKENS:
        return True
    if any(w.startswith(NON_FOOD_CYR_PREFIXES) for w in words):
        return True
    return _has_phrase(f" {folded} ", NON_FOOD_PHRASES)


def has_conflicting_tags(
    raw_categories: Sequence[str] | None = None,
    osm_tags: Mapping[str, str] | None = None,
) -> bool:
    """Контур 2: переиспользованный или смешанный узел Geoapify / OSM."""
    if osm_tags and osm_tags.get("cuisine"):
        return False

    if raw_categories:
        for cat in raw_categories:
            cat_l = cat.lower()
            if any(cat_l.startswith(prefix) for prefix in CONFLICT_GEOAPIFY_PREFIXES):
                return True

    if osm_tags:
        if osm_tags.get("sport") or osm_tags.get("healthcare") or osm_tags.get("office"):
            return True
        shop = osm_tags.get("shop", "").lower()
        if shop and shop not in _FOOD_SHOPS:
            return True
        if osm_tags.get("tourism", "").lower() in _CONFLICT_TOURISM:
            return True
        if osm_tags.get("leisure", "").lower() in _CONFLICT_LEISURE:
            return True
        if osm_tags.get("amenity", "").lower() in _CONFLICT_AMENITY:
            return True

    return False


def reject_reason(place: Mapping[str, Any]) -> str | None:
    """Причина отсева места или None, если проверка пройдена."""
    name = str(place.get("name") or "")
    if has_food_anchor(name):
        return None
    if has_non_food_signal(name):
        return "name_non_food"
    if has_conflicting_tags(
        raw_categories=place.get("raw_categories"),
        osm_tags=place.get("osm_tags"),
    ):
        return "tag_conflict"
    return None


def drop_non_food(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Отсекает нецелевые карточки ДО подсчёта провайдера."""
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for p in places:
        reason = reject_reason(p)
        if reason:
            dropped.append(f"{p.get('name')!r} ({reason})")
            continue
        kept.append(p)
    if dropped:
        logger.info("Non-food places dropped (%s): %s", len(dropped), "; ".join(dropped[:5]))
    return kept
