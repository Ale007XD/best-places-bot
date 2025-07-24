import httpx
import asyncio
import logging
from typing import List, Dict, Any

# ... функция fetch_places_by_type остается без изменений ...
async def fetch_places_by_type(client: httpx.AsyncClient, api_key: str, lat: float, lon: float, radius: int, place_type: str) -> List[Dict[str, Any]]:
    results_for_type = []
    url = (f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lon}&radius={radius}&type={place_type}&language=ru&key={api_key}")
    for _ in range(2):
        try:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            results_for_type.extend(data.get('results', []))
            next_page_token = data.get('next_page_token')
            if next_page_token:
                await asyncio.sleep(2)
                url = f"https://maps.googleapis.com/maps/api/place/nearbysearch/json?pagetoken={next_page_token}&key={api_key}"
            else: break
        except httpx.RequestError as e:
            logging.error(f"Ошибка при запросе типа '{place_type}': {e}")
            break
    return results_for_type


async def find_places(
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float
) -> List[Dict[str, Any]]:
    all_places = []
    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_places_by_type(client, api_key, lat, lon, radius, "restaurant"),
            fetch_places_by_type(client, api_key, lat, lon, radius, "cafe"),
        ]
        list_of_results = await asyncio.gather(*tasks)
        for sublist in list_of_results: all_places.extend(sublist)

    filtered_places = []
    seen_place_ids = set()
    for place in all_places:
        place_id = place.get('place_id')
        rating = place.get('rating')
        if place_id and place_id not in seen_place_ids and rating and float(rating) >= min_rating:
            filtered_places.append({
                "name": place.get('name', 'Название не указано'),
                "rating": float(rating),
                "address": place.get('vicinity', 'Адрес не указан'),
                "place_id": place_id
            })
            seen_place_ids.add(place_id)

    sorted_places = sorted(filtered_places, key=lambda p: p['rating'], reverse=True)
    
    # Возвращаем ВСЕХ отсортированных кандидатов, а не только топ-3
    return sorted_places
