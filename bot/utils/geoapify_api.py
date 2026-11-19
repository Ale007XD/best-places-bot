# bot/utils/geoapify_api.py
"""
Geoapify Places API — базовый слой (данные OpenStreetMap, ODbL): рейтингов и фото нет.

Бесплатный тариф: 3 000 кредитов в день; нужна атрибуция («Powered by Geoapify» +
© OpenStreetMap contributors) — её добавляет оркестратор по полю provider выдачи.
Данные можно кэшировать (см. OSM_CACHE_TTL), в отличие от Foursquare/Google.

Категории фильтруются на стороне API (параметр categories), поэтому выдача не «вымывается»
лимитом: бар в плотном центре не теряется среди 100 ближайших кафе. Сортировка по
расстоянию — bias=proximity.

Ключ API не должен попадать в логи: httpx кладёт полный URL в текст исключений, поэтому
ошибки логируются только по типу и HTTP-статусу.
"""

import logging
from typing import Any

import httpx

from bot.utils.http import get_client

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://api.geoapify.com/v2/places"
_TIMEOUT_SEC = 8.0

# kind → категории Geoapify (через запятую; места могут иметь несколько категорий)
KIND_CATEGORIES: dict[str, str] = {
    "cafe": "catering.cafe",
    "restaurant": "catering.restaurant,catering.fast_food,catering.food_court",
    "bar": "catering.bar,catering.pub,catering.biergarten",
}
KIND_CATEGORIES["all"] = ",".join(KIND_CATEGORIES.values())

# Метки типа — те же, что у Overpass: по ним работает классификатор категории (querytools).
_TYPE_LABELS = (
    ("catering.cafe", "Café"),
    ("catering.fast_food", "Fast food"),
    ("catering.food_court", "Food court"),
    ("catering.pub", "Pub"),
    ("catering.biergarten", "Beer garden"),
    ("catering.bar", "Bar"),
    ("catering.restaurant", "Restaurant"),
)
_OSM_TYPES = {"n": "node", "w": "way", "r": "relation"}


def _primary_type(categories: list[str]) -> str:
    for prefix, label in _TYPE_LABELS:
        if any(c == prefix or c.startswith(prefix + ".") for c in categories):
            return label
    return ""


def _place_id(props: dict[str, Any]) -> str | None:
    """Тот же id, что у Overpass (osm:way/123) — отметки пользователей не теряются при смене
    провайдера. Нет данных об объекте OSM — собственный place_id Geoapify."""
    raw = (props.get("datasource") or {}).get("raw") or {}
    osm_id, osm_type = raw.get("osm_id"), _OSM_TYPES.get(str(raw.get("osm_type", "")).lower())
    if osm_id and osm_type:
        return f"osm:{osm_type}/{osm_id}"
    return f"gp:{props['place_id']}" if props.get("place_id") else None


def _normalize(feature: dict[str, Any]) -> dict[str, Any] | None:
    props = feature.get("properties") or {}
    name = props.get("name")
    lat, lon = props.get("lat"), props.get("lon")
    if lat is None or lon is None:  # запасной путь: GeoJSON-геометрия [lon, lat]
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) >= 2:
            lon, lat = coords[0], coords[1]
    if not name or lat is None or lon is None:
        return None

    street = " ".join(x for x in (props.get("housenumber"), props.get("street")) if x)
    return {
        "place_id": _place_id(props),
        "name": name,
        "rating": None,
        "user_ratings_total": 0,
        "primary_type": _primary_type(props.get("categories") or []),
        "lat": lat,
        "lon": lon,
        "vicinity": street or props.get("address_line2") or None,
        "photo_url": None,
        "source": "osm",
    }


async def find_places_geoapify(
    lat: float,
    lon: float,
    radius: int,
    api_key: str,
    kind: str = "all",
    lang_code: str = "en",
    limit: int = 100,
    url: str = DEFAULT_URL,
) -> list[dict[str, Any]] | None:
    """Заведения вокруг точки, ближайшие первыми.

    None — сбой (ключ, лимит, сеть); [] — успешный ответ без мест.
    """
    if not api_key:
        return None
    params = {
        "categories": KIND_CATEGORIES.get(kind, KIND_CATEGORIES["all"]),
        "filter": f"circle:{lon},{lat},{int(radius)}",
        "bias": f"proximity:{lon},{lat}",
        "limit": limit,
        "lang": lang_code[:2],
        "apiKey": api_key,
    }
    try:
        r = await get_client().get(url, params=params, timeout=_TIMEOUT_SEC)
        r.raise_for_status()
        features = r.json().get("features", [])
    except httpx.HTTPStatusError as e:
        logger.warning("Geoapify HTTP %s", e.response.status_code)  # без URL: в нём ключ
        return None
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Geoapify failed: %s", type(e).__name__)
        return None

    places = [p for f in features if (p := _normalize(f))]
    logger.info("Geoapify: %s places", len(places))
    return places
