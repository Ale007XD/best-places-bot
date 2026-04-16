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
) -> str:
    raw = f"{round(lat,4)}:{round(lon,4)}:{radius}:{min_rating}:{max_rating}"
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


def _score(place: Dict[str, Any], user_lat: float, user_lon: float) -> float:
    """
    Ranking:
    - приоритет рейтинга (FSQ)
    - затем расстояние
    """

    rating = float(place.get("rating") or 0.0)

    lat = place.get("lat")
    lon = place.get("lon")

    if lat is None or lon is None:
        return rating

    distance = calculate_distance(user_lat, user_lon, float(lat), float(lon))

    # чем ближе — тем выше score
    distance_score = 1 / (1 + distance)

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

    cache_key = _make_cache_key(lat, lon, radius, min_rating, max_rating)

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
        limit=30,
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

        merged.extend(vietmap_results)
        merged = _deduplicate(merged)

    # 🔹 5. RANKING
    ranked = sorted(
        merged,
        key=lambda p: _score(p, lat, lon),
        reverse=True,
    )

    # 🔹 6. CACHE WRITE
    try:
        await redis_conn.setex(
            cache_key,
            CACHE_TTL,
            json.dumps(ranked[:10])  # кешируем больше, чем отдаём
        )
    except Exception as e:
        logging.warning("Cache write failed: %s", e)

    return ranked
