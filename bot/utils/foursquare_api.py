# bot/utils/foursquare_api.py
"""
Foursquare Places API (актуальный: places-api.foursquare.com).

Отличия от легаси v3 (api.foursquare.com/v3):
- хост и путь: https://places-api.foursquare.com/places/search;
- авторизация: `Authorization: Bearer <Service Key>`;
- обязательный заголовок `X-Places-Api-Version` (допустимо 2025-06-17);
- поля ответа: fsq_place_id, latitude/longitude на верхнем уровне
  (вместо fsq_id и geocodes.main);
- категории: параметр fsq_category_ids.

Один запрос на все категории (а не три). Фильтр по рейтингу здесь НЕ применяется:
сырой список кэшируется и фильтруется локально, поэтому смена порога рейтинга
не стоит ни одного нового API-вызова.

rating и stats — расширенные (Pro/Premium) поля. Если ключ их не получает и API
отвечает 4xx, запрос автоматически повторяется с базовым набором полей.
Тогда рейтинг у мест будет None (см. places_service).
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from bot.utils.http import get_client

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places-api.foursquare.com/places/search"

# Restaurant / Coffee Shop / Bar. Сверять с Category Taxonomy при смене версии API.
CATEGORY_IDS = "13065,13032,13003"

BASE_FIELDS = "fsq_place_id,name,latitude,longitude,categories,location,distance"
EXTRA_FIELDS = "rating,stats,price"

_auth_error_logged = False
# Если ключ не получает расширенные поля — час не тратим на заведомо неудачный запрос.
_EXTENDED_RETRY_AFTER_SEC = 3600
_extended_denied_until = 0.0


async def _get(params: dict[str, Any], headers: dict[str, str]) -> httpx.Response | None:
    """GET с одним повтором на сетевую ошибку / 5xx / 429."""
    client = get_client()
    for attempt in (1, 2):
        try:
            r = await client.get(SEARCH_URL, params=params, headers=headers)
        except httpx.RequestError as e:
            logger.error("FSQ request error (attempt %s): %s", attempt, e)
            r = None
        if r is not None and r.status_code not in (429, 500, 502, 503, 504):
            return r
        if attempt == 1:
            await asyncio.sleep(0.5)
    return r


def _normalize_place(p: dict[str, Any]) -> dict[str, Any]:
    """Приводит объект FSQ к внутренней схеме. Рейтинг FSQ 0–10 → 0–5."""
    loc = p.get("location") or {}
    cats = p.get("categories") or []

    raw_rating = p.get("rating")
    try:
        rating = round(float(raw_rating) / 2, 2) if raw_rating is not None else None
    except (TypeError, ValueError):
        rating = None

    return {
        "place_id": p.get("fsq_place_id"),
        "name": p.get("name"),
        "rating": rating,
        "user_ratings_total": (p.get("stats") or {}).get("total_ratings") or 0,
        "primary_type": (cats[0].get("name") if cats else None) or "",
        "lat": p.get("latitude"),
        "lon": p.get("longitude"),
        "vicinity": loc.get("formatted_address") or loc.get("address"),
        "source": "fsq",
    }


async def fetch_places(
    api_key: str,
    api_version: str,
    lat: float,
    lon: float,
    radius: int,
    lang_code: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Все заведения (restaurant/cafe/bar) вокруг точки, без фильтра по рейтингу."""
    global _auth_error_logged, _extended_denied_until

    if not api_key:
        logger.error("FSQ_API_KEY is empty")
        return []

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Places-Api-Version": api_version,
        "Accept": "application/json",
        "Accept-Language": lang_code,
    }
    params: dict[str, Any] = {
        "ll": f"{lat},{lon}",
        "radius": radius,
        "fsq_category_ids": CATEGORY_IDS,
        "limit": limit,
        "fields": BASE_FIELDS
        if time.monotonic() < _extended_denied_until
        else f"{BASE_FIELDS},{EXTRA_FIELDS}",
    }

    r = await _get(params, headers)

    # Расширенные поля недоступны по тарифу/ключу — пробуем базовый набор.
    if r is not None and r.status_code in (400, 402, 403, 422):
        logger.warning(
            "FSQ %s with extended fields (%s) — retry with base fields",
            r.status_code,
            r.text[:200],
        )
        _extended_denied_until = time.monotonic() + _EXTENDED_RETRY_AFTER_SEC
        params["fields"] = BASE_FIELDS
        r = await _get(params, headers)

    if r is None:
        return []

    if not r.is_success:
        if r.status_code in (401, 403) and not _auth_error_logged:
            _auth_error_logged = True
            logger.error(
                "FSQ auth failed (HTTP %s). Нужен Service Key из Developer Console "
                "(Bearer) и актуальный X-Places-Api-Version. Ответ: %s",
                r.status_code,
                r.text[:200],
            )
        else:
            logger.error("FSQ error: HTTP %s — %s", r.status_code, r.text[:300])
        return []

    try:
        results = r.json().get("results", [])
    except ValueError as e:
        logger.error("FSQ returned non-JSON: %s", e)
        return []

    seen: set[str] = set()
    places = []
    for p in results:
        pid = p.get("fsq_place_id")
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)
        places.append(_normalize_place(p))
    return places
