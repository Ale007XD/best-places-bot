Best Places Bot

Telegram-бот для поиска лучших кафе, ресторанов и баров рядом с пользователем.
Диалог: язык → геолокация → радиус → диапазон рейтинга → топ-3 заведения.

---

Архитектура

bot/
├── main.py                  # Точка входа: Bot, Dispatcher, RedisStorage, middleware
├── config.py                # Pydantic Settings (.env)
├── handlers/
│   └── user_handlers.py     # FSM-диалог + вызов поиска + отправка карточек
├── keyboards/
│   └── inline_keyboards.py  # Inline клавиатуры
├── middlewares/
│   ├── i18n.py              # Язык из Redis → `_` в data
│   └── redis.py             # DI: прокидывает redis_conn в handlers
├── services/
│   └── translator.py        # Локализация (JSON)
├── utils/
│   ├── places_service.py    # 🔥 Оркестратор поиска (core)
│   ├── foursquare_api.py    # Foursquare API (rating + enrichment)
│   ├── mapbox_api.py        # Mapbox API (primary search)
│   ├── vietmap_api.py       # VietMap API (fallback VN)
│   ├── geospatial.py        # Distance + bearing
│   └── analytics.py         # Redis-метрики
└── locales/
    ├── ru.json
    ├── en.json
    └── zh.json

Стек: Python 3.11, aiogram 3.x, httpx (async), Redis, pydantic-settings, Docker Compose.

---

Поток диалога (FSM)

/start
  └─► waiting_for_language
        └─► waiting_for_location
              └─► waiting_for_radius
                    └─► waiting_for_rating
                          └─► search → top-3 карточки

Хранение:

- язык → Redis ("user_lang:{user_id}")
- FSM → RedisStorage

---

🔥 Ключевая архитектура (Production)

Multi-provider поиск

PlacesService
 ├── Mapbox (primary search)
 ├── Foursquare (ratings + enrichment)
 └── VietMap (fallback для Вьетнама)

Flow:

Cache → Mapbox + Foursquare → FSQ fallback → VietMap → Rank → Top-3

---

🧠 places_service.py (ядро системы)

Основные функции

- Параллельные запросы ("asyncio.gather")
- Дедупликация (name + lat + lon)
- Ranking:
  - рейтинг (приоритет)
  - расстояние
- Fallback chain:
  - расширение фильтра FSQ
  - VietMap (локальные места)
- Redis cache (TTL = 10 минут)

Формально

Result = Rank(Merge(Providers)) with Cache

---

⚡ Redis Cache

- Ключ: geo-hash (lat/lon/radius/rating)
- TTL: 600 секунд
- Хранится top-10 (для повторных запросов)

Преимущества:

- ×5–10 ускорение
- снижение API-запросов
- стабильный UX

---

🔌 Dependency Injection (Middleware)

Redis передаётся через middleware:

Update → RedisMiddleware → handler(redis_conn)

Плюсы:

- нет глобальных переменных
- соблюдение Stateless Engine
- чистая архитектура

---

📊 Провайдеры

Mapbox

- основной источник POI
- хорошее покрытие в Азии
- быстрый

Foursquare

- рейтинг (0–5)
- категории
- фильтрация

VietMap

- локальные заведения во Вьетнаме
- используется только как fallback

---

🧭 geospatial.py

- Haversine (без зависимостей)
- bearing → локализованное направление

---

📈 analytics.py

- Redis pipeline
- метрики:
  - DAU
  - search_count
  - empty_results
  - feature_usage

---

Конфигурация

Используется "pydantic-settings".

.env:

BOT_TOKEN=
FSQ_API_KEY=
MAPBOX_TOKEN=
VIETMAP_API_KEY=
ADMIN_ID=

---

Развёртывание

Локально

cp .env.example .env
docker compose up --build

---

CI/CD

GitHub Actions:

1. Генерация ".env"
2. SCP на VPS
3. Docker deploy

---

⚠️ Известные дефекты (актуально)

#| Файл| Проблема| Решение
1| keyboards| несоответствие радиусов| унифицировать
2| foursquare_api| лишний "_" аргумент| удалить
3| FSM| нет ручного ввода| добавить handlers

---

🚀 Roadmap

1. Качество поиска

- Smart merge (объединение одного POI из разных API)
- Provider scoring (FSQ > Mapbox > VietMap)

2. UX

- кнопка "Новый поиск"
- loading-индикатор
- история запросов

3. Данные

- фото (Foursquare)
- детали (часы, сайт)
- фильтр "открыто сейчас"

4. Инфраструктура

- cache hit rate
- provider metrics
- мониторинг

---

🧠 Архитектурные принципы

- FSM = детерминированная система
- Stateless Engine
- Dependency Injection через middleware
- Multi-provider orchestration
- Минимализм (без overengineering)

---

💡 Итог

Система перешла от:

Single API → Multi-provider Orchestrator

Это даёт:

- почти нулевые empty results
- лучшее покрытие во Вьетнаме
- масштабируемость без рефакторинга
