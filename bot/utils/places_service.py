# bot/utils/places_service.py

import asyncio
from typing import List, Dict, Any
import logging

from bot.utils.foursquare_api import find_places as fsq_find
from bot.utils.mapbox_api import find_places_mapbox
from bot.utils.geospatial import calculate_distance


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
) -> List[Dict[str, Any]]:

    logging.info("PlacesService: multi-provider search")

    # 🔥 Mapbox — основной источник
    mapbox_task = find_places_mapbox(
        lat=lat,
        lon=lon,
        radius=radius,
        limit=30,
        lang_code=lang_code,
        access_token=mapbox_token,
    )

    # 🔥 Foursquare — enrichment + фильтр
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

    # fallback если мало результатов
    if len(merged) < 3:
        logging.info("Fallback: expanding FSQ search")

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

    return ranked
