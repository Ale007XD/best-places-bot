# bot/utils/places_service.py

import asyncio
import json
import hashlib
from typing import List, Dict, Any
import logging

from bot.utils.foursquare_api import find_places as fsq_find
from bot.utils.mapbox_api import find_places_mapbox
from bot.utils.vietmap_api import find_places_vietmap
from bot.utils.geospatial import calculate_distance


CACHE_TTL = 600  # 10 минут


def _make_cache_key(
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    max_rating: float,
    lang_code: str,
) -> str:
    raw = f"{round(lat,4)}:{round(lon,4)}:{radius}:{min_rating}:{max_rating}:{lang_code}"
    h = hashlib.md5(raw.encode()).hexdigest()
    return f"places:{h}"


def _deduplicate(places: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []

    for p in places:
        key = (p.get("name"), p.get("lat"), p.get("lon"))
        if key not in seen:
            seen.add(key)
            result.append(p)

    return result


def _score(place: Dict[str, Any], user_lat: float, user_lon: float, radius: int) -> float:
    """
    Ranking:
    - приоритет рейтинга (FSQ)
    - затем расстояние (линейно по всему радиусу, а не 1/(1+d) — та функция
      схлопывалась к ~0 уже к 50м и дистанция переставала на что-либо влиять)
    """

    rating = float(place.get("rating") or 0.0)

    lat = place.get("lat")
    lon = place.get("lon")

    if lat is None or lon is None:
        return 0.7 * rating

    distance = calculate_distance(user_lat, user_lon, float(lat), float(lon))

    # чем ближе — тем выше score; на границе радиуса вклад дистанции — 0
    distance_score = max(0.0, 1 - (distance / radius)) if radius > 0 else 0.0

    return 0.7 * rating + 0.3 * distance_score


async def search_places(
    _,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    max_rating: float,
    lang_code: str,
    fsq_api_key: str,
    mapbox_token: str,
    vietmap_api_key: str,
    redis_conn,
) -> List[Dict[str, Any]]:
    """
    Production Places Orchestrator

    Flow:
    1. Cache
    2. Mapbox + Foursquare (parallel)
    3. Merge + deduplicate
    4. Fallback (FSQ → VietMap)
    5. Ranking
    6. Cache write
    """

    cache_key = _make_cache_key(lat, lon, radius, min_rating, max_rating, lang_code)

    # 🔹 1. CACHE READ
    try:
        cached = await redis_conn.get(cache_key)
        if cached:
            logging.info("CACHE HIT")
            return json.loads(cached)
    except Exception as e:
        logging.warning("Cache read failed: %s", e)

    logging.info("CACHE MISS → querying providers")

    # 🔹 2. PROVIDERS (parallel)
    mapbox_task = find_places_mapbox(
        lat=lat,
        lon=lon,
        radius=radius,
        limit=10,  # Geocoding v5: максимум 10, find_places_mapbox дополнительно зажимает сам
        lang_code=lang_code,
        access_token=mapbox_token,
    )

    fsq_task = fsq_find(
        _,
        api_key=fsq_api_key,
        lat=lat,
        lon=lon,
        radius=radius,
        min_rating=min_rating,
        max_rating=max_rating,
        lang_code=lang_code,
    )

    mapbox_results, fsq_results = await asyncio.gather(mapbox_task, fsq_task)

    merged = mapbox_results + fsq_results
    merged = _deduplicate(merged)

    # Если ниже сработает фолбэк — итоговый список перестанет строго
    # соответствовать запрошенному диапазону рейтинга, и его нельзя будет
    # закэшировать под ключом этого диапазона (иначе следующий пользователь
    # с тем же диапазоном получит из кэша чужой, нерелевантный результат).
    used_widened_fallback = False

    # 🔹 3. FALLBACK #1 — расширяем FSQ
    if len(merged) < 3:
        logging.info("Fallback: expanding Foursquare search")

        fsq_fallback = await fsq_find(
            _,
            api_key=fsq_api_key,
            lat=lat,
            lon=lon,
            radius=radius,
            min_rating=0.0,
            max_rating=5.0,
            lang_code=lang_code,
        )

        if fsq_fallback:
            used_widened_fallback = True

        merged.extend(fsq_fallback)
        merged = _deduplicate(merged)

    # 🔹 4. FALLBACK #2 — VietMap (локальные места)
    if len(merged) < 3:
        logging.info("Fallback: VietMap activated")

        vietmap_results = await find_places_vietmap(
            lat=lat,
            lon=lon,
            radius=radius,
            api_key=vietmap_api_key,
        )

        if vietmap_results:
            used_widened_fallback = True  # у VietMap рейтинга нет вовсе

        merged.extend(vietmap_results)
        merged = _deduplicate(merged)

    # 🔹 5. RANKING
    ranked = sorted(
        merged,
        key=lambda p: _score(p, lat, lon, radius),
        reverse=True,
    )

    # 🔹 6. CACHE WRITE — только «честные» результаты в исходном диапазоне.
    # Кэшируем, только если явный фолбэк не расширял диапазон И среди того,
    # что реально уйдёт в кэш, нет мест без рейтинга вовсе. Второе условие
    # нужно отдельно: Mapbox/VietMap (rating=None) не проходят фильтр диапазона
    # ни на одном этапе, но Mapbox мержится в merged на шаге 2 безусловно —
    # если его одного хватило на >=3 места, ни один фолбэк не сработает,
    # used_widened_fallback останется False, а нерейтингованное место всё
    # равно попадёт в кэш под ключом узкого диапазона.
    to_cache = ranked[:10]  # кешируем больше, чем отдаём
    has_unrated = any(p.get("rating") is None for p in to_cache)

    if not used_widened_fallback and not has_unrated:
        try:
            await redis_conn.setex(cache_key, CACHE_TTL, json.dumps(to_cache))
        except Exception as e:
            logging.warning("Cache write failed: %s", e)
    else:
        logging.info("Skipping cache write: results widened or contain unrated places")

    return ranked
