# bot/utils/google_maps_api.py
# -*- coding: utf-8 -*-
"""
Интеграция с Google Places Nearby Search.

Ключевые отличия:
- Явная проверка полей 'status' и 'error_message' в ответе API.
- Пагинация до 3 страниц результатов (Google выдаёт до 60 мест по 20 на страницу).
- Неблокирующая задержка ~2.5 сек для активации next_page_token.
- Валидация наличия API-ключа до выполнения запроса.
- Единая нормализация записи места и удаление дубликатов по place_id.
- Сигнатура find_places(_, api_key, lat, lon, radius, min_rating, max_rating, lang_code).
"""

from typing import List, Dict, Any, Iterable
import asyncio
import logging

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
    Выполняет Nearby Search для одного типа мест с проверкой статуса и пагинацией.
    Возвращает «сырые» элементы Google (как приходят от API).
    """
    results_for_type: List[Dict[str, Any]] = []

    # Базовая проверка ключа ещё до запроса
    if not api_key or str(api_key).strip().lower() in ("none", ""):
        logging.error("GOOGLE_MAPS_API_KEY is empty or missing")
        return results_for_type

    # Стартовый URL: координата, радиус, тип, язык и ключ
    url = (
        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        f"?location={lat},{lon}&radius={radius}&type={place_type}"
        f"&language={lang_code}&key={api_key}"
    )

    # Google возвращает до 3 страниц. Обходим максимум 3 итерации.
    for attempt in range(3):
        try:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            data = response.json()

            status = data.get("status")
            if status == "OK":
                # Накопить результаты текущей страницы
                results_for_type.extend(data.get("results", []))
                # Проверить, есть ли следующая страница
                next_page_token = data.get("next_page_token")
                if next_page_token:
                    # Токен «созревает» спустя ~2 сек
                    await asyncio.sleep(2.5)
                    url = (
                        "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                        f"?pagetoken={next_page_token}&key={api_key}"
                    )
                    continue
                # Нет следующей страницы — выходим
                break

            if status == "ZERO_RESULTS":
                # Нет результатов для данных параметров — это не ошибка
                break

            # Любой иной статус — логируем и прекращаем
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
    Возвращает «основной» тип из списка типов места по заданному приоритету.
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
    # Если ничего из приоритета нет — берём первый же тип
    return next(iter(place_types))


def _dedupe_by_place_id(places: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Убирает дубликаты по place_id, сохраняя первый встретившийся объект.
    """
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for p in places:
        pid = p.get("place_id")
        if pid and pid not in seen:
            seen.add(pid)
            deduped.append(p)
    return deduped


def _normalize_place(p: Dict[str, Any]) -> Dict[str, Any]:
    """
    Приводит запись места к устойчивой схеме ключевых полей.
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
    Ищет рядом заведения по нескольким типам (еда/напитки), убирает дубликаты,
    фильтрует по заданному диапазону рейтинга и нормализует структуру записи.
    """
    # Базовые типы для охвата заведений питания/напитков
    place_types = ["restaurant", "cafe", "bar"]

    async with httpx.AsyncClient() as client:
        # Параллельные запросы по типам
        tasks = [
            fetch_places_by_type(
                None, client, api_key, lat, lon, radius, p_type, lang_code
            )
            for p_type in place_types
        ]
        all_results_nested = await asyncio.gather(*tasks)

    # Сплющиваем результаты
    raw_all: List[Dict[str, Any]] = [it for sub in all_results_nested for it in sub]

    # Удаляем дубликаты по place_id
    deduped = _dedupe_by_place_id(raw_all)

    # Фильтрация по рейтингу в диапазоне [min_rating, max_rating]
    def in_range(p: Dict[str, Any]) -> bool:
        r = p.get("rating")
        try:
            r_val = float(r) if r is not None else 0.0
        except (TypeError, ValueError):
            r_val = 0.0
        return float(min_rating) <= r_val <= float(max_rating)

    filtered = [p for p in deduped if in_range(p)]

    # Нормализуем выходной формат
    return [_normalize_place(p) for p in filtered]
