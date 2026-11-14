# bot/utils/vietmap_api.py

import logging
from typing import Any

import httpx

from bot.utils.http import get_client

logger = logging.getLogger(__name__)


async def find_places_vietmap(
    lat: float,
    lon: float,
    radius: int,
    api_key: str,
) -> list[dict[str, Any]]:
    """
    VietMap Places API (аварийный фолбэк для Вьетнама).

    NB: эндпоинт и параметры не сверены с документацией VietMap — проверить
    вручную (curl) перед тем, как полагаться на этот фолбэк.
    """

    url = "https://maps.vietmap.vn/api/place/search"

    params = {
        "apikey": api_key,
        "lat": lat,
        "lng": lon,
        "radius": radius,
    }

    try:
        r = await get_client().get(url, params=params)

        if not r.is_success:
            logger.error("VietMap error: %s %s", r.status_code, r.text[:200])
            return []

        data = r.json()
        results = data.get("data", [])

        return [_normalize(p) for p in results]

    except (httpx.HTTPError, ValueError) as e:
        logger.error("VietMap request failed: %s", e)
        return []


def _normalize(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": p.get("id"),
        "name": p.get("name"),
        "rating": None,  # VietMap часто без рейтинга
        "user_ratings_total": 0,
        "primary_type": "",
        "lat": p.get("lat"),
        "lon": p.get("lng"),
        "vicinity": p.get("address"),
        "source": "vietmap",
    }
