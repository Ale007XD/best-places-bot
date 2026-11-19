# bot/utils/places_service.py
"""
Оркестратор поиска.

Принципы:
• Один запрос обслуживается ОДНИМ провайдером — данные разных источников не смешиваются
  (лицензии: Foursquare запрещает серверный кэш и интеграцию с ODbL-материалом).
• Foursquare (рейтинги, фото, а также базовые поля) — всегда «живой» запрос, без кэша;
  хранится только fsq_id в отметках пользователей. Единственное исключение — одновременные
  дубли не схлопываются: общего ответа между пользователями нет.
• Geoapify и Overpass (данные OSM, ODbL) кэшируются по ячейке ~100 м: запрос идёт из центра
  ячейки с запасом радиуса, а расстояния считаются от точки пользователя.

Flow:
1. Платный поиск (with_ratings): Foursquare с рейтингами/фото. Если мест меньше TOP_N —
   пробуем базовых провайдеров и берём того, у кого мест больше (без смешивания).
2. Базовый поиск: провайдеры по BASE_PROVIDERS (geoapify, fsq, overpass) — первый, давший
   >= TOP_N мест; иначе тот, у кого мест больше. Mapbox/VietMap — только если совсем пусто.
   Все провайдеры сбоят — SearchUnavailable (это не «мест нет»).
3. Дедупликация (нормализованное имя + близость).
4. Локальный фильтр категории и минимального рейтинга, ранжирование.
   Если мест с нужным рейтингом меньше TOP_N, добираются лучшие из остальных
   (флаг relaxed), чтобы пользователь не упирался в пустой ответ.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
from typing import Any, NamedTuple

from redis.exceptions import RedisError

from bot.config import settings
from bot.services import community
from bot.utils.foursquare_api import FoursquareError, ratings_available
from bot.utils.foursquare_api import fetch_places as fsq_fetch
from bot.utils.geoapify_api import find_places_geoapify
from bot.utils.geospatial import calculate_distance
from bot.utils.mapbox_api import find_places_mapbox
from bot.utils.overpass_api import find_places_overpass
from bot.utils.querytools import matches_kind
from bot.utils.vietmap_api import find_places_vietmap

logger = logging.getLogger(__name__)

EMPTY_CACHE_TTL = 3600  # пустой, но успешный ответ OSM-провайдера — на час
# Ячейка кэша: координаты округляются до 3 знаков (~110 м). Запрос идёт из ЦЕНТРА ячейки с
# запасом на её половину диагонали (≤ ~80 м), чтобы любая точка внутри ячейки была покрыта.
CELL_DECIMALS = 3
FETCH_MARGIN_M = 80
FETCH_RADII = (600, 1100)  # два «ведра» радиуса запроса — мало ключей, один ответ на все шаги
OSM_PROVIDERS = frozenset({"geoapify", "overpass"})
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
    provider: str = ""  # кто обслужил запрос: fsq | geoapify | overpass | mapbox | vietmap


class LoadInfo(NamedTuple):
    raw: list[dict[str, Any]]
    from_cache: bool
    fsq_ok: bool = True  # первый выбранный провайдер ответил (для метрик качества)
    used_osm: bool = False
    rated: bool = False
    provider: str = ""


def fetch_radius(radius: int) -> int:
    """Радиус запроса к провайдеру: показ + запас на ячейку, по «вёдрам» (600 / 1100 / выше)."""
    need = radius + FETCH_MARGIN_M
    for r in FETCH_RADII:
        if need <= r:
            return r
    return int(math.ceil(need / 100) * 100)


def _cell(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, CELL_DECIMALS), round(lon, CELL_DECIMALS)


def _make_cache_key(
    lat: float,
    lon: float,
    radius: int,
    lang_code: str,
    variant: str = "osm",
    kind: str = "all",
) -> str:
    """Ключ кэша OSM-провайдера: ячейка, «ведро» радиуса, язык, провайдер, категория."""
    clat, clon = _cell(lat, lon)
    raw = f"{clat}:{clon}:{fetch_radius(radius)}:{lang_code}:{variant}:{kind}"
    return f"places:v4:{hashlib.md5(raw.encode()).hexdigest()}"


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
    kind: str = "all",
) -> SearchResult:
    """Чистая функция: дедуп → радиус → категория → фильтр по рейтингу → ранжирование."""
    places = [
        p
        for p in _deduplicate(raw)
        if _within_radius(p, lat, lon, radius) and matches_kind(p, kind)
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


async def _write_cache(redis_conn, key: str, raw: list[dict[str, Any]]) -> None:
    ttl = settings.OSM_CACHE_TTL if raw else EMPTY_CACHE_TTL
    try:
        await redis_conn.setex(key, ttl, json.dumps(raw))
    except (RedisError, TypeError) as e:
        logger.warning("Cache write failed: %s", e)


def _base_providers(skip: frozenset[str] = frozenset()) -> list[str]:
    """Провайдеры базового поиска по BASE_PROVIDERS — только доступные (ключ / включён)."""
    out: list[str] = []
    for name in (x.strip().lower() for x in settings.BASE_PROVIDERS.split(",")):
        if name in skip or name in out:
            continue
        if (
            (name == "geoapify" and settings.GEOAPIFY_API_KEY)
            or (name == "overpass" and settings.OVERPASS_ENABLED)
            or name == "fsq"
        ):
            out.append(name)
    return out


async def _osm_provider(
    name: str, lat: float, lon: float, radius: int, lang_code: str, redis_conn, kind: str
) -> LoadInfo | None:
    """Geoapify / Overpass: кэш по ячейке (ODbL разрешает). None — сбой провайдера."""
    key = _make_cache_key(lat, lon, radius, lang_code, name, kind)
    raw = await _read_cache(redis_conn, key)
    if raw is not None:
        logger.info("CACHE HIT (%s)", name)
        return LoadInfo(raw, True, True, True, False, name)

    if len(_inflight) > 1000:  # не копим замки бесконечно
        for k in [k for k, lk in _inflight.items() if not lk.locked()]:
            del _inflight[k]
    async with _inflight.setdefault(key, asyncio.Lock()):
        raw = await _read_cache(redis_conn, key)  # мог заполнить первый
        if raw is not None:
            return LoadInfo(raw, True, True, True, False, name)
        clat, clon = _cell(lat, lon)
        frad = fetch_radius(radius)
        if name == "geoapify":
            raw = await find_places_geoapify(
                clat,
                clon,
                frad,
                settings.GEOAPIFY_API_KEY,
                kind,
                lang_code,
                url=settings.GEOAPIFY_URL,
            )
        else:
            raw = await find_places_overpass(clat, clon, frad, settings.OVERPASS_URL, kind=kind)
        if raw is None:
            return None
        await _write_cache(redis_conn, key, raw)
        return LoadInfo(raw, False, True, True, False, name)


async def _fsq_provider(
    lat: float, lon: float, radius: int, lang_code: str, want_ratings: bool
) -> LoadInfo | None:
    """Foursquare — всегда живой запрос, без кэша и без схлопывания между пользователями
    (правила Foursquare запрещают хранить ответы). None — сбой провайдера."""
    try:
        res = await fsq_fetch(
            settings.FSQ_API_KEY,
            settings.FSQ_API_VERSION,
            lat,
            lon,
            radius,
            lang_code,
            want_ratings=want_ratings,
        )
    except FoursquareError as e:
        logger.warning("Foursquare failed (%s)", e)
        return None
    return LoadInfo(res.places, False, True, False, res.rated, "fsq")


async def _load_raw(
    lat: float,
    lon: float,
    radius: int,
    lang_code: str,
    redis_conn,
    with_ratings: bool = True,
    kind: str = "all",
) -> LoadInfo:
    """Выбирает ОДНОГО провайдера (см. docstring модуля) и возвращает его места как есть."""
    # Если Premium-поля сейчас недоступны (нет кредитов) — это обычный базовый запрос.
    want_rated = with_ratings and ratings_available()

    best: LoadInfo | None = None
    attempted = 0
    first_ok: bool | None = None

    def consider(info: LoadInfo | None) -> LoadInfo | None:
        """Запоминает результат; возвращает его, если мест достаточно."""
        nonlocal best, attempted, first_ok
        attempted += 1
        if first_ok is None:
            first_ok = info is not None
        if info is None:
            return None
        if best is None or len(info.raw) > len(best.raw):
            best = info
        return info if len(info.raw) >= TOP_N else None

    order = _base_providers(skip=frozenset({"fsq"}) if want_rated else frozenset())
    if want_rated:
        order.insert(0, "fsq")  # платный поиск начинается с Foursquare (рейтинги, фото)

    for name in order:
        if name == "fsq":
            info = await _fsq_provider(lat, lon, radius, lang_code, want_rated)
        else:
            info = await _osm_provider(name, lat, lon, radius, lang_code, redis_conn, kind)
        if (done := consider(info)) is not None:
            return done._replace(fsq_ok=bool(first_ok))

    # Совсем пусто — аварийные провайдеры с ключами, если заданы.
    if best is None or not best.raw:
        extra: list[dict[str, Any]] = []
        provider = ""
        if settings.MAPBOX_TOKEN:
            extra = await find_places_mapbox(
                lat=lat,
                lon=lon,
                radius=radius,
                limit=10,
                lang_code=lang_code,
                access_token=settings.MAPBOX_TOKEN,
            )
            provider = "mapbox"
        if not extra and settings.VIETMAP_API_KEY:
            extra = await find_places_vietmap(
                lat=lat, lon=lon, radius=radius, api_key=settings.VIETMAP_API_KEY
            )
            provider = "vietmap"
        if extra:
            return LoadInfo(extra, False, bool(first_ok), False, False, provider)

    if best is None:
        raise SearchUnavailable("all providers failed")
    return best._replace(fsq_ok=bool(first_ok))


async def search_places(
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    lang_code: str,
    redis_conn,
    with_ratings: bool = True,
    kind: str = "all",
) -> SearchResult:
    """with_ratings=False — базовая выдача без платных рейтингов/фото (бесплатный уровень).

    kind — фильтр категории (all/cafe/restaurant/bar): на стороне провайдера для
    Geoapify/Overpass (категория входит в ключ кэша), локально для остальных.
    """
    info = await _load_raw(lat, lon, radius, lang_code, redis_conn, with_ratings, kind)
    # Отметки пользователей меняются постоянно — к кэшу провайдера не относятся.
    try:
        places = await community.attach_votes(redis_conn, info.raw)
    except RedisError as e:
        logger.warning("attach_votes failed: %s", e)
        places = info.raw
    return select_places(places, lat, lon, radius, min_rating, kind)._replace(
        from_cache=info.from_cache,
        fsq_ok=info.fsq_ok,
        used_osm=info.used_osm,
        rated=info.rated,
        provider=info.provider,
    )
