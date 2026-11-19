# bot/utils/querytools.py
"""Чистые функции: радиус и категория в тексте inline-запроса, классификация заведения."""

import re
from typing import NamedTuple

KINDS = ("all", "cafe", "restaurant", "bar")
# Радиусы inline «прилипают» к этим значениям — иначе кэш по точке дробится на сотни ключей.
RADIUS_BUCKETS = (200, 500, 1000, 2000, 3000)

_RADIUS_RE = re.compile(
    r"^(?:r)?(\d+(?:[.,]\d+)?)\s*(км|km|k|м|m)?$",
    re.IGNORECASE,
)

# Слова-фильтры категории в запросе (ru/en/vi/th/zh). Только токен целиком.
_KIND_WORDS: dict[str, str] = {
    "кафе": "cafe", "кофе": "cafe", "кофейня": "cafe", "cafe": "cafe", "café": "cafe",
    "coffee": "cafe", "caphe": "cafe", "กาแฟ": "cafe", "咖啡": "cafe",
    "ресторан": "restaurant", "рестораны": "restaurant", "еда": "restaurant",
    "restaurant": "restaurant", "food": "restaurant",
    "อาหาร": "restaurant", "餐厅": "restaurant",
    "бар": "bar", "бары": "bar", "паб": "bar", "bar": "bar", "pub": "bar", "bars": "bar",
    "บาร์": "bar", "酒吧": "bar",
}

# Классификация по названию категории провайдера (FSQ — на языке запроса; OSM — наши метки).
# Порядок важен: «Wine Bar», «Coffee Bar» → bar/cafe проверяются раньше «restaurant».
_BAR_RE = re.compile(
    r"\b(bar|pub|brewery|cocktail|lounge|nightclub|beer|wine|бар|паб|пивн\w*|коктейл\w*|винн\w*)\b"
    r"|酒吧|啤酒|บาร์|ผับ|quán bar|quán nhậu",
    re.IGNORECASE,
)
_CAFE_RE = re.compile(
    r"\b(cafe|café|coffee|tea|bakery|dessert|кафе|кофейн\w*|кофе|чайн\w*|пекарн\w*|кондитерск\w*)\b"
    r"|咖啡|茶|甜品|面包|กาแฟ|คาเฟ่|ชา|cà phê|cafe|quán nước|trà",
    re.IGNORECASE,
)
_RESTAURANT_RE = re.compile(
    r"\b(restaurant|diner|bistro|eatery|fast food|pizza\w*|burger|noodle|sushi|grill|"
    r"steakhouse|bbq|pho|food|snack|ресторан\w*|столов\w*|пиццер\w*|фастфуд|бистро|суши|"
    r"шашлы\w*|закусочн\w*)\b"
    r"|餐|饭|面|ร้านอาหาร|อาหาร|nhà hàng|quán ăn|phở|bún|cơm",
    re.IGNORECASE,
)


def classify_kind(place: dict) -> str:
    """'cafe' | 'restaurant' | 'bar' | 'other' по primary_type места."""
    t = str(place.get("primary_type") or "")
    if not t:
        return "other"
    if _BAR_RE.search(t):
        return "bar"
    if _CAFE_RE.search(t):
        return "cafe"
    if _RESTAURANT_RE.search(t):
        return "restaurant"
    return "other"


def matches_kind(place: dict, kind: str) -> bool:
    return kind == "all" or classify_kind(place) == kind


def parse_radius(token: str) -> int | None:
    """'1км', '500м', '1.5km', 'r2000' → метры; иначе None. Голое число не считаем радиусом."""
    m = _RADIUS_RE.match(token.strip())
    if not m:
        return None
    num, unit = m.group(1), (m.group(2) or "").casefold()
    if not unit and not token.strip().casefold().startswith("r"):
        return None
    value = float(num.replace(",", "."))
    meters = value * 1000 if unit in ("км", "km", "k") else value
    return int(meters) if meters >= 50 else None


def snap_radius(radius: int, max_radius: int = RADIUS_BUCKETS[-1]) -> int:
    """Ближайший сверху бакет (не теряем места), не больше max_radius."""
    cap = min(max_radius, RADIUS_BUCKETS[-1])
    for b in RADIUS_BUCKETS:
        if radius <= b:
            return min(b, cap)
    return cap


class ParsedQuery(NamedTuple):
    radius: int | None  # None — не указан
    kind: str  # 'all' если не указана
    rest: str  # остаток текста (название / адрес / координаты)


def parse_query(text: str) -> ParsedQuery:
    """Вырезает токены радиуса и категории; остальное — для фильтра по названию/геокодера."""
    radius: int | None = None
    kind = "all"
    rest: list[str] = []
    for tok in (text or "").split():
        r = parse_radius(tok)
        if r is not None and radius is None:
            radius = r
            continue
        k = _KIND_WORDS.get(tok.casefold().replace("ё", "е").strip(",.;"))
        if k is not None and kind == "all":
            kind = k
            continue
        rest.append(tok)
    return ParsedQuery(radius, kind, " ".join(rest))
