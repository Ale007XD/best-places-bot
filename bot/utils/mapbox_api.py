# bot/utils/mapbox_api.py

import logging
from typing import List, Dict, Any

import httpx


async def find_places_mapbox(
    lat: float,
    lon: float,
    radius: int,
    limit: int,
    lang_code: str,
    access_token: str,
) -> List[Dict[str, Any]]:
    """
    Mapbox Geocoding API (POI search)
    """

    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/restaurant,cafe,bar.json"

    params = {
        "proximity": f"{lon},{lat}",
        "limit": limit,
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

        return [_normalize(f) for f in features]

    except Exception as e:
        logging.error("Mapbox request failed: %s", e)
        return []


def _normalize(f: Dict[str, Any]) -> Dict[str, Any]:
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
