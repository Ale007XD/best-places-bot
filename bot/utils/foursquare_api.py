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

Уровни полей (тарифицируются по-разному):
  base   — Pro-поля (имя, координаты, категории, адрес);
  rating — + rating, stats, price (Premium, расходуют кредиты);
  full   — + photos (Premium).
Запрос идёт с самого богатого уровня из разрешённых и при отказе (нет кредитов —
HTTP 429 «no API credits remaining», либо поле недоступно — 400/402/403/422) спускается
ниже; отказавший уровень на час блокируется, чтобы не тратить запросы впустую.
Бесплатным пользователям (want_ratings=False) сразу запрашивается base.

При настоящей ошибке (ключ, кредиты на всё, сеть, 5xx, не-JSON) бросается
FoursquareError — оркестратор отличает «сбой провайдера» от «мест нет».
"""

import asyncio
import logging
import re
import time
from typing import Any, NamedTuple

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
# Сортировка по расстоянию: без неё API отдаёт по релевантности, и в плотном центре лимит в
# 50 мест съедают далёкие «раскрученные» заведения, а ближайшие не попадают в выдачу. Если
# API отвергнет параметр (HTTP 400 про sort) — отключаем и работаем как раньше.
_use_sort = True

_FOOD_RE = re.compile(
    r"\b(restaurant|cafe|café|coffee|tea|bar|pub|bakery|diner|bistro|eatery|brewery|"
    r"noodle|pizza|burger|dessert|ice cream|juice|grill|steakhouse|sushi|food|snack|"
    r"pho|bbq)\b",
    re.IGNORECASE,
)

BASE_FIELDS = "fsq_place_id,name,latitude,longitude,categories,location,distance"
EXTRA_FIELDS = "rating,stats,price"
_TIER_FIELDS = {
    "full": f"{BASE_FIELDS},{EXTRA_FIELDS},photos",
    "rating": f"{BASE_FIELDS},{EXTRA_FIELDS}",
    "base": BASE_FIELDS,
}
_TIER_ORDER = ("full", "rating", "base")
RATED_TIERS = ("full", "rating")

PHOTO_SIZE = "600x400"

_auth_error_logged = False
# Отказавший уровень полей час не пробуем — не тратим вызовы впустую.
_TIER_RETRY_AFTER_SEC = 3600
_tier_blocked_until: dict[str, float] = {"full": 0.0, "rating": 0.0}


class FoursquareError(Exception):
    """Провайдер недоступен или отверг запрос (в отличие от пустого, но успешного ответа)."""


class FsqResult(NamedTuple):
    places: list[dict[str, Any]]
    tier: str  # какой уровень полей реально получен: "full" | "rating" | "base"

    @property
    def rated(self) -> bool:
        return self.tier in RATED_TIERS


def ratings_available() -> bool:
    """Есть ли хоть один незаблокированный уровень с рейтингом (Premium-кредиты не отказали)."""
    now = time.monotonic()
    return any(_tier_blocked_until[t] <= now for t in RATED_TIERS)


def _first_tier(want_ratings: bool) -> str:
    if want_ratings:
        now = time.monotonic()
        for t in RATED_TIERS:
            if _tier_blocked_until[t] <= now:
                return t
    return "base"


def _next_tier(tier: str) -> str:
    now = time.monotonic()
    for t in _TIER_ORDER[_TIER_ORDER.index(tier) + 1 :]:
        if t == "base" or _tier_blocked_until[t] <= now:
            return t
    return "base"


def _is_credit_error(r: httpx.Response) -> bool:
    return r.status_code == 429 and "credit" in r.text.lower()


async def _get(params: dict[str, Any], headers: dict[str, str]) -> httpx.Response | None:
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


def _photo_url(p: dict[str, Any]) -> str | None:
    """Первая фотография места (поле photos, Premium): prefix + WxH + suffix."""
    for ph in p.get("photos") or []:
        prefix, suffix = ph.get("prefix"), ph.get("suffix")
        if prefix and suffix:
            return f"{prefix}{PHOTO_SIZE}{suffix}"
    return None


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
        "photo_url": _photo_url(p),
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
    want_ratings: bool = True,
) -> FsqResult:
    """
    Все заведения (restaurant/cafe/bar) вокруг точки, без фильтра по рейтингу.
    Пустой список — успешный ответ без мест. FoursquareError — сбой провайдера.
    """
    global _auth_error_logged, _category_idx, _use_sort

    if not api_key:
        raise FoursquareError("FSQ_API_KEY is empty")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Places-Api-Version": api_version,
        "Accept": "application/json",
        "Accept-Language": lang_code,
    }

    tier = _first_tier(want_ratings)
    idx = _category_idx
    r: httpx.Response | None = None
    category: str | None = None
    while True:
        category = _CATEGORY_CANDIDATES[idx]
        params: dict[str, Any] = {
            "ll": f"{lat},{lon}",
            "radius": radius,
            "limit": limit,
            "fields": _TIER_FIELDS[tier],
        }
        if category:
            params["fsq_category_ids"] = category
        if _use_sort:
            params["sort"] = "DISTANCE"

        r = await _get(params, headers)
        if r is None:
            break

        # Параметр sort отвергнут — отключаем его (запоминаем) и повторяем без него.
        if (
            _use_sort
            and r.status_code == 400
            and "sort" in r.text.lower()
            and "fsq_category_ids" not in r.text
        ):
            logger.warning("FSQ rejected sort=DISTANCE (%s) — disabling", r.text[:120])
            _use_sort = False
            continue

        # Категория отвергнута — следующий вариант (и запоминаем его).
        if (
            r.status_code == 400
            and "fsq_category_ids" in r.text
            and idx + 1 < len(_CATEGORY_CANDIDATES)
        ):
            logger.warning("FSQ rejected category %s (%s) — trying next", category, r.text[:120])
            idx += 1
            _category_idx = idx
            continue

        # Поля уровня недоступны (нет кредитов / тариф) — спускаемся на уровень ниже.
        if (
            tier != "base"
            and (r.status_code in (400, 402, 403, 422) or _is_credit_error(r))
            and "fsq_category_ids" not in r.text
        ):
            logger.warning(
                "FSQ %s on tier '%s' (%s) — stepping down", r.status_code, tier, r.text[:160]
            )
            until = time.monotonic() + _TIER_RETRY_AFTER_SEC
            if _is_credit_error(r):
                # Кредиты — на весь аккаунт: платные уровни (с рейтингом и с фото) недоступны оба.
                for t in RATED_TIERS:
                    _tier_blocked_until[t] = until
                tier = "base"
            else:
                _tier_blocked_until[tier] = until  # недоступно именно это поле (например, photos)
                tier = _next_tier(tier)
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
            if any(_FOOD_RE.search(c.get("name") or "") for c in p.get("categories") or [])
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
    return FsqResult(places, tier)
