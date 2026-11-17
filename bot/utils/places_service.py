# bot/utils/places_service.py
"""
Оркестратор поиска.

Flow:
1. Кэш сырого списка (по точке/радиусу/языку — НЕ по рейтингу).
2. Foursquare: один запрос на все категории (при нехватке кредитов на Premium-поля —
   автоматически с базовыми, без рейтинга).
3. Если мест меньше TOP_N или Foursquare сбоит — дополняем OpenStreetMap (Overpass,
   бесплатно, без рейтинга). Mapbox/VietMap — только если совсем пусто и заданы ключи.
   Если Foursquare сбоит и ничего не найдено — SearchUnavailable (не «мест нет»).
4. Дедупликация (нормализованное имя + близость).
5. Локальный фильтр по минимальному рейтингу и ранжирование.
   Если мест с нужным рейтингом меньше TOP_N, добираются лучшие из остальных
   (флаг relaxed), чтобы пользователь не упирался в пустой ответ.
"""

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, NamedTuple

from redis.exceptions import RedisError

from bot.config import settings
from bot.services import community
from bot.utils.foursquare_api import FoursquareError, ratings_available
from bot.utils.foursquare_api import fetch_places as fsq_fetch
from bot.utils.geospatial import calculate_distance
from bot.utils.mapbox_api import find_places_mapbox
from bot.utils.overpass_api import find_places_overpass
from bot.utils.vietmap_api import find_places_vietmap

logger = logging.getLogger(__name__)

CACHE_TTL = 600  # 10 минут
FALLBACK_CACHE_TTL = 120  # если Foursquare сбоил — кэшируем ненадолго
TOP_N = 3  # сколько мест показываем
RESULT_LIMIT = 10  # сколько возвращаем наверх (с запасом)
DEDUP_DISTANCE_M = 75
# «Тосты» и «Тосты, винное бистро» — одно заведение с разным оформлением имени.
# Имена не равны, но ядра вложены друг в друга: допускаем больший разброс координат.
DEDUP_FUZZY_DISTANCE_M = 120
_GENERIC_WORDS = frozenset({
    "бар", "бистро", "кафе", "ресторан", "паб", "пиццерия", "кофейня", "столовая",
    "винный", "винное", "винотека", "cafe", "bar", "pub", "restaurant", "bistro",
    "wine", "coffee", "the", "и",
})


class SearchUnavailable(Exception):
    """Основной провайдер сбоит, а фолбэки ничего не нашли — это не «мест нет»."""


class SearchResult(NamedTuple):
    places: list[dict[str, Any]]
    relaxed: bool  # не хватило мест с нужным рейтингом — добавлены лучшие из остальных
    rating_available: bool  # есть ли хоть у одного места рейтинг
    from_cache: bool = False  # ответ из кэша — внешний API не вызывался (бесплатно)
    fsq_ok: bool = True  # основной провайдер ответил (для метрик качества)
    used_osm: bool = False  # в выдачу попали данные OpenStreetMap
    rated: bool = False  # в ответе есть рейтинги/фото провайдера (Premium-уровень полей)


class LoadInfo(NamedTuple):
    raw: list[dict[str, Any]]
    from_cache: bool
    fsq_ok: bool = True
    used_osm: bool = False
    rated: bool = False


def _make_cache_key(
    lat: float, lon: float, radius: int, lang_code: str, variant: str = "r"
) -> str:
    """variant: "r" — с рейтингами и фото (Premium-поля), "b" — базовый уровень."""
    raw = f"{round(lat, 4)}:{round(lon, 4)}:{radius}:{lang_code}:{variant}"
    return f"places:v3:{hashlib.md5(raw.encode()).hexdigest()}"


def _strip_ratings(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Скрывает платные данные Foursquare (рейтинг, число оценок, фото) в копиях мест."""
    out = []
    for p in raw:
        if p.get("source") == "fsq":
            p = {**p, "rating": None, "user_ratings_total": 0, "photo_url": None}
        out.append(p)
    return out


def _norm_name(name: Any) -> str:
    return re.sub(r"[\W_]+", "", str(name or "").casefold())


def _core_tokens(name: Any) -> frozenset[str]:
    """Значимые слова названия без «бар/бистро/кафе…» (для нечёткого сравнения)."""
    toks = re.findall(r"[^\W_]+", str(name or "").casefold().replace("ё", "е"))
    return frozenset([t for t in toks if t not in _GENERIC_WORDS] or toks)


def _fuzzy_same_name(a: Any, b: Any) -> bool:
    ta, tb = _core_tokens(a), _core_tokens(b)
    return bool(ta and tb and (ta <= tb or tb <= ta))


def _deduplicate(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убирает дубли: то же имя (без регистра/пунктуации) и ближе DEDUP_DISTANCE_M.

    Из дублей остаётся запись с рейтингом (приоритет у Foursquare — он идёт первым).
    """
    result: list[dict[str, Any]] = []
    for p in places:
        name = _norm_name(p.get("name"))
        dup_idx = None
        for i, q in enumerate(result):
            exact = name == _norm_name(q.get("name"))
            if not exact and not _fuzzy_same_name(p.get("name"), q.get("name")):
                continue
            limit = DEDUP_DISTANCE_M if exact else DEDUP_FUZZY_DISTANCE_M
            # Два разных объекта одного провайдера (разные id) с ОДИНАКОВЫМ именем — это
            # разные заведения (сеть в одном здании). Разные оформления имени — дубль.
            if (
                exact
                and p.get("source") == q.get("source")
                and p.get("place_id")
                and q.get("place_id")
                and p["place_id"] != q["place_id"]
            ):
                continue
            if None in (p.get("lat"), p.get("lon"), q.get("lat"), q.get("lon")):
                dup_idx = i
                break
            d = calculate_distance(
                float(p["lat"]), float(p["lon"]), float(q["lat"]), float(q["lon"])
            )
            if d <= limit:
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
    if len(strict) < TOP_N:
        # Мест с рейтингом мало (редкий район / часть данных из OSM) — добираем ближайшими.
        unrated = rank([p for p in places if p.get("rating") is None])
        extra = unrated[: TOP_N - len(strict)]
        relaxed = relaxed or bool(extra)
        strict = strict + extra
    return SearchResult(strict[:RESULT_LIMIT], relaxed, True)


_inflight: dict[str, asyncio.Lock] = {}


async def _read_cache(redis_conn, key: str) -> list[dict[str, Any]] | None:
    try:
        cached = await redis_conn.get(key)
        if cached:
            return json.loads(cached)
    except (RedisError, ValueError) as e:
        logger.warning("Cache read failed: %s", e)
    return None


async def _cached(redis_conn, key_r: str, key_b: str, want_rated: bool) -> LoadInfo | None:
    """
    Запрос с рейтингами читает только «богатый» кэш. Базовый запрос читает свой кэш,
    а при его отсутствии — богатый (рейтинги и фото при этом скрываются).
    """
    if want_rated:
        raw = await _read_cache(redis_conn, key_r)
        return LoadInfo(raw, True, rated=True) if raw is not None else None
    raw = await _read_cache(redis_conn, key_b)
    if raw is not None:
        return LoadInfo(raw, True)
    raw = await _read_cache(redis_conn, key_r)
    return LoadInfo(_strip_ratings(raw), True) if raw is not None else None


async def _fetch_and_cache(
    key_r: str,
    key_b: str,
    lat: float,
    lon: float,
    radius: int,
    lang_code: str,
    redis_conn,
    want_rated: bool,
) -> LoadInfo:
    fsq_ok = True
    rated = False
    try:
        res = await fsq_fetch(
            settings.FSQ_API_KEY,
            settings.FSQ_API_VERSION,
            lat,
            lon,
            radius,
            lang_code,
            want_ratings=want_rated,
        )
        raw, rated = res.places, res.rated
    except FoursquareError as e:
        logger.warning("Foursquare failed (%s) — using fallbacks", e)
        raw, fsq_ok = [], False

    # Мало данных (редкий район или сбой FSQ) — дополняем бесплатным OSM.
    used_osm = False
    if len(raw) < TOP_N and settings.OVERPASS_ENABLED:
        osm = await find_places_overpass(lat, lon, radius, settings.OVERPASS_URL)
        used_osm = bool(osm)
        raw = raw + osm

    # Совсем пусто — аварийные провайдеры с ключами, если заданы.
    if not raw and settings.MAPBOX_TOKEN:
        raw += await find_places_mapbox(
            lat=lat,
            lon=lon,
            radius=radius,
            limit=10,
            lang_code=lang_code,
            access_token=settings.MAPBOX_TOKEN,
        )
    if not raw and settings.VIETMAP_API_KEY:
        raw += await find_places_vietmap(
            lat=lat, lon=lon, radius=radius, api_key=settings.VIETMAP_API_KEY
        )

    if not raw and not fsq_ok:
        raise SearchUnavailable("Foursquare failed and fallbacks returned nothing")

    if raw:
        try:
            ttl = CACHE_TTL if fsq_ok else FALLBACK_CACHE_TTL
            await redis_conn.setex(key_r if rated else key_b, ttl, json.dumps(raw))
        except (RedisError, TypeError) as e:
            logger.warning("Cache write failed: %s", e)
    return LoadInfo(raw, False, fsq_ok, used_osm, rated)


async def _load_raw(
    lat: float,
    lon: float,
    radius: int,
    lang_code: str,
    redis_conn,
    with_ratings: bool = True,
) -> LoadInfo:
    """
    Кэш → (single-flight) Foursquare/фолбэки. Одинаковые параллельные запросы с промахом
    кэша не плодят вызовы API: первый ходит во внешний API, остальные ждут и берут кэш.
    """
    key_r = _make_cache_key(lat, lon, radius, lang_code, "r")
    key_b = _make_cache_key(lat, lon, radius, lang_code, "b")
    # Если Premium-поля сейчас недоступны (нет кредитов) — это обычный базовый запрос.
    want_rated = with_ratings and ratings_available()

    hit = await _cached(redis_conn, key_r, key_b, want_rated)
    if hit is not None:
        logger.info("CACHE HIT (%s)", "rated" if want_rated else "base")
        return hit

    if len(_inflight) > 1000:  # не копим замки бесконечно
        for k in [k for k, lk in _inflight.items() if not lk.locked()]:
            del _inflight[k]
    lock = _inflight.setdefault(key_r if want_rated else key_b, asyncio.Lock())
    async with lock:
        hit = await _cached(redis_conn, key_r, key_b, want_rated)  # мог заполнить первый
        if hit is not None:
            return hit
        return await _fetch_and_cache(
            key_r, key_b, lat, lon, radius, lang_code, redis_conn, want_rated
        )


async def search_places(
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    lang_code: str,
    redis_conn,
    with_ratings: bool = True,
) -> SearchResult:
    """with_ratings=False — базовая выдача без платных рейтингов/фото (бесплатный уровень)."""
    info = await _load_raw(lat, lon, radius, lang_code, redis_conn, with_ratings)
    # Отметки пользователей меняются постоянно — к кэшу провайдера не относятся.
    try:
        places = await community.attach_votes(redis_conn, info.raw)
    except RedisError as e:
        logger.warning("attach_votes failed: %s", e)
        places = info.raw
    return select_places(places, lat, lon, radius, min_rating)._replace(
        from_cache=info.from_cache,
        fsq_ok=info.fsq_ok,
        used_osm=info.used_osm,
        rated=info.rated,
    )
