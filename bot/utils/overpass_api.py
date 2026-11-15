# bot/utils/overpass_api.py
"""
OpenStreetMap через Overpass API: бесплатный фолбэк без ключа и кредитов.

Даёт название, тип, адрес и координаты заведений, но НЕ рейтинги — поэтому такие места
показываются «ближайшими» с пометкой (см. places_service.select_places). Для Вьетнама
покрытие OSM хорошее. Публичный сервер — fair use: результаты кэшируются, поиск
пользователя ограничен троттлингом.
"""

import logging
from typing import Any

import httpx

from bot.utils.http import get_client

logger = logging.getLogger(__name__)

_TYPES = {
    "restaurant": "Restaurant",
    "cafe": "Café",
    "bar": "Bar",
    "pub": "Pub",
    "fast_food": "Fast food",
}


def _normalize(el: dict[str, Any]) -> dict[str, Any] | None:
    tags = el.get("tags") or {}
    name = tags.get("name")
    center = el.get("center") or {}
    lat = el.get("lat", center.get("lat"))
    lon = el.get("lon", center.get("lon"))
    if not name or lat is None or lon is None:
        return None

    street = " ".join(
        x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x
    )
    return {
        "place_id": f"osm:{el.get('type')}/{el.get('id')}",
        "name": name,
        "rating": None,
        "user_ratings_total": 0,
        "primary_type": _TYPES.get(tags.get("amenity", ""), tags.get("amenity", "")),
        "lat": lat,
        "lon": lon,
        "vicinity": tags.get("addr:full") or street or None,
        "source": "osm",
    }


async def find_places_overpass(
    lat: float, lon: float, radius: int, url: str, limit: int = 60
) -> list[dict[str, Any]]:
    """Заведения вокруг точки. При любой ошибке — [] (фолбэк не должен ронять поиск)."""
    query = (
        "[out:json][timeout:15];"
        f"nwr(around:{int(radius)},{lat},{lon})"
        '["amenity"~"^(restaurant|cafe|bar|pub|fast_food)$"]["name"];'
        f"out center {limit};"
    )
    try:
        r = await get_client().post(
            url,
            data={"data": query},
            headers={"User-Agent": "best-places-bot/1.0"},
            timeout=20.0,
        )
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except (httpx.HTTPError, ValueError) as e:
        logger.error("Overpass error: %s", e)
        return []

    places = [p for el in elements if (p := _normalize(el))]
    logger.info("Overpass: %s places", len(places))
    return places
