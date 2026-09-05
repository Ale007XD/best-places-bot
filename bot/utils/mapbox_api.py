# bot/utils/mapbox_api.py

import logging
import math
from typing import Any

import httpx

from bot.utils.geospatial import calculate_distance


def _bbox_from_radius(lat: float, lon: float, radius: int) -> str:
    """
    Geocoding v5 не принимает параметр radius (он молча игнорировался) —
    вместо этого строим bbox нужного размера вокруг точки пользователя.
    """
    delta_lat = radius / 111_320  # метров в одном градусе широты
    delta_lon = radius / (111_320 * max(math.cos(math.radians(lat)), 1e-6))
    return f"{lon - delta_lon},{lat - delta_lat},{lon + delta_lon},{lat + delta_lat}"


async def find_places_mapbox(
    lat: float,
    lon: float,
    radius: int,
    limit: int,
    lang_code: str,
    access_token: str,
) -> list[dict[str, Any]]:
    """
    Mapbox Geocoding API (POI search)

    NB: это forward-geocoding, а не полноценный category search — строка
    "restaurant,cafe,bar" уходит как текст запроса, а не фильтр категорий.
    Для честного поиска по категориям нужен Mapbox Search Box API
    (category search endpoint) — здесь только минимальный фикс:
    bbox вместо игнорируемого radius + types=poi, чтобы отсечь не-POI совпадения.
    """

    url = "https://api.mapbox.com/geocoding/v5/mapbox.places/restaurant,cafe,bar.json"

    params = {
        "proximity": f"{lon},{lat}",
        "bbox": _bbox_from_radius(lat, lon, radius),
        "types": "poi",
        # У Geocoding v5 максимум — 10 (запрошенное значение молча обрежется/даст ошибку).
        "limit": max(1, min(int(limit), 10)),
        "language": lang_code,
        "access_token": access_token,
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params=params, timeout=10.0)

        if not r.is_success:
            logging.error("Mapbox error: %s %s", r.status_code, r.text[:200])
            return []

        data = r.json()
        features = data.get("features", [])

        places = [_normalize(f) for f in features]

        # bbox — квадрат вокруг точки, его углы дальше radius (до ~1.41×radius).
        # Отсекаем всё, что реально дальше запрошенного радиуса.
        return [
            p
            for p in places
            if p.get("lat") is not None
            and p.get("lon") is not None
            and calculate_distance(lat, lon, p["lat"], p["lon"]) <= radius
        ]

    except Exception as e:
        logging.error("Mapbox request failed: %s", e)
        return []


def _normalize(f: dict[str, Any]) -> dict[str, Any]:
    coords = f.get("geometry", {}).get("coordinates", [None, None])

    return {
        "place_id": f.get("id"),
        "name": f.get("text"),
        "rating": None,  # Mapbox не даёт рейтинг
        "user_ratings_total": 0,
        "types": f.get("place_type", []),
        "primary_type": f.get("place_type", ["poi"])[0],
        "lat": coords[1],
        "lon": coords[0],
        "vicinity": f.get("place_name"),
        "price_level": None,
        "business_status": "OPERATIONAL",
        "opening_hours": None,
        "photos": [],
        "icon": None,
        "icon_background_color": None,
        "permanently_closed": None,
    }
