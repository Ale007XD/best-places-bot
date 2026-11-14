# bot/utils/places_service.py
"""
Оркестратор поиска.

Flow:
1. Кэш сырого списка (по точке/радиусу/языку — НЕ по рейтингу).
2. Foursquare: один запрос на все категории.
3. Только если Foursquare вернул пусто (сбой/нет данных) — аварийные
   Mapbox и VietMap (если заданы ключи). Такие результаты не кэшируются.
4. Дедупликация (нормализованное имя + близость).
5. Локальный фильтр по минимальному рейтингу и ранжирование.
   Если мест с нужным рейтингом меньше TOP_N, добираются лучшие из остальных
   (флаг relaxed), чтобы пользователь не упирался в пустой ответ.
"""

import hashlib
import json
import logging
import re
from typing import Any, NamedTuple

from redis.exceptions import RedisError

from bot.config import settings
from bot.utils.foursquare_api import fetch_places as fsq_fetch
from bot.utils.geospatial import calculate_distance
from bot.utils.mapbox_api import find_places_mapbox
from bot.utils.vietmap_api import find_places_vietmap

logger = logging.getLogger(__name__)

CACHE_TTL = 600  # 10 минут
TOP_N = 3  # сколько мест показываем
RESULT_LIMIT = 10  # сколько возвращаем наверх (с запасом)
DEDUP_DISTANCE_M = 75


class SearchResult(NamedTuple):
    places: list[dict[str, Any]]
    relaxed: bool  # не хватило мест с нужным рейтингом — добавлены лучшие из остальных
    rating_available: bool  # есть ли хоть у одного места рейтинг


def _make_cache_key(lat: float, lon: float, radius: int, lang_code: str) -> str:
    raw = f"{round(lat, 4)}:{round(lon, 4)}:{radius}:{lang_code}"
    return f"places:v2:{hashlib.md5(raw.encode()).hexdigest()}"


def _norm_name(name: Any) -> str:
    return re.sub(r"[\W_]+", "", str(name or "").casefold())


def _deduplicate(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убирает дубли: то же имя (без регистра/пунктуации) и ближе DEDUP_DISTANCE_M.

    Из дублей остаётся запись с рейтингом (приоритет у Foursquare — он идёт первым).
    """
    result: list[dict[str, Any]] = []
    for p in places:
        name = _norm_name(p.get("name"))
        dup_idx = None
        for i, q in enumerate(result):
            if name != _norm_name(q.get("name")):
                continue
            if None in (p.get("lat"), p.get("lon"), q.get("lat"), q.get("lon")):
                dup_idx = i
                break
            d = calculate_distance(
                float(p["lat"]), float(p["lon"]), float(q["lat"]), float(q["lon"])
            )
            if d <= DEDUP_DISTANCE_M:
                dup_idx = i
                break
        if dup_idx is None:
            result.append(p)
        elif result[dup_idx].get("rating") is None and p.get("rating") is not None:
            result[dup_idx] = p
    return result


def _score(p: dict[str, Any], user_lat: float, user_lon: float, radius: int) -> float:
    """0.7 · рейтинг (нормирован на 0–1) + 0.3 · близость (линейно по радиусу)."""
    rating = float(p.get("rating") or 0.0) / 5.0
    if p.get("lat") is None or p.get("lon") is None or radius <= 0:
        return 0.7 * rating
    dist = calculate_distance(user_lat, user_lon, float(p["lat"]), float(p["lon"]))
    return 0.7 * rating + 0.3 * max(0.0, 1 - dist / radius)


def _within_radius(p: dict[str, Any], lat: float, lon: float, radius: int) -> bool:
    if p.get("lat") is None or p.get("lon") is None:
        return True
    d = calculate_distance(lat, lon, float(p["lat"]), float(p["lon"]))
    return d <= radius


def select_places(
    raw: list[dict[str, Any]],
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
) -> SearchResult:
    """Чистая функция: дедуп → радиус → фильтр по рейтингу → ранжирование."""
    places = [
        p for p in _deduplicate(raw) if _within_radius(p, lat, lon, radius)
    ]

    def rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda p: _score(p, lat, lon, radius), reverse=True)

    rated = [p for p in places if p.get("rating") is not None]

    # Рейтингов нет вовсе (поле недоступно / фолбэк-провайдеры) — просто ближайшие.
    if not rated:
        return SearchResult(rank(places)[:RESULT_LIMIT], False, False)

    if min_rating <= 0:
        unrated = [p for p in places if p.get("rating") is None]
        return SearchResult(
            (rank(rated) + rank(unrated))[:RESULT_LIMIT], False, True
        )

    strict = rank([p for p in rated if p["rating"] >= min_rating])
    relaxed = False
    if len(strict) < TOP_N:
        rest = rank([p for p in rated if p["rating"] < min_rating])
        extra = rest[: TOP_N - len(strict)]
        relaxed = bool(extra)
        strict = strict + extra
    return SearchResult(strict[:RESULT_LIMIT], relaxed, True)


async def _load_raw(
    lat: float, lon: float, radius: int, lang_code: str, redis_conn
) -> list[dict[str, Any]]:
    cache_key = _make_cache_key(lat, lon, radius, lang_code)

    try:
        cached = await redis_conn.get(cache_key)
        if cached:
            logger.info("CACHE HIT")
            return json.loads(cached)
    except (RedisError, ValueError) as e:
        logger.warning("Cache read failed: %s", e)

    raw = await fsq_fetch(
        settings.FSQ_API_KEY,
        settings.FSQ_API_VERSION,
        lat,
        lon,
        radius,
        lang_code,
    )

    if raw:
        try:
            await redis_conn.setex(cache_key, CACHE_TTL, json.dumps(raw))
        except (RedisError, TypeError) as e:
            logger.warning("Cache write failed: %s", e)
        return raw

    # Foursquare пуст: аварийные провайдеры (без рейтинга, не кэшируются).
    logger.warning("FSQ returned nothing — trying emergency providers")
    if settings.MAPBOX_TOKEN:
        raw += await find_places_mapbox(
            lat=lat,
            lon=lon,
            radius=radius,
            limit=10,
            lang_code=lang_code,
            access_token=settings.MAPBOX_TOKEN,
        )
    if settings.VIETMAP_API_KEY and len(raw) < TOP_N:
        raw += await find_places_vietmap(
            lat=lat, lon=lon, radius=radius, api_key=settings.VIETMAP_API_KEY
        )
    return raw


async def search_places(
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    lang_code: str,
    redis_conn,
) -> SearchResult:
    raw = await _load_raw(lat, lon, radius, lang_code, redis_conn)
    return select_places(raw, lat, lon, radius, min_rating)
