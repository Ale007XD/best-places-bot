# bot/utils/foursquare_api.py
# -*- coding: utf-8 -*-
"""
Интеграция с Foursquare Places API v3 (замена Google Places).

Отличия от google_maps_api.py:
- Authorization через заголовок, не query-параметр.
- Рейтинг FSQ в шкале 0–10 → нормализуем делением на 2 перед фильтрацией.
- Нет пагинации (FSQ возвращает до 50 объектов за запрос, достаточно).
- Дедупликация по fsq_id.
- Сигнатура find_places идентична google_maps_api.py — downstream-код не меняется.

Получить ключ: https://foursquare.com/developers/signup (1000 req/день бесплатно).
"""

import asyncio
import logging
from typing import Any, Dict, List

import httpx

# Маппинг: имя типа → ID категории Foursquare
# Полный список: https://docs.foursquare.com/data-products/docs/categories
CATEGORY_MAP: Dict[str, str] = {
    "restaurant": "13065",
    "cafe": "13032",
    "bar": "13003",
}


async def _fetch_by_category(
    client: httpx.AsyncClient,
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    category_id: str,
    lang_code: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Запрашивает заведения одной категории через FSQ Places Search.
    Возвращает сырые объекты из FSQ.
    """
    if not api_key or str(api_key).strip().lower() in ("none", ""):
        logging.error("FSQ_API_KEY is empty or missing")
        return []

    url = "https://api.foursquare.com/v3/places/search"
    headers = {
        "Authorization": api_key,
        # Обязательный заголовок для нового Places API (без него — 410 Gone)
        "X-Places-Api-Version": "1970-01-01",
        "Accept-Language": lang_code,
    }
    params = {
        "ll": f"{lat},{lon}",
        "radius": radius,
        "categories": category_id,
        "limit": limit,
        "fields": "fsq_id,name,rating,stats,location,categories,geocodes,price,hours",
    }

    try:
        r = await client.get(url, headers=headers, params=params, timeout=10.0)
        if not r.is_success:
            logging.error(
                "FSQ error for category %s: HTTP %s — %s",
                category_id, r.status_code, r.text[:300],
            )
            return []
        data = r.json()
        return data.get("results", [])
    except httpx.RequestError as e:
        logging.error("FSQ request error for category %s: %s", category_id, e)
        return []


def _normalize_place(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Приводит объект FSQ к той же схеме, что google_maps_api._normalize_place.
    Рейтинг делится на 2: FSQ 0–10 → 0–5 (совместимо с фильтром).
    """
    loc = p.get("location") or {}
    geo = (p.get("geocodes") or {}).get("main") or {}
    cats = p.get("categories") or []
    hours = p.get("hours") or {}

    raw_rating = p.get("rating")
    rating = round(float(raw_rating) / 2, 2) if raw_rating is not None else None

    primary_type = cats[0].get("name", "point_of_interest") if cats else "point_of_interest"

    return {
        "place_id": p.get("fsq_id"),
        "name": p.get("name"),
        "rating": rating,
        "user_ratings_total": (p.get("stats") or {}).get("total_ratings", 0),
        "types": [c.get("name", "") for c in cats],
        "primary_type": primary_type,
        "lat": geo.get("latitude"),
        "lon": geo.get("longitude"),
        "vicinity": loc.get("formatted_address") or loc.get("address"),
        "price_level": p.get("price"),
        "business_status": "OPERATIONAL",
        "opening_hours": {"open_now": hours.get("open_now")},
        "photos": [],
        "icon": None,
        "icon_background_color": None,
        "permanently_closed": None,
    }


async def find_places(
    _,
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float,
    max_rating: float,
    lang_code: str,
) -> List[Dict[str, Any]]:
    """
    Ищет заведения (restaurant / cafe / bar) через Foursquare Places API.
    Параллельные запросы по категориям, дедупликация по fsq_id,
    фильтрация по диапазону рейтинга [min_rating, max_rating] (шкала 0–5).
    Сигнатура идентична google_maps_api.find_places.
    """
    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_by_category(client, api_key, lat, lon, radius, cat_id, lang_code)
            for cat_id in CATEGORY_MAP.values()
        ]
        nested = await asyncio.gather(*tasks)

    # Дедупликация по fsq_id
    seen: set = set()
    raw: List[Dict[str, Any]] = []
    for sub in nested:
        for p in sub:
            pid = p.get("fsq_id")
            if pid and pid not in seen:
                seen.add(pid)
                raw.append(p)

    normalized = [_normalize_place(p) for p in raw]

    # Фильтр по рейтингу
    def in_range(p: Dict[str, Any]) -> bool:
        r = p.get("rating")
        try:
            r_val = float(r) if r is not None else 0.0
        except (TypeError, ValueError):
            r_val = 0.0
        return float(min_rating) <= r_val <= float(max_rating)

    return [p for p in normalized if in_range(p)]
