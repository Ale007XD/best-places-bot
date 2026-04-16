# bot/utils/vietmap_api.py

import logging
from typing import List, Dict, Any

import httpx


async def find_places_vietmap(
    lat: float,
    lon: float,
    radius: int,
    api_key: str,
) -> List[Dict[str, Any]]:
    """
    VietMap Places API (fallback для Вьетнама)
    """

    url = "https://maps.vietmap.vn/api/place/search"

    params = {
        "apikey": api_key,
        "lat": lat,
        "lng": lon,
        "radius": radius,
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, params=params, timeout=10.0)

        if not r.is_success:
            logging.error("VietMap error: %s %s", r.status_code, r.text[:200])
            return []

        data = r.json()
        results = data.get("data", [])

        return [_normalize(p) for p in results]

    except Exception as e:
        logging.error("VietMap request failed: %s", e)
        return []


def _normalize(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "place_id": p.get("id"),
        "name": p.get("name"),
        "rating": None,  # VietMap часто без рейтинга
        "user_ratings_total": 0,
        "types": [],
        "primary_type": "local_place",
        "lat": p.get("lat"),
        "lon": p.get("lng"),
        "vicinity": p.get("address"),
        "price_level": None,
        "business_status": "OPERATIONAL",
        "opening_hours": None,
        "photos": [],
        "icon": None,
        "icon_background_color": None,
        "permanently_closed": None,
    }
