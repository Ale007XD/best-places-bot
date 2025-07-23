import httpx
from typing import List, Dict, Any, Optional

async def find_places(
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float
) -> List[Dict[str, Any]]:
    """
    Асинхронно ищет заведения через Google Places API и фильтрует их.
    """
    # Google Places API ищет в широкой категории, типы 'restaurant', 'cafe', 'food' покрываются 'restaurant'
    search_types = ["restaurant", "cafe", "food"]
    all_places = []
    
    # Places API не позволяет фильтровать по рейтингу в запросе, фильтруем на нашей стороне.
    # API также не позволяет указывать несколько типов, поэтому делаем несколько запросов.
    async with httpx.AsyncClient() as client:
        for place_type in search_types:
            url = (
                f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                f"?location={lat},{lon}"
                f"&radius={radius}"
                f"&type={place_type}"
                f"&language=ru"
                f"&key={api_key}"
            )
            try:
                response = await client.get(url)
                response.raise_for_status()
                results = response.json().get('results', [])
                all_places.extend(results)
            except httpx.HTTPStatusError as e:
                print(f"Ошибка запроса к Google API: {e}")
                # Можно добавить логирование или обработку ошибок
                continue

    # Фильтруем результаты по рейтингу и убираем дубликаты по place_id
    filtered_places = []
    seen_place_ids = set()

    for place in all_places:
        place_id = place.get('place_id')
        rating = place.get('rating', 0)
        
        if place_id not in seen_place_ids and rating and float(rating) >= min_rating:
            filtered_places.append({
                "name": place.get('name'),
                "rating": float(rating),
                "address": place.get('vicinity'),
                "place_id": place_id
            })
            seen_place_ids.add(place_id)
            
    # Сортируем по убыванию рейтинга и возвращаем топ-3
    sorted_places = sorted(filtered_places, key=lambda x: x['rating'], reverse=True)
    return sorted_places[:3]
