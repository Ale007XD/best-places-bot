# Best Places Bot 🥷

Telegram-бот-помощник для поиска лучших кафе, ресторанов и баров рядом с пользователем.  
Диалог: язык → геолокация → радиус → диапазон рейтинга → топ-3 заведения.

---

## Архитектура

```
bot/
├── main.py                  # Точка входа: Bot, Dispatcher, MemoryStorage, polling
├── config.py                # Pydantic Settings (.env)
├── handlers/
│   └── user_handlers.py     # FSM-диалог + сборка карточек + отправка результатов
├── keyboards/
│   └── inline_keyboards.py  # Клавиатуры: язык, радиус, рейтинг, шаринг
├── middlewares/
│   └── i18n.py              # I18nMiddleware — читает lang из Redis, пишет `_` в data
├── services/
│   └── translator.py        # Загрузка JSON-локалей, get_string()
├── utils/
│   ├── google_maps_api.py   # Nearby Search: 3 типа параллельно, пагинация, дедупликация
│   ├── geospatial.py        # Haversine-расстояние, азимут → текстовое направление
│   └── analytics.py        # Redis-счётчики: DAU, поиски, пустые результаты, фичи
└── locales/
    ├── ru.json
    ├── en.json
    └── zh.json
```

**Стек:** Python 3.11, aiogram 3.x, httpx (async), Redis, pydantic-settings, Docker Compose.

---

## Поток диалога (FSM)

```
/start
  └─► waiting_for_language      (inline: ru / en / zh)
        └─► waiting_for_location    (ReplyKeyboard с request_location)
              └─► waiting_for_radius    (inline: 50 / 100 / 200 м / вручную)
                    └─► waiting_for_rating   (inline: 4.0–4.5 / 4.41–4.7 / 4.71–5.0 / вручную)
                          └─► [поиск] → топ-3 карточки
```

Язык сохраняется в Redis (`user_lang:{user_id}`), остальные данные — в FSM-хранилище (MemoryStorage).

---

## Ключевые решения и их обоснование

### google_maps_api.py
- Три типа (`restaurant`, `cafe`, `bar`) запрашиваются **параллельно** через `asyncio.gather` — минимальное суммарное latency.
- Пагинация до 3 страниц (до 60 объектов на тип) с задержкой 2.5 с для активации `next_page_token`.
- Дедупликация по `place_id` до фильтрации — защита от пересечений типов.
- `_normalize_place()` фиксирует контракт выходной схемы: downstream-код не зависит от сырого ответа API.
- Ранняя проверка ключа избавляет от трудноотлаживаемого `INVALID_KEY` в середине цепочки.

### i18n
- Middleware читает язык из Redis **на каждый апдейт** — язык можно менять без перезапуска бота.
- `_` передаётся через `data` — хендлеры получают переводчик как зависимость (DI), без глобального состояния.

### geospatial.py
- Формула Haversine (stdlib `math`) — нет внешних зависимостей, достаточная точность для дистанций ≤ 5 км.
- `bearing_to_direction` принимает `_` как первый аргумент (переводчик), что позволяет локализовать направления.

### analytics.py
- Redis pipeline (`SADD` + `INCR` + `HINCRBY`) — все метрики за один roundtrip.
- Ключи вида `stats:*:daily:{YYYY-MM-DD}` — естественный TTL через `EXPIRE` или ручную ротацию.
- **Дефект:** `Analytics` создаёт собственное соединение с Redis вместо переиспользования соединения из `main.py`. В нагруженном сценарии это лишний connection pool.

---

## Известные дефекты

| # | Файл | Проблема | Рекомендация |
|---|------|----------|--------------|
| 1 | `main.py` | `MemoryStorage` — состояния FSM теряются при рестарте контейнера | Заменить на `RedisStorage` (aiogram поддерживает из коробки) |
| 2 | `analytics.py` | Дублирующее Redis-соединение | Принимать `redis.Redis` как параметр конструктора, передавать из `main.py` |
| 3 | `user_handlers.py` | `Analytics` не вызывается — класс объявлен, но нигде не инстанциируется | Подключить в `main.py` и передать через `data` или Bot data |
| 4 | `inline_keyboards.py` | Радиус в клавиатуре (50/100/200 м) не совпадает с комментарием в коде (200/500/1000 м) | Привести к единому значению |
| 5 | `i18n.py` | Язык берётся из Redis, но нигде не сохраняется при выборе (`set_language` в хендлере не делает `redis.set`) | Добавить `await redis_conn.set(f"user_lang:{user_id}", lang_code)` в хендлер `set_language` |
| 6 | `google_maps_api.py` | `fetch_places_by_type` и `get_primary_type` принимают `_` первым аргументом, но вызываются с `None` | Убрать `_` из сигнатур или передавать реальный переводчик |
| 7 | `docker-compose.yml` | Нет `volumes` для Redis — данные теряются при рестарте | Добавить `volumes: redis_data:/data` |
| 8 | `config.py` | `env_file='../.env'` — путь относительный, ломается при запуске не из `bot/` | Использовать `Path(__file__).parent.parent / ".env"` |

---

## Развёртывание

### Локально

```bash
cp .env.example .env   # заполнить BOT_TOKEN, GOOGLE_MAPS_API_KEY, ADMIN_ID
docker compose up --build
```

### CI/CD (GitHub Actions → VPS)

Pipeline в `.github/workflows/deploy.yml`:
1. Формирует `.env` из GitHub Secrets.
2. Копирует проект на VPS через `scp-action`.
3. Выполняет `docker compose down && docker compose up --build -d` по SSH.

Необходимые секреты: `BOT_TOKEN`, `GOOGLE_MAPS_API_KEY`, `ADMIN_ID`, `VPS_HOST`, `VPS_USERNAME`, `VPS_SSH_PRIVATE_KEY`.

---

## Переменные окружения

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен Telegram-бота (BotFather) |
| `GOOGLE_MAPS_API_KEY` | Ключ Google Places API (или замена — см. ниже) |
| `ADMIN_ID` | Telegram user_id для получения фидбэка |

---

## Замена Google Maps API для Вьетнама

### Почему Google Maps проблематичен во Вьетнаме

Google Maps API работает во Вьетнаме, однако имеет практические ограничения:
- **Покрытие заведений** в небольших городах и туристических районах значительно хуже, чем у локальных провайдеров.
- **Стоимость:** Nearby Search — $32 / 1000 запросов; при пагинации (3 страницы × 3 типа) реальная стоимость одного поиска пользователя достигает ~$0.29.
- **Латентность** от серверов во Вьетнаме до Google выше, чем до локальных CDN.

---

### Рекомендуемая замена: Foursquare Places API (FSQ)

**Почему Foursquare:**
- Отличное покрытие Юго-Восточной Азии, особенно ресторанов и кафе в Ханое, Хошимине, Дананге, Хойане.
- Бесплатный tier: **1000 запросов/день** (достаточно для MVP и тестирования).
- Платный план от $150/мес за 500k вызовов — в ~3–5× дешевле Google при аналогичных объёмах.
- REST API максимально близок к Google Places по структуре: те же `lat/lon`, `radius`, категории, рейтинг.
- Официальная документация: https://docs.foursquare.com/reference/place-search

#### Минимальный патч `google_maps_api.py` → `foursquare_api.py`

```python
# bot/utils/foursquare_api.py
import asyncio, logging
from typing import List, Dict, Any
import httpx

# Маппинг типов Google → категории Foursquare
CATEGORY_MAP = {
    "restaurant": "13065",
    "cafe":       "13032",
    "bar":        "13003",
}

async def _fetch_by_category(
    client: httpx.AsyncClient,
    api_key: str,
    lat: float, lon: float,
    radius: int,
    category_id: str,
    lang_code: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    url = "https://api.foursquare.com/v3/places/search"
    headers = {
        "Authorization": api_key,
        "Accept-Language": lang_code,
    }
    params = {
        "ll": f"{lat},{lon}",
        "radius": radius,
        "categories": category_id,
        "limit": limit,
        "fields": "fsq_id,name,rating,stats,location,categories,geocodes",
    }
    try:
        r = await client.get(url, headers=headers, params=params, timeout=10.0)
        r.raise_for_status()
        return r.json().get("results", [])
    except httpx.RequestError as e:
        logging.error("FSQ HTTP error: %s", e)
        return []


def _normalize(p: Dict[str, Any]) -> Dict[str, Any]:
    loc = p.get("location", {})
    geo = (p.get("geocodes") or {}).get("main", {})
    cats = p.get("categories") or []
    return {
        "place_id":          p.get("fsq_id"),
        "name":              p.get("name"),
        "rating":            (p.get("rating") or 0) / 2,   # FSQ: 0–10 → 0–5
        "user_ratings_total": (p.get("stats") or {}).get("total_ratings", 0),
        "types":             [c.get("name", "") for c in cats],
        "primary_type":      cats[0].get("name", "point_of_interest") if cats else "point_of_interest",
        "lat":               geo.get("latitude"),
        "lon":               geo.get("longitude"),
        "vicinity":          loc.get("formatted_address") or loc.get("address"),
        "price_level":       p.get("price"),
        "business_status":   "OPERATIONAL",
        "opening_hours":     {},
        "photos":            [],
    }


async def find_places(
    _,
    api_key: str,
    lat: float, lon: float,
    radius: int,
    min_rating: float,
    max_rating: float,
    lang_code: str,
) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        tasks = [
            _fetch_by_category(client, api_key, lat, lon, radius, cat_id, lang_code)
            for cat_id in CATEGORY_MAP.values()
        ]
        nested = await asyncio.gather(*tasks)

    seen, raw = set(), []
    for sub in nested:
        for p in sub:
            pid = p.get("fsq_id")
            if pid and pid not in seen:
                seen.add(pid)
                raw.append(p)

    normalized = [_normalize(p) for p in raw]

    return [
        p for p in normalized
        if float(min_rating) <= float(p.get("rating") or 0) <= float(max_rating)
    ]
```

**В `config.py`:** переименовать `GOOGLE_MAPS_API_KEY` → `FSQ_API_KEY`.  
**В `user_handlers.py`:** заменить импорт на `from bot.utils.foursquare_api import find_places`.

Сигнатура `find_places` идентична — остальной код не меняется.

---

### Альтернатива: OpenStreetMap + Overpass API (бесплатно, без ключа)

Подходит если бюджет нулевой или требуется полная независимость от коммерческих провайдеров.

**Плюсы:** бесплатно, данные открытые, хорошее покрытие Вьетнама (активное OSM-сообщество в Ханое и HCMC).  
**Минусы:** нет рейтингов (придётся убрать фильтр или использовать внешний источник), более сложный запрос (Overpass QL), скорость публичного сервера нестабильна — рекомендуется self-hosted Overpass или Nominatim.

```python
# Пример Overpass-запроса для ресторанов в радиусе 500 м
query = f"""
[out:json][timeout:10];
(
  node["amenity"~"restaurant|cafe|bar"](around:{radius},{lat},{lon});
  way["amenity"~"restaurant|cafe|bar"](around:{radius},{lat},{lon});
);
out center 50;
"""
url = "https://overpass-api.de/api/interpreter"
```

---

### Итоговое сравнение

| Провайдер | Покрытие VN | Рейтинги | Цена | Сложность интеграции |
|---|---|---|---|---|
| **Google Places** | Хорошее | ✅ | $$$  | Низкая (текущий код) |
| **Foursquare FSQ** | Отличное в городах | ✅ | $ (1k/день бесплатно) | Низкая (патч ~50 строк) |
| **OpenStreetMap/Overpass** | Хорошее | ❌ | Бесплатно | Средняя |

**Рекомендация:** Foursquare как прямая замена с минимальными изменениями кода и лучшим покрытием заведений в туристических зонах Вьетнама.
