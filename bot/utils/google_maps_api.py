import httpx
from typing import List, Dict, Any

async def find_places(
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float
) -> List[Dict[str, Any]]:
    """
    Асинхронно ищет заведения через Google Places API, используя ключевое слово
    для точной фильтрации, и затем фильтрует их по рейтингу.
    """
    # Используем ключевое слово "еда" для максимального охвата релевантных мест.
    # Это работает надежнее, чем перебор нескольких типов.
    keyword = "еда"
    places = []

    async with httpx.AsyncClient() as client:
        # Формируем URL для запроса к Nearby Search API с ключевым словом
        url = (
            f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
            f"?location={lat},{lon}"
            f"&radius={radius}"
            f"&keyword={keyword}"  # <-- НАШЕ ГЛАВНОЕ УЛУЧШЕНИЕ
            f"&language=ru"
            f"&key={api_key}"
        )
        try:
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            results = response.json().get('results', [])
            places = results
        except httpx.RequestError as e:
            print(f"Ошибка запроса к Google API: {e}")
            return [] # В случае ошибки возвращаем пустой список

    # Фильтруем результаты по рейтингу на нашей стороне
    filtered_places = []
    for place in places:
        rating = place.get('rating')
        if rating and float(rating) >= min_rating:
            filtered_places.append({
                "name": place.get('name', 'Название не указано'),
                "rating": float(rating),
                "address": place.get('vicinity', 'Адрес не указан'),
                "place_id": place.get('place_id')
            })

    # Сортируем по убыванию рейтинга и возвращаем не более 3-х лучших
    sorted_places = sorted(filtered_places, key=lambda p: p['rating'], reverse=True)
    return sorted_places[:3]
