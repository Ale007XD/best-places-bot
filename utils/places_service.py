# bot/utils/places_service.py

import asyncio
from typing import List, Dict, Any
import logging

from bot.utils.foursquare_api import find_places as fsq_find
from bot.utils.geospatial import calculate_distance

# (заглушка под будущий Mapbox)
async def mapbox_find(*args, **kwargs) -> List[Dict[str, Any]]:
    return []


def _deduplicate(places: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []

    for p in places:
        pid = p.get("place_id") or p.get("name")
        if pid and pid not in seen:
            seen.add(pid)
            result.append(p)

    return result


def _score(place: Dict[str, Any], user_lat: float, user_lon: float) -> float:
    rating = float(place.get("rating") or 0.0)

    lat = place.get("lat")
    lon = place.get("lon")

    if lat is None or lon is None:
        return rating

    distance = calculate_distance(user_lat, user_lon, float(lat), float(lon))

    # чем ближе — тем лучше
    distance_score = 1 / (1 + distance)

    return (
        0.6 * rating +
        0.4 * distance_score
    )


async def search_places(
    _,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    max_rating: float,
    lang_code: str,
    api_key: str,
) -> List[Dict[str, Any]]:
    """
    Production orchestrator:
    - multi-provider (FSQ + future Mapbox)
    - deterministic merge
    - scoring
    - fallback
    """

    logging.info("PlacesService: start search")

    fsq_task = fsq_find(
        _,
        api_key=api_key,
        lat=lat,
        lon=lon,
        radius=radius,
        min_rating=min_rating,
        max_rating=max_rating,
        lang_code=lang_code,
    )

    mapbox_task = mapbox_find()  # пока пусто

    results = await asyncio.gather(fsq_task, mapbox_task)

    merged = []
    for r in results:
        merged.extend(r)

    merged = _deduplicate(merged)

    # fallback: если мало результатов — ослабляем фильтр
    if len(merged) < 3:
        logging.info("Fallback triggered: relaxing rating filter")

        fallback = await fsq_find(
            _,
            api_key=api_key,
            lat=lat,
            lon=lon,
            radius=radius,
            min_rating=0.0,
            max_rating=5.0,
            lang_code=lang_code,
        )

        merged.extend(fallback)
        merged = _deduplicate(merged)

    # ранжирование
    ranked = sorted(
        merged,
        key=lambda p: _score(p, lat, lon),
        reverse=True
    )

    return ranked
