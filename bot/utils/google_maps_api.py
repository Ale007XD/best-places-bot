import httpx
import asyncio # <-- Добавляем импорт для асинхронной паузы
from typing import List, Dict, Any

async def find_places(
    api_key: str,
    lat: float,
    lon: float,
    radius: int,
    min_rating: float
) -> List[Dict[str, Any]]:
    """
    Асинхронно ищет заведения через Google Places API, обрабатывая пагинацию,
    чтобы собрать более полный список перед фильтрацией.
    """
    keyword = "еда"
    all_places = []
    
    # URL для первоначального запроса
    url = (
        f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        f"?location={lat},{lon}"
        f"&radius={radius}"
        f"&keyword={keyword}"
        f"&language=ru"
        f"&key={api_key}"
    )

    async with httpx.AsyncClient() as client:
        # Цикл для обработки до 3 страниц результатов (максимум от Google API)
        for _ in range(3):
            try:
                response = await client.get(url, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                
                # Добавляем найденные места в общий список
                all_places.extend(data.get('results', []))
                
                # Проверяем, есть ли следующая страница
                next_page_token = data.get('next_page_token')
                
                if next_page_token:
                    # ВАЖНО: Google API требует небольшую паузу перед запросом следующей страницы
                    await asyncio.sleep(2)
                    
                    # Формируем URL для следующей страницы
                    url = (
                        f"https://maps.googleapis.com/maps/api/place/nearbysearch/json"
                        f"?pagetoken={next_page_token}"
                        f"&key={api_key}"
                    )
                else:
                    # Если токена нет, значит, страниц больше нет, выходим из цикла
                    break
            except httpx.RequestError as e:
                print(f"Ошибка запроса к Google API: {e}")
                break # В случае ошибки прерываем сбор данных

    # Теперь, когда у нас есть полный список (до 60 заведений), фильтруем его
    filtered_places = []
    seen_place_ids = set() # Используем для удаления дубликатов

    for place in all_places:
        place_id = place.get('place_id')
        rating = place.get('rating')
        
        if place_id and place_id not in seen_place_ids and rating and float(rating) >= min_rating:
            filtered_places.append({
                "name": place.get('name', 'Название не указано'),
                "rating": float(rating),
                "address": place.eget('vicinity', 'Адрес не указан'),
                "place_id": place_id
            })
            seen_place_ids.add(place_id)

    # Сортируем по убыванию рейтинга и возвращаем не более 3-х лучших
    sorted_places = sorted(filtered_places, key=lambda p: p['rating'], reverse=True)
    return sorted_places[:3]
