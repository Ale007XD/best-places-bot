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
   >= TOP_N мест; иначе тот, у кого мест больше. Mapbox — только если совсем пусто.
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
import unicodedata
from typing import Any, NamedTuple

from redis.exceptions import RedisError

from bot.config import settings
from bot.services import community
from bot.services.translator import get_string
from bot.utils.foursquare_api import FoursquareError, ratings_available
from bot.utils.foursquare_api import fetch_places as fsq_fetch
from bot.utils.geoapify_api import find_places_geoapify, reverse_region
from bot.utils.geospatial import calculate_distance
from bot.utils.mapbox_api import find_places_mapbox
from bot.utils.overpass_api import find_places_overpass
from bot.utils.querytools import classify_kind, matches_kind

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
DEDUP_DISTANCE_M = 40  # то же имя: дубль, только если ближе (разные филиалы сети — не дубль)
# «Тосты» и «Тосты, винное бистро» — одно заведение с разным оформлением имени.
# Имена не равны, но ядра вложены друг в друга: допускаем больший разброс координат.
DEDUP_FUZZY_DISTANCE_M = 45  # «Тосты» / «Тосты, винное бистро»: имя похоже — ближе этого дубль
# Дальше (до DEDUP_FAR_DISTANCE_M) — дубль только если улица совпадает или неизвестна у одного
# из мест; разные улицы — разные заведения. Ядро имени должно быть длиннее 3 символов.
DEDUP_FAR_DISTANCE_M = 120
FSQ_FETCH_RADIUS = 1000  # FSQ — живой запрос на максимум; расширение радиуса идёт в памяти
EXPAND_STEPS = (100, 200, 300, 500, 1000)  # шаги авто-расширения, если мест меньше TOP_N
DISTANCE_PENALTY_PER_100M = 0.03  # штраф в score за каждые 100 м (шкала рейтинга 0–0.7)
_GENERIC_WORDS = frozenset({
    "бар", "бистро", "кафе", "ресторан", "паб", "пиццерия", "кофейня", "столовая",
    "винный", "винное", "винотека", "cafe", "bar", "pub", "restaurant", "bistro",
    "wine", "coffee", "the", "и",
    "quán", "nhà", "hàng", "ร้าน",  # вьетнамское «заведение/ресторан», тайское «ร้าน»
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
    provider: str = ""  # кто обслужил запрос: fsq | geoapify | overpass | mapbox
    radius_asked: int = 0  # радиус, выбранный пользователем
    radius_used: int = 0  # радиус, по которому реально выбраны места (≥ asked при расширении)
    by_accuracy: bool = False  # старт радиуса поднят из-за низкой точности геопозиции


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


def coverage_m(provider: str, radius: int) -> int:
    """До какого радиуса загруженный набор мест полон — дальше расширять «в памяти» нельзя."""
    if provider == "fsq":
        return FSQ_FETCH_RADIUS
    if provider in OSM_PROVIDERS:
        return fetch_radius(radius) - FETCH_MARGIN_M
    return radius


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
    return f"places:v5:{hashlib.md5(raw.encode()).hexdigest()}"


_SAME_PIN_M = 3  # заведения ближе этого — «одна и та же точка» (с запасом на округление)
_FSQ_DISTANCE_TOLERANCE_M = 150  # допустимое расхождение расстояния FSQ и по координатам


def _street_key(vicinity: Any) -> str | None:
    """Улица без номера дома: «85 (Phạm Văn Chiêu), Hồ Chí Minh» → «phạm văn chiêu».

    None — улицу выделить нельзя (адреса нет): такое место не считается противоречащим.
    """
    first = str(vicinity or "").split(",", 1)[0].replace("(", " ").replace(")", " ")
    tokens = [t for t in first.casefold().split() if not any(ch.isdigit() for ch in t)]
    return " ".join(tokens) or None


def drop_suspect_pins(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убирает места с «битыми» координатами провайдера.

    Признак: несколько РАЗНЫХ заведений в одной точке (с точностью ~1 м), у которых адреса на
    разных улицах — такое бывает, когда провайдер не смог определить положение и поставил
    все записи в одну координату (нередко за сотни км от реального адреса). Заведения в
    одном здании (фудкорт, торговый центр) имеют общую улицу и не затрагиваются.
    """
    located = [p for p in places if p.get("lat") is not None and p.get("lon") is not None]
    # Кластеры «одной точки» (single linkage, _SAME_PIN_M): устойчиво к округлению координат.
    parent = list(range(len(located)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(located):
        for j in range(i + 1, len(located)):
            b = located[j]
            if (
                calculate_distance(
                    float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])
                )
                <= _SAME_PIN_M
            ):
                parent[find(j)] = find(i)

    clusters: dict[int, list[dict[str, Any]]] = {}
    for i, p in enumerate(located):
        clusters.setdefault(find(i), []).append(p)

    bad: set[int] = set()
    for group in clusters.values():
        streets = {k for p in group if (k := _street_key(p.get("vicinity")))}
        if len(group) >= 2 and len(streets) >= 2:
            bad.update(id(p) for p in group)
            logger.warning(
                "Suspect pins dropped: %s places at (%s, %s) with different streets: %s",
                len(group),
                group[0]["lat"],
                group[0]["lon"],
                "; ".join(str(p.get("name")) for p in group),
            )
    return [p for p in places if id(p) not in bad]


def drop_distance_mismatch(
    places: list[dict[str, Any]], lat: float, lon: float
) -> list[dict[str, Any]]:
    """Отбрасывает места, у которых расстояние по версии провайдера (fsq_distance) не
    сходится с расстоянием по их координатам: значит, координаты или расстояние неверны."""
    kept = []
    for p in places:
        fd = p.get("fsq_distance")
        if fd is not None and p.get("lat") is not None and p.get("lon") is not None:
            mine = calculate_distance(lat, lon, float(p["lat"]), float(p["lon"]))
            if abs(float(fd) - mine) > _FSQ_DISTANCE_TOLERANCE_M:
                logger.warning(
                    "Distance mismatch dropped: %r provider=%sm by coordinates=%sm",
                    p.get("name"),
                    fd,
                    mine,
                )
                continue
        kept.append(p)
    return kept


_REGION_NOISE = re.compile(r"\b(thanh pho|chang wat|tinh|tp|city|province|prefecture)\b")
REGION_CACHE_PREFIX = "region:v1:"


def _fold_region(text: Any) -> str:
    """«Hồ Chí Minh City» → «ho chi minh»: без диакритики, регистра и слов city/province."""
    s = unicodedata.normalize("NFKD", str(text or "")).replace("đ", "d").replace("Đ", "D")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", _REGION_NOISE.sub(" ", s)).strip()


def _place_region(vicinity: Any) -> str | None:
    """Последний сегмент адреса («…, Hồ Chí Minh») в нормализованном виде; нет — None."""
    v = re.sub(r"\([^)]*\)", " ", str(vicinity or ""))
    if "," not in v:
        return None
    region = _fold_region(v.rsplit(",", 1)[1])
    return region if len(region.replace(" ", "")) >= 3 else None


def flag_foreign_region(places: list[dict[str, Any]], pin_text: str | None) -> list[dict[str, Any]]:
    """Помечает foreign_region у мест, чей регион из адреса не найден среди названий региона
    точки пользователя. Не удаляет: после переименований регионов (реформа 2025) возможны
    ложные срабатывания, поэтому такие места лишь уходят в конец выдачи."""
    pin = _fold_region(pin_text).replace(" ", "")
    if not pin:
        return places
    out = []
    for p in places:
        region = _place_region(p.get("vicinity"))
        if region and region.replace(" ", "") not in pin:
            logger.warning(
                "Foreign region demoted: %r region=%r pin=%r", p.get("name"), region, pin_text
            )
            p = {**p, "foreign_region": True}
        out.append(p)
    return out


def _region_key(lat: float, lon: float) -> str:
    """Ключ кэша региона: хеш ячейки, чтобы координаты не лежали в Redis открытым текстом."""
    clat, clon = _cell(lat, lon)
    return REGION_CACHE_PREFIX + hashlib.md5(f"{clat}:{clon}".encode()).hexdigest()


async def _pin_region(lat: float, lon: float, redis_conn) -> str | None:
    """Названия региона точки пользователя (Geoapify reverse), кэш по ячейке ~110 м."""
    key = _region_key(lat, lon)
    try:
        cached = await redis_conn.get(key)
        if cached:
            return cached if isinstance(cached, str) else cached.decode()
    except RedisError as e:
        logger.warning("Region cache read failed: %s", e)
    text = await reverse_region(lat, lon, settings.GEOAPIFY_API_KEY)
    if text:
        try:
            await redis_conn.setex(key, settings.OSM_CACHE_TTL, text)
        except RedisError as e:
            logger.warning("Region cache write failed: %s", e)
    return text or None


async def _demote_foreign(
    places: list[dict[str, Any]], lat: float, lon: float, redis_conn
) -> list[dict[str, Any]]:
    if not settings.GEOAPIFY_API_KEY or not any(_place_region(p.get("vicinity")) for p in places):
        return places
    return flag_foreign_region(places, await _pin_region(lat, lon, redis_conn))


async def _drop_closed(places: list[dict[str, Any]], redis_conn) -> list[dict[str, Any]]:
    """Убирает закрытые места: помечены провайдером (FSQ date_closed) или жалобами пользователей.

    Идёт ДО подсчёта провайдера: иначе закрытые места «засчитываются», а выдача пустеет."""
    kept = [p for p in places if not p.get("closed_at")]
    if len(kept) < len(places):
        logger.info("Provider-closed places dropped: %s", len(places) - len(kept))
    try:
        crowd = await community.closed_ids(redis_conn, [p.get("place_id") for p in kept])
    except RedisError as e:
        logger.warning("closed_ids failed: %s", e)
        return kept
    if crowd:
        logger.info("User-reported closed places dropped: %s", len(crowd))
    return [p for p in kept if p.get("place_id") not in crowd]


def _named(raw: list[dict[str, Any]]) -> int:
    """Сколько мест с названием: безымянные не считаются «достаточным» результатом."""
    return sum(1 for p in raw if not p.get("unnamed"))


_UNNAMED_KEYS = {
    "cafe": "unnamed_cafe",
    "restaurant": "unnamed_restaurant",
    "bar": "unnamed_bar",
}


def _label_unnamed(places: list[dict[str, Any]], lang_code: str) -> list[dict[str, Any]]:
    """Безымянным местам — подпись «Кафе без названия» на языке пользователя (в копиях)."""
    out = []
    for p in places:
        if p.get("unnamed") and not p.get("name"):
            key = _UNNAMED_KEYS.get(classify_kind(p), "unnamed_place")
            p = {**p, "name": get_string(key, lang=lang_code)}
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


def _far_duplicate(p: dict[str, Any], q: dict[str, Any]) -> bool:
    """Дубль на расстоянии 45–120 м: улица совпадает или неизвестна у одного из мест,
    ядро имени длиннее 3 символов. Известные и разные улицы — разные заведения."""
    core = "".join(sorted(_core_tokens(p.get("name")) & _core_tokens(q.get("name"))))
    if len(core) <= 3:
        return False
    sp, sq = _street_key(p.get("vicinity")), _street_key(q.get("vicinity"))
    return sp is None or sq is None or sp == sq


def _deduplicate(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убирает дубли одного заведения (разные оформления имени, один и тот же объект из двух баз).

    • то же имя — ближе DEDUP_DISTANCE_M;
    • похожее имя (ядро одного вложено в другое) — ближе DEDUP_FUZZY_DISTANCE_M;
    • дальше, до DEDUP_FAR_DISTANCE_M, — только при той же (или неизвестной) улице.
    Из дублей остаётся запись с рейтингом (приоритет у Foursquare — он идёт первым).
    """
    result: list[dict[str, Any]] = []
    for p in places:
        name = _norm_name(p.get("name"))
        if not name:  # безымянные не склеиваем: у них нечего сравнивать
            result.append(p)
            continue
        dup_idx = None
        for i, q in enumerate(result):
            exact = name == _norm_name(q.get("name"))
            if not exact and not _fuzzy_same_name(p.get("name"), q.get("name")):
                continue
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
            near = DEDUP_DISTANCE_M if exact else DEDUP_FUZZY_DISTANCE_M
            if d <= near or (d <= DEDUP_FAR_DISTANCE_M and _far_duplicate(p, q)):
                dup_idx = i
                break
        if dup_idx is None:
            result.append(p)
        elif result[dup_idx].get("rating") is None and p.get("rating") is not None:
            result[dup_idx] = p
    return result


def _score(p: dict[str, Any], user_lat: float, user_lon: float) -> float:
    """0.7 · рейтинг (0–1) − штраф за расстояние (0.03 за каждые 100 м, не зависит от радиуса).

    Абсолютный штраф: место в 80 м не «обесценивается» оттого, что выбран радиус 1000 м."""
    rating = float(p.get("rating") or 0.0) / 5.0
    if p.get("lat") is None or p.get("lon") is None:
        return 0.7 * rating
    dist = calculate_distance(user_lat, user_lon, float(p["lat"]), float(p["lon"]))
    return 0.7 * rating - DISTANCE_PENALTY_PER_100M * dist / 100


def _within_radius(p: dict[str, Any], lat: float, lon: float, radius: int) -> bool:
    if p.get("lat") is None or p.get("lon") is None:
        return False  # без координат нельзя доказать, что место в радиусе
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
    """Чистая функция: дедуп → радиус → категория → фильтр по рейтингу → ранжирование.

    Места с foreign_region (адрес в другом регионе, чем точка пользователя) идут строго
    после остальных и только добирают выдачу.
    """
    local = [p for p in raw if not p.get("foreign_region")]
    foreign = [p for p in raw if p.get("foreign_region")]
    res = _select(local, lat, lon, radius, min_rating, kind)
    if foreign and len(res.places) < RESULT_LIMIT:
        extra = _select(foreign, lat, lon, radius, 0.0, kind)
        room = RESULT_LIMIT - len(res.places)
        res = res._replace(
            places=res.places + extra.places[:room],
            rating_available=res.rating_available or extra.rating_available,
        )
    return res


def _select(
    raw: list[dict[str, Any]],
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    kind: str,
) -> SearchResult:
    places = [
        p
        for p in _deduplicate(raw)
        if _within_radius(p, lat, lon, radius) and matches_kind(p, kind)
    ]

    def rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Безымянные — только после именованных (они добирают выдачу, если именованных мало).
        return sorted(
            items,
            key=lambda p: (not p.get("unnamed"), _score(p, lat, lon)),
            reverse=True,
        )

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
            FSQ_FETCH_RADIUS,
            lang_code,
            want_ratings=want_ratings,
        )
    except FoursquareError as e:
        logger.warning("Foursquare failed (%s)", e)
        return None
    # Короткая «улика» в логе: что именно вернул провайдер (имя, координаты, его расстояние).
    logger.info(
        "FSQ raw %s places; first: %s",
        len(res.places),
        " | ".join(
            f"{p.get('name')!r} {p.get('lat')},{p.get('lon')} d={p.get('fsq_distance')}"
            for p in res.places[:3]
        ),
    )
    places = drop_distance_mismatch(res.places, lat, lon)
    return LoadInfo(places, False, True, False, res.rated, "fsq")


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
        # Битые координаты провайдера отсекаем ДО подсчёта: иначе провайдер с мусором
        # «выигрывает» у провайдера с меньшим, но настоящим числом мест.
        info = info._replace(raw=drop_suspect_pins(info.raw))
        if best is None or (_named(info.raw), len(info.raw)) > (_named(best.raw), len(best.raw)):
            best = info
        return info if _named(info.raw) >= TOP_N else None

    order = _base_providers(skip=frozenset({"fsq"}) if want_rated else frozenset())
    if want_rated:
        order.insert(0, "fsq")  # платный поиск начинается с Foursquare (рейтинги, фото)

    for name in order:
        if name == "fsq":
            info = await _fsq_provider(lat, lon, radius, lang_code, want_rated)
            if info is not None:
                info = info._replace(raw=await _demote_foreign(info.raw, lat, lon, redis_conn))
        else:
            info = await _osm_provider(name, lat, lon, radius, lang_code, redis_conn, kind)
        if info is not None:
            info = info._replace(raw=await _drop_closed(info.raw, redis_conn))
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
        if extra:
            # Аварийный путь тоже проходит чистку: жалобы «закрыто» и «битые пины».
            extra = drop_suspect_pins(await _drop_closed(extra, redis_conn))
        if extra:
            return LoadInfo(extra, False, bool(first_ok), False, False, provider)

    if best is None:
        raise SearchUnavailable("all providers failed")
    return best._replace(fsq_ok=bool(first_ok))


def _radius_steps(start: int, cover: int) -> list[int]:
    """Старт и дальше шаги расширения из EXPAND_STEPS, не выходя за полноту данных."""
    return [start] + [r for r in EXPAND_STEPS if start < r <= cover]


async def search_places(
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    lang_code: str,
    redis_conn,
    with_ratings: bool = True,
    kind: str = "all",
    accuracy_m: float | None = None,
) -> SearchResult:
    """with_ratings=False — базовая выдача без платных рейтингов/фото (бесплатный уровень).

    kind — фильтр категории (all/cafe/restaurant/bar): на стороне провайдера для
    Geoapify/Overpass (категория входит в ключ кэша), локально для остальных.

    Радиус: если мест меньше TOP_N, он расширяется шагами (100→200→300→500→1000) без нового
    запроса — по уже загруженным данным (OSM-«ведро» 600 м покрывает до 500 м, FSQ — 1000 м).
    accuracy_m — точность геопозиции: радиус меньше неё бессмыслен, старт поднимается.
    """
    info = await _load_raw(lat, lon, radius, lang_code, redis_conn, with_ratings, kind)
    # Отметки пользователей меняются постоянно — к кэшу провайдера не относятся.
    try:
        places = await community.attach_votes(redis_conn, info.raw)
    except RedisError as e:
        logger.warning("attach_votes failed: %s", e)
        places = info.raw

    cover = max(coverage_m(info.provider, radius), radius)
    start = radius
    if accuracy_m and accuracy_m > radius:
        allowed = [r for r in EXPAND_STEPS if r <= cover] or [radius]
        start = max(next((r for r in allowed if r >= accuracy_m), allowed[-1]), radius)
    selected = used = None
    for r in _radius_steps(start, cover):
        selected, used = select_places(places, lat, lon, r, min_rating, kind), r
        if _named(selected.places) >= TOP_N:
            break
    assert selected is not None and used is not None
    return selected._replace(
        places=_label_unnamed(selected.places, lang_code),
        from_cache=info.from_cache,
        fsq_ok=info.fsq_ok,
        used_osm=info.used_osm,
        rated=info.rated,
        provider=info.provider,
        radius_asked=radius,
        radius_used=used,
        by_accuracy=start > radius,
    )
