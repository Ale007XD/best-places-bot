import httpx
import asyncio
from typing import List, Dict, Any

async def find_places(
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float
) -> List[Dict[str, Any]]:
    """
    Адаптивно ищет заведения через Google Places API, прекращая поиск,
    как только найдено достаточное количество релевантных мест.
    """
    keyword = "еда"
    filtered_places = []
    seen_place_ids = set()
    
    url = (
        f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        f"?location={lat},{lon}"
        f"&radius={radius}"
        f"&keyword={keyword}"
        f"&language=ru"
        f"&key={api_key}"
    )

    async with httpx.AsyncClient() as client:
        # Цикл для обработки до 3 страниц результатов
        for _ in range(3):
            try:
                response = await client.get(url, timeout=10.0)
                response.raise_for_status()
                data = response.json()

                # Фильтруем результаты с текущей страницы СРАЗУ
                for place in data.get('results', []):
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
                
                # --- УМНАЯ ПРОВЕРКА ---
                # Если мы уже набрали 3 или больше хороших заведений, нет смысла продолжать
                if len(filtered_places) >= 3:
                    break

                next_page_token = data.get('next_page_token')
                if next_page_token:
                    await asyncio.sleep(2) # Пауза нужна только если мы идем на следующую страницу
                    url = (
                        f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                        f"?pagetoken={next_page_token}"
                        f"&key={api_key}"
                    )
                else:
                    break # Страниц больше нет
            except httpx.RequestError as e:
                print(f"Ошибка запроса к Google API: {e}")
                break

    # Сортируем все, что нашли, и возвращаем лучшие 3
    sorted_places = sorted(filtered_places, key=lambda p: p['rating'], reverse=True)
    return sorted_places[:3]
