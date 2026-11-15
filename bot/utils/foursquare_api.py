# bot/utils/foursquare_api.py
"""
Foursquare Places API (актуальный: places-api.foursquare.com).

Отличия от легаси v3 (api.foursquare.com/v3):
- хост и путь: https://places-api.foursquare.com/places/search;
- авторизация: `Authorization: Bearer <Service Key>`;
- обязательный заголовок `X-Places-Api-Version` (допустимо 2025-06-17);
- поля ответа: fsq_place_id, latitude/longitude на верхнем уровне
  (вместо fsq_id и geocodes.main);
- категории: параметр fsq_category_ids, и ТОЛЬКО 24-символьные (BSON) ID. Старые
  числовые (13065, 13032, 13003) дают HTTP 400 "fsq_category_ids: invalid id".

Один запрос на все категории. Фильтр по рейтингу здесь НЕ применяется:
сырой список кэшируется и фильтруется локально.

rating / stats / price — Premium-поля, они тарифицируются кредитами. Если кредитов
нет (HTTP 429 «no API credits remaining») или поля недоступны по ключу (400/402/403/422),
запрос автоматически повторяется с базовыми (Pro) полями, а на час расширенные поля
отключаются. Тогда у мест нет рейтинга (rating=None), см. places_service.

При настоящей ошибке (ключ, кредиты на всё, сеть, 5xx, не-JSON) бросается
FoursquareError — оркестратор отличает «сбой провайдера» от «мест нет».
"""

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from bot.utils.http import get_client

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places-api.foursquare.com/places/search"

# "Dining and Drinking" — родительская категория (рестораны, кафе, бары, пекарни…).
# ID взят из документации Foursquare (Research Feed Schema). Если API его не принимает
# (HTTP 400 про fsq_category_ids), переходим к запросу без фильтра категорий и
# отбираем заведения локально по названию категории.
DINING_CATEGORY_ID = "63be6904847c3692a84b9bb5"
_CATEGORY_CANDIDATES: tuple[str | None, ...] = (DINING_CATEGORY_ID, None)
_category_idx = 0  # индекс рабочего варианта; запоминается между запросами

_FOOD_RE = re.compile(
    r"\b(restaurant|cafe|café|coffee|tea|bar|pub|bakery|diner|bistro|eatery|brewery|"
    r"noodle|pizza|burger|dessert|ice cream|juice|grill|steakhouse|sushi|food|snack|"
    r"pho|bbq)\b",
    re.IGNORECASE,
)

BASE_FIELDS = "fsq_place_id,name,latitude,longitude,categories,location,distance"
EXTRA_FIELDS = "rating,stats,price"

_auth_error_logged = False
# Если ключ не может получать расширенные поля — час не тратим на заведомо неудачный запрос.
_EXTENDED_RETRY_AFTER_SEC = 3600
_extended_denied_until = 0.0


class FoursquareError(Exception):
    """Провайдер недоступен или отверг запрос (в отличие от пустого, но успешного ответа)."""


def _is_credit_error(r: httpx.Response) -> bool:
    return r.status_code == 429 and "credit" in r.text.lower()


async def _get(
    params: dict[str, Any], headers: dict[str, str]
) -> httpx.Response | None:
    """GET с одним повтором на сетевую ошибку / 5xx / 429 (кроме «нет кредитов»)."""
    client = get_client()
    r: httpx.Response | None = None
    for attempt in (1, 2):
        try:
            r = await client.get(SEARCH_URL, params=params, headers=headers)
        except httpx.RequestError as e:
            logger.error("FSQ request error (attempt %s): %s", attempt, e)
            r = None
        if r is not None and (
            r.status_code not in (429, 500, 502, 503, 504) or _is_credit_error(r)
        ):
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
    """
    Все заведения (restaurant/cafe/bar) вокруг точки, без фильтра по рейтингу.
    [] — успешный ответ без мест. FoursquareError — сбой провайдера.
    """
    global _auth_error_logged, _extended_denied_until, _category_idx

    if not api_key:
        raise FoursquareError("FSQ_API_KEY is empty")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Places-Api-Version": api_version,
        "Accept": "application/json",
        "Accept-Language": lang_code,
    }

    r: httpx.Response | None = None
    category: str | None = None
    for idx in range(_category_idx, len(_CATEGORY_CANDIDATES)):
        category = _CATEGORY_CANDIDATES[idx]
        extended = time.monotonic() >= _extended_denied_until
        params: dict[str, Any] = {
            "ll": f"{lat},{lon}",
            "radius": radius,
            "limit": limit,
            "fields": f"{BASE_FIELDS},{EXTRA_FIELDS}" if extended else BASE_FIELDS,
        }
        if category:
            params["fsq_category_ids"] = category

        r = await _get(params, headers)

        # Расширенные поля недоступны (нет кредитов / тариф) — повторяем с базовыми.
        if (
            extended
            and r is not None
            and (r.status_code in (400, 402, 403, 422) or _is_credit_error(r))
            and "fsq_category_ids" not in r.text
        ):
            logger.warning(
                "FSQ %s with extended fields (%s) — retry with base fields",
                r.status_code,
                r.text[:160],
            )
            _extended_denied_until = time.monotonic() + _EXTENDED_RETRY_AFTER_SEC
            params["fields"] = BASE_FIELDS
            r = await _get(params, headers)

        # Категория отвергнута — пробуем следующий вариант (и запоминаем его).
        if (
            r is not None
            and r.status_code == 400
            and "fsq_category_ids" in r.text
            and idx + 1 < len(_CATEGORY_CANDIDATES)
        ):
            logger.warning(
                "FSQ rejected category %s (%s) — trying next", category, r.text[:120]
            )
            _category_idx = idx + 1
            continue
        break

    if r is None:
        raise FoursquareError("network error")

    if not r.is_success:
        if r.status_code in (401, 403) and not _auth_error_logged:
            _auth_error_logged = True
            logger.error(
                "FSQ auth failed (HTTP %s). Нужен Service Key из Developer Console "
                "(Bearer) и актуальный X-Places-Api-Version. Ответ: %s",
                r.status_code,
                r.text[:200],
            )
        elif _is_credit_error(r):
            logger.error(
                "FSQ: у аккаунта нет кредитов даже на базовые поля — пополните/включите "
                "автоплатёж: https://foursquare.com/developers/orgs"
            )
        else:
            logger.error("FSQ error: HTTP %s — %s", r.status_code, r.text[:300])
        raise FoursquareError(f"HTTP {r.status_code}")

    try:
        results = r.json().get("results", [])
    except ValueError as e:
        raise FoursquareError(f"non-JSON response: {e}") from e

    if category is None:
        # Без серверного фильтра приходят любые POI — оставляем еду и напитки.
        results = [
            p
            for p in results
            if any(
                _FOOD_RE.search(c.get("name") or "") for c in p.get("categories") or []
            )
        ]

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
