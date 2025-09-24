import asyncio
import logging
from typing import List, Dict, Any, Iterable

import httpx


async def fetch_places_by_type(
    _,
    client: httpx.AsyncClient,
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    place_type: str,
    lang_code: str,
) -> List[Dict[str, Any]]:
    """
    Выполняет Nearby Search для одного типа заведений с обработкой статуса и пагинацией до 3 страниц.
    """
    results_for_type: List[Dict[str, Any]] = []

    if not api_key or str(api_key).strip().lower() in ("none", ""):
        logging.error("GOOGLE_MAPS_API_KEY is empty or missing")
        return results_for_type

    # Стартовый запрос по координате/радиусу/типу
    url = (
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        f"?location={lat},{lon}&radius={radius}&type={place_type}"
        f"&language={lang_code}&key={api_key}"
    )

    # Google возвращает максимум 3 страницы по next_page_token
    for attempt in range(3):
        try:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            data = response.json()

            status = data.get("status")
            if status == "OK":
                results_for_type.extend(data.get("results", []))
                next_page_token = data.get("next_page_token")
                if next_page_token:
                    # next_page_token активируется через ~2 сек
                    await asyncio.sleep(2.5)
                    url = (
                        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                        f"?pagetoken={next_page_token}&key={api_key}"
                    )
                    continue
                break
            elif status == "ZERO_RESULTS":
                # Нет результатов для данного типа в радиусе
                break
            else:
                logging.error(
                    "Places error for type '%s': status=%s, error=%s",
                    place_type,
                    status,
                    data.get("error_message"),
                )
                break
        except httpx.RequestError as e:
            logging.error("HTTP error for type '%s': %s", place_type, e)
            break

    return results_for_type


def get_primary_type(_, place_types: Iterable[str]) -> str:
    """
    Возвращает основной тип из списка типов места по приоритету.
    """
    if not place_types:
        return "point_of_interest"

    priority = [
        "restaurant",
        "cafe",
        "bar",
        "bakery",
        "meal_takeaway",
        "meal_delivery",
        "food",
        "point_of_interest",
        "establishment",
    ]
    types_set = set(place_types)
    for t in priority:
        if t in types_set:
            return t
    # Если ничего из приоритета нет — берём первый
    return next(iter(place_types))


def _dedupe_by_place_id(places: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Убирает дубликаты по place_id, сохраняя первый встретившийся объект.
    """
    seen = set()
    deduped = []
    for p in places:
        pid = p.get("place_id")
        if pid and pid not in seen:
            seen.add(pid)
            deduped.append(p)
    return deduped


def _normalize_place(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Приводит запись места к устойчивой форме с ключевыми полями.
    """
    loc = (p.get("geometry") or {}).get("location") or {}
    return {
        "place_id": p.get("place_id"),
        "name": p.get("name"),
        "rating": p.get("rating"),
        "user_ratings_total": p.get("user_ratings_total"),
        "types": p.get("types") or [],
        "primary_type": get_primary_type(None, p.get("types") or []),
        "lat": loc.get("lat"),
        "lon": loc.get("lng"),
        "vicinity": p.get("vicinity") or p.get("formatted_address"),
        "price_level": p.get("price_level"),
        "business_status": p.get("business_status"),
        "opening_hours": p.get("opening_hours") or {},
        "photos": p.get("photos") or [],
        "icon": p.get("icon"),
        "icon_background_color": p.get("icon_background_color"),
        "permanently_closed": p.get("permanently_closed"),
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
    Ищет места рядом по нескольким типам и фильтрует по рейтингу в диапазоне [min_rating, max_rating].
    Возвращает нормализованный список без дубликатов.
    """
    # Базовый набор типов для еды/напитков; по желанию можно расширить.
    place_types = ["restaurant", "cafe", "bar"]

    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_places_by_type(
                None, client, api_key, lat, lon, radius, p_type, lang_code
            )
            for p_type in place_types
        ]
        all_results_nested = await asyncio.gather(*tasks)

    # Плоский список всех результатов по типам
    all_results: List[Dict[str, Any]] = [
        item for sublist in all_results_nested for item in sublist
    ]

    # Убираем дубликаты по place_id
    deduped = _dedupe_by_place_id(all_results)

    # Фильтрация по рейтингу (None считаем как 0.0)
    def in_rating_range(p: Dict[str, Any]) -> bool:
        r = p.get("rating")
        try:
            r_val = float(r) if r is not None else 0.0
        except (TypeError, ValueError):
            r_val = 0.0
        return (r_val >= float(min_rating)) and (r_val <= float(max_rating))

    filtered = [p for p in deduped if in_rating_range(p)]

    # Возвращаем нормализованные записи
    return [_normalize_place(p) for p in filtered]
