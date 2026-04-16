# bot/utils/places_service.py

import asyncio
import json
import hashlib
from typing import List, Dict, Any
import logging

from bot.utils.foursquare_api import find_places as fsq_find
from bot.utils.mapbox_api import find_places_mapbox
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
    rating = float(place.get("rating") or 0.0)

    lat = place.get("lat")
    lon = place.get("lon")

    if lat is None or lon is None:
        return rating

    distance = calculate_distance(user_lat, user_lon, float(lat), float(lon))
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
    redis_conn,  # 🔥 ВАЖНО: передаём извне
) -> List[Dict[str, Any]]:

    cache_key = _make_cache_key(lat, lon, radius, min_rating, max_rating)

    # 🔹 1. CACHE READ
    cached = await redis_conn.get(cache_key)
    if cached:
        logging.info("CACHE HIT")
        return json.loads(cached)

    logging.info("CACHE MISS → querying providers")

    # 🔹 2. PROVIDERS
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

    # 🔹 fallback
    if len(merged) < 3:
        fallback = await fsq_find(
            _,
            api_key=fsq_api_key,
            lat=lat,
            lon=lon,
            radius=radius,
            min_rating=0.0,
            max_rating=5.0,
            lang_code=lang_code,
        )
        merged.extend(fallback)
        merged = _deduplicate(merged)

    ranked = sorted(
        merged,
        key=lambda p: _score(p, lat, lon),
        reverse=True,
    )

    # 🔹 3. CACHE WRITE
    try:
        await redis_conn.setex(
            cache_key,
            CACHE_TTL,
            json.dumps(ranked[:10])  # кешируем чуть больше
        )
    except Exception as e:
        logging.warning("Cache write failed: %s", e)

    return ranked
