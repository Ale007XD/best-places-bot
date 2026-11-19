# bot/diag.py
"""
Диагностика внешних сервисов бота — чтобы за секунды увидеть, что именно «отвалилось».

  • в Telegram (админ):      /diag   или   /diag premium  (+ проверка платных полей Foursquare,
                                                            тратит 1 Premium-вызов)
  • из контейнера на сервере: docker compose exec telegram-bot python -m bot.diag [premium]
  • сырой ответ Foursquare:   python -m bot.diag raw [lat lon [radius]]  — что именно
                              вернул провайдер: координаты, его расстояние, адрес.
  • категории Foursquare:     python -m bot.diag cats [lat lon]  — выводит категории
                              (id и название) реальных заведений рядом, с частотой; нужны
                              для серверного фильтра cafe/bar/restaurant у Foursquare.

Проверяет: Redis, Telegram, Foursquare (базовый уровень и, по запросу, Premium),
каждое зеркало Overpass (статус слотов + реальный маленький запрос) и Nominatim.
"""

import asyncio
import json
import logging
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from bot.config import settings
from bot.utils import overpass_api as osm
from bot.utils.foursquare_api import DINING_CATEGORY_ID, SEARCH_URL, fetch_places
from bot.utils.geoapify_api import find_places_geoapify
from bot.utils.geospatial import calculate_distance
from bot.utils.http import close_client, get_client

HCMC = (10.7769, 106.7009)  # центр Хошимина: заведомо плотная точка
_UA = {"User-Agent": "best-places-bot/1.0 (diag)"}


class Check(NamedTuple):
    name: str
    ok: bool
    ms: int
    detail: str


async def _timed(name: str, fn: Callable[[], Awaitable[tuple[bool, str]]]) -> Check:
    t = time.monotonic()
    try:
        ok, detail = await fn()
    except Exception as e:  # noqa: BLE001 — диагностика должна показать любую ошибку
        ok, detail = False, f"{type(e).__name__}: {str(e)[:120]}"
    return Check(name, ok, int((time.monotonic() - t) * 1000), detail)


async def _foursquare(premium: bool) -> tuple[bool, str]:
    res = await fetch_places(
        settings.FSQ_API_KEY,
        settings.FSQ_API_VERSION,
        *HCMC,
        300,
        "en",
        limit=5,
        want_ratings=premium,
    )
    detail = f"{len(res.places)} мест, уровень полей: {res.tier}"
    if premium and not res.rated:
        return False, detail + " — Premium недоступен (нет кредитов / тариф)"
    return bool(res.places), detail


async def _geoapify() -> tuple[bool, str]:
    places = await find_places_geoapify(
        *HCMC, 300, settings.GEOAPIFY_API_KEY, limit=5, url=settings.GEOAPIFY_URL
    )
    if places is None:
        return False, "ошибка (ключ / лимит / сеть) — подробности в логах бота"
    return bool(places), f"{len(places)} мест"


async def _overpass(url: str) -> tuple[bool, str]:
    status = ""
    try:  # /api/status показывает свободные слоты и лимиты сервера (не у всех зеркал есть)
        r = await get_client().get(
            url.rsplit("/interpreter", 1)[0] + "/status", headers=_UA, timeout=6.0
        )
        lines = [ln.strip() for ln in r.text.splitlines() if "slot" in ln.lower() or "rate limit" in ln.lower()]
        status = " | ".join(lines[:2])
    except Exception:  # noqa: BLE001
        status = "status: нет ответа"

    r = await get_client().post(
        url,
        data={"data": osm.build_query(*HCMC, 300, limit=5)},
        headers=_UA,
        timeout=osm._ATTEMPT_TIMEOUT_SEC,
    )
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} · {status}"
    n = len(r.json().get("elements", []))
    return n > 0, f"{n} объектов · {status}"


async def _nominatim() -> tuple[bool, str]:
    r = await get_client().get(
        settings.GEOCODER_URL,
        params={"q": "Ben Thanh Market", "format": "jsonv2", "limit": 1},
        headers=_UA,
        timeout=8.0,
    )
    r.raise_for_status()
    return bool(r.json()), "ответ получен"


async def run_diagnostics(*, premium: bool = False, bot=None, redis_conn=None) -> list[Check]:
    tasks = []
    if redis_conn is not None:
        async def ping() -> tuple[bool, str]:
            return bool(await redis_conn.ping()), "PONG"

        tasks.append(_timed("Redis", ping))
    if bot is not None:
        async def me() -> tuple[bool, str]:
            return True, f"@{(await bot.me()).username}"

        tasks.append(_timed("Telegram getMe", me))
    tasks.append(_timed("Foursquare (базовые поля)", lambda: _foursquare(False)))
    if premium:
        tasks.append(_timed("Foursquare (рейтинги+фото, Premium)", lambda: _foursquare(True)))
    if settings.GEOAPIFY_API_KEY:
        tasks.append(_timed("Geoapify (базовый слой)", _geoapify))
    for url in [u.strip() for u in settings.OVERPASS_URL.split(",") if u.strip()]:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        tasks.append(_timed(f"Overpass {host}", lambda u=url: _overpass(u)))
    if settings.GEOCODER_ENABLED:
        tasks.append(_timed("Nominatim (адреса)", _nominatim))
    return list(await asyncio.gather(*tasks))


def format_report(checks: list[Check]) -> str:
    lines = [
        f"{'✅' if c.ok else '❌'} {c.name} — {c.ms} мс\n    {c.detail}" for c in checks
    ]
    if not settings.GEOAPIFY_API_KEY:
        lines.append(
            "ℹ️ GEOAPIFY_API_KEY не задан: базовый поиск идёт через Foursquare (без кэша) и Overpass."
        )
    pauses = osm.cooldowns()
    if pauses:
        lines.append(
            "⏸ Overpass-зеркала на паузе после сбоя: "
            + ", ".join(f"{u.split('//', 1)[-1].split('/', 1)[0]} ({s} с)" for u, s in pauses.items())
        )
    ok_osm = [c for c in checks if c.name.startswith("Overpass") and c.ok]
    if any(c.name.startswith("Overpass") for c in checks) and not ok_osm:
        lines.append("⚠️ Ни одно зеркало Overpass не отвечает — добавьте другие в OVERPASS_URL (.env).")
    return "\n".join(lines)


async def fsq_raw(lat: float, lon: float, radius: int = 500) -> list[str]:
    """Сырые места Foursquare рядом (до 10) со сверкой расстояния. Базовые поля — вызов Pro."""
    r = await get_client().get(
        SEARCH_URL,
        params={
            "ll": f"{lat},{lon}",
            "radius": radius,
            "limit": 10,
            "fields": "fsq_place_id,name,latitude,longitude,distance,location,categories",
            "fsq_category_ids": DINING_CATEGORY_ID,
            "sort": "DISTANCE",
        },
        headers={
            "Authorization": f"Bearer {settings.FSQ_API_KEY}",
            "X-Places-Api-Version": settings.FSQ_API_VERSION,
            "Accept": "application/json",
        },
        timeout=10.0,
    )
    r.raise_for_status()
    lines = []
    for p in r.json().get("results", []):
        plat, plon = p.get("latitude"), p.get("longitude")
        mine = (
            calculate_distance(lat, lon, float(plat), float(plon))
            if plat is not None and plon is not None
            else None
        )
        cats = p.get("categories") or [{}]
        lines.append(
            f"{p.get('name')!r} lat/lon={plat},{plon} fsq_distance={p.get('distance')} "
            f"by_coords={mine} addr={(p.get('location') or {}).get('formatted_address')!r} "
            f"cat={cats[0].get('name')!r} id={p.get('fsq_place_id')}"
        )
    return lines


async def fsq_categories(lat: float, lon: float) -> list[tuple[int, dict]]:
    """Категории реальных заведений вокруг точки (по частоте). Базовые поля — вызов Pro."""
    r = await get_client().get(
        SEARCH_URL,
        params={
            "ll": f"{lat},{lon}",
            "radius": 500,
            "limit": 50,
            "fields": "fsq_place_id,name,categories",
            "fsq_category_ids": DINING_CATEGORY_ID,
        },
        headers={
            "Authorization": f"Bearer {settings.FSQ_API_KEY}",
            "X-Places-Api-Version": settings.FSQ_API_VERSION,
            "Accept": "application/json",
            "Accept-Language": "en",
        },
        timeout=10.0,
    )
    r.raise_for_status()
    counter: Counter[str] = Counter()
    for place in r.json().get("results", []):
        for c in place.get("categories") or []:
            counter[json.dumps({k: v for k, v in c.items() if k != "icon"}, ensure_ascii=False)] += 1
    return [(n, json.loads(k)) for k, n in counter.most_common(60)]


async def _main() -> None:
    logging.basicConfig(level=logging.WARNING)
    args = sys.argv[1:]
    try:
        if args[:1] == ["raw"]:
            lat, lon = (float(args[1]), float(args[2])) if len(args) >= 3 else HCMC
            radius = int(args[3]) if len(args) >= 4 else 500
            print("\n".join(await fsq_raw(lat, lon, radius)) or "пусто")
            return
        if args[:1] == ["cats"]:
            lat, lon = (float(args[1]), float(args[2])) if len(args) >= 3 else HCMC
            for n, cat in await fsq_categories(lat, lon):
                print(n, json.dumps(cat, ensure_ascii=False))
            return
        print(format_report(await run_diagnostics(premium="premium" in args)))
    finally:
        await close_client()


if __name__ == "__main__":
    asyncio.run(_main())
