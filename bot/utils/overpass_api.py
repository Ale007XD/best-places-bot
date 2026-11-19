# bot/utils/overpass_api.py
"""
OpenStreetMap через Overpass API: бесплатный фолбэк без ключа и кредитов.

Даёт название, тип, адрес и координаты заведений, но НЕ рейтинги — такие места
показываются «ближайшими» с пометкой (см. places_service.select_places).

Публичные серверы перегружаются (502/429/таймауты), а правила их использования просят:
не гонять параллельные запросы к одному серверу, кэшировать, не злоупотреблять. Поэтому:
  • адреса задаются списком (OVERPASS_URL через запятую) и пробуются СТРОГО ПО ОЧЕРЕДИ;
  • сервер, который не ответил, уходит «на паузу» (_COOLDOWN_SEC) — следующие поиски
    пропускают его, а не ждут таймаут заново (раньше один сбой = +10 секунд на каждый поиск);
  • если паузу взяли все серверы, Overpass пропускается мгновенно;
  • общий бюджет времени на поиск ограничен (_TOTAL_BUDGET_SEC).
Данные © участники OpenStreetMap (ODbL) — бот указывает это в выдаче.
"""

import logging
import time
from typing import Any

import httpx

from bot.utils.http import get_client

logger = logging.getLogger(__name__)

_TYPES = {
    "restaurant": "Restaurant",
    "cafe": "Café",
    "bar": "Bar",
    "pub": "Pub",
    "fast_food": "Fast food",
}

_ATTEMPT_TIMEOUT_SEC = 7.0
_TOTAL_BUDGET_SEC = 14.0
_COOLDOWN_SEC = 90.0
_cooldown_until: dict[str, float] = {}


def cooldowns() -> dict[str, int]:
    """url → сколько секунд ещё на паузе (только активные) — для /diag."""
    now = time.monotonic()
    return {u: int(t - now) for u, t in _cooldown_until.items() if t > now}


def _normalize(el: dict[str, Any]) -> dict[str, Any] | None:
    tags = el.get("tags") or {}
    name = tags.get("name")
    center = el.get("center") or {}
    lat = el.get("lat", center.get("lat"))
    lon = el.get("lon", center.get("lon"))
    if not name or lat is None or lon is None:
        return None

    street = " ".join(
        x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x
    )
    return {
        "place_id": f"osm:{el.get('type')}/{el.get('id')}",
        "name": name,
        "rating": None,
        "user_ratings_total": 0,
        "primary_type": _TYPES.get(tags.get("amenity", ""), tags.get("amenity", "")),
        "lat": lat,
        "lon": lon,
        "vicinity": tags.get("addr:full") or street or None,
        "photo_url": tags["image"] if str(tags.get("image", "")).startswith("http") else None,
        "source": "osm",
    }


_KIND_AMENITIES = {
    "all": "restaurant|cafe|bar|pub|fast_food",
    "cafe": "cafe",
    "restaurant": "restaurant|fast_food",
    "bar": "bar|pub",
}


def build_query(lat: float, lon: float, radius: int, limit: int = 300, kind: str = "all") -> str:
    """Узлы и пути отдельно (не nwr): быстрее и без лишних отношений.

    Без сортировки Overpass отдаёт объекты по id (сначала узлы, потом пути), поэтому лимит
    обрезал бы произвольные места, а не дальние: он с запасом, ближайшие выбираем локально.
    """
    amenities = _KIND_AMENITIES.get(kind, _KIND_AMENITIES["all"])
    flt = f'["amenity"~"^({amenities})$"]["name"]'
    around = f"(around:{int(radius)},{lat},{lon})"
    return f"[out:json][timeout:8];(node{around}{flt};way{around}{flt};);out center {limit};"


async def query_server(url: str, query: str) -> list[dict[str, Any]] | None:
    """Один сервер. None — ошибка (в отличие от успешного, но пустого ответа)."""
    try:
        r = await get_client().post(
            url,
            data={"data": query},
            headers={"User-Agent": "best-places-bot/1.0"},
            timeout=_ATTEMPT_TIMEOUT_SEC,
        )
        r.raise_for_status()
        return r.json().get("elements", [])
    except (httpx.HTTPError, ValueError) as e:
        # у ReadTimeout str(e) пустой — без имени класса в логе не видно причины
        logger.warning("Overpass %s failed: %s", url, str(e) or type(e).__name__)
        return None


async def find_places_overpass(
    lat: float, lon: float, radius: int, url: str, limit: int = 300, kind: str = "all"
) -> list[dict[str, Any]] | None:
    """Заведения вокруг точки. None — сбой (все зеркала на паузе / не ответили), [] — мест нет.

    url — один адрес или несколько через запятую (зеркала, пробуются по порядку).
    """
    urls = [u.strip() for u in url.split(",") if u.strip()]
    now = time.monotonic()
    active = [u for u in urls if _cooldown_until.get(u, 0.0) <= now]
    if not active:
        logger.info("Overpass: all mirrors are cooling down — skipped")
        return None

    query = build_query(lat, lon, radius, limit, kind)
    started = time.monotonic()
    elements: list[dict[str, Any]] | None = None
    for u in active:
        if time.monotonic() - started > _TOTAL_BUDGET_SEC:
            break
        elements = await query_server(u, query)
        if elements is not None:
            _cooldown_until.pop(u, None)
            break
        _cooldown_until[u] = time.monotonic() + _COOLDOWN_SEC

    if elements is None:
        logger.error("Overpass: all %s tried servers failed", len(active))
        return None

    places = [p for el in elements if (p := _normalize(el))]
    logger.info("Overpass: %s places", len(places))
    return places
