# bot/utils/overpass_api.py
"""
OpenStreetMap через Overpass API: бесплатный фолбэк без ключа и кредитов.

Даёт название, тип, адрес и координаты заведений, но НЕ рейтинги — поэтому такие места
показываются «ближайшими» с пометкой (см. places_service.select_places). Для Вьетнама
покрытие OSM хорошее. Публичные серверы — fair use: результаты кэшируются, поиск
пользователя ограничен троттлингом. Публичные инстансы часто отвечают 502/таймаутом,
поэтому адресов может быть несколько (через запятую): запрос идёт на первый, а если он
не ответил за 2 с — параллельно стартует следующий; берётся первый успешный ответ.
"""

import asyncio
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


_HEDGE_DELAY_SEC = 2.0
_ATTEMPT_TIMEOUT_SEC = 8.0


async def _query_one(url: str, query: str) -> list[dict[str, Any]] | None:
    """Один сервер. None — ошибка (в отличие от успешного, но пустого ответа)."""
    try:
        r = await get_client().post(
            url,
            data={"data": query},
            headers={"User-Agent": "best-places-bot/1.0"},
            timeout=_ATTEMPT_TIMEOUT_SEC,
        )
        r.raise_for_status()
        return r.json().get("elements", [])
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Overpass %s failed: %s", url, e or type(e).__name__)
        return None


async def find_places_overpass(
    lat: float, lon: float, radius: int, url: str, limit: int = 60
) -> list[dict[str, Any]]:
    """Заведения вокруг точки. При любой ошибке — [] (фолбэк не должен ронять поиск).

    url — один адрес или несколько через запятую (зеркала).
    """
    urls = [u.strip() for u in url.split(",") if u.strip()]
    if not urls:
        return []

    query = (
        "[out:json][timeout:8];"
        f"nwr(around:{int(radius)},{lat},{lon})"
        '["amenity"~"^(restaurant|cafe|bar|pub|fast_food)$"]["name"];'
        f"out center {limit};"
    )

    async def attempt(i: int, u: str) -> list[dict[str, Any]] | None:
        if i:
            await asyncio.sleep(i * _HEDGE_DELAY_SEC)
        return await _query_one(u, query)

    tasks = [asyncio.create_task(attempt(i, u)) for i, u in enumerate(urls)]
    elements: list[dict[str, Any]] | None = None
    try:
        pending = set(tasks)
        while pending and elements is None:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                if t.result() is not None:
                    elements = t.result()
                    break
    finally:
        for t in tasks:
            t.cancel()

    if elements is None:
        logger.error("Overpass: all %s servers failed", len(urls))
        return []

    places = [p for el in elements if (p := _normalize(el))]
    logger.info("Overpass: %s places", len(places))
    return places
