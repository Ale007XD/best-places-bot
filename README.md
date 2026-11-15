# Best Places Bot

Telegram-бот: топ-3 кафе, ресторана и бара рядом с пользователем.

**Диалог:** геопозиция → один экран параметров (радиус + минимальный рейтинг +
«Искать») → одно сообщение с тремя местами и кнопками «открыть на карте».
Язык определяется автоматически (по языку Telegram), параметры запоминаются.

Стек: Python 3.11, aiogram 3.x, httpx, Redis, pydantic-settings, Docker Compose.

---

## Быстрый запуск

```bash
cp .env.example .env      # заполнить BOT_TOKEN и FSQ_API_KEY — больше ничего не нужно
docker compose up -d --build
docker compose logs -f telegram-bot
```

Обязательные переменные — только `BOT_TOKEN` и `FSQ_API_KEY` (Service Key из
Foursquare Developer Console). Остальное опционально, см. `.env.example`.

Проверка ключа Foursquare одной командой:

```bash
curl -s "https://places-api.foursquare.com/places/search?ll=10.7769,106.7009&radius=500&limit=1&fsq_category_ids=63be6904847c3692a84b9bb5" \
  -H "Authorization: Bearer $FSQ_API_KEY" -H "X-Places-Api-Version: 2025-06-17"
```

Команды бота: `/start`, `/language`, `/feedback` и `/stats` (последние две — если задан `ADMIN_ID`).

---

## Архитектура

```
bot/
├── main.py                 # Bot, Dispatcher, RedisStorage (TTL 6 ч), graceful shutdown
├── config.py               # pydantic-settings (.env)
├── handlers/user_handlers.py   # диалог, экран параметров, поиск, /feedback, /stats
├── keyboards/inline_keyboards.py
├── middlewares/i18n.py     # язык (Redis → язык Telegram), redis_conn в handlers
├── services/translator.py  # локализация (JSON)
├── utils/
│   ├── places_service.py   # оркестратор: кэш → Foursquare → (аварийно) Mapbox/VietMap → ранжирование
│   ├── foursquare_api.py   # Places API (places-api.foursquare.com, Bearer), 1 запрос на поиск
│   ├── mapbox_api.py       # аварийный фолбэк
│   ├── vietmap_api.py      # аварийный фолбэк (эндпоинт не сверен с документацией!)
│   ├── http.py             # общий httpx-клиент
│   ├── geospatial.py       # расстояние / азимут
│   └── analytics.py        # Redis-метрики (TTL 90 дней)
└── locales/{ru,en,zh}.json
```

### Поиск

1. Кэш в Redis (10 мин) хранит **сырой** список мест по точке/радиусу/языку —
   без рейтинга в ключе, поэтому смена порога рейтинга не тратит API-вызовов.
2. Один запрос к Foursquare на все категории (restaurant / coffee shop / bar).
   Если поля `rating`/`stats` недоступны по ключу (4xx), запрос автоматически
   повторяется с базовыми полями; тогда бот показывает ближайшие места с пометкой.
3. Mapbox/VietMap вызываются **только** если Foursquare вернул пусто и заданы ключи.
4. Дедупликация (нормализованное имя + расстояние ≤ 75 м) → фильтр по радиусу →
   фильтр по рейтингу → ранжирование (0.7·рейтинг/5 + 0.3·близость).
   Если мест с нужным рейтингом меньше трёх, добираются лучшие из остальных
   (с пометкой), а не пустой ответ.

### Хранение данных

Геопозиция и параметры лежат в FSM (Redis) не дольше 6 часов; в `user_prefs:{id}`
сохраняются только радиус и порог рейтинга; координаты не пишутся никуда, кроме FSM.

---

## Проверки

```bash
pip install -r requirements-dev.txt
ruff check bot/ scripts/
BOT_TOKEN=1:x FSQ_API_KEY=x python -m scripts.smoke   # нужен Redis на localhost:6379
```

## Известные ограничения

- Категории Foursquare: новый API принимает только 24-символьные ID (числовые вроде 13065 дают HTTP 400). Используется родитель «Dining and Drinking»; если и он отвергнут — запрос без фильтра и отбор по названию категории.
- Эндпоинт VietMap не подтверждён документацией — это аварийный фолбэк.
- `rating`/`stats` — расширенные поля Foursquare; их доступность зависит от тарифа ключа.
