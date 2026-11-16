# bot/diag.py
"""
Диагностика внешних сервисов бота — чтобы за секунды увидеть, что именно «отвалилось».

  • в Telegram (админ):      /diag   или   /diag premium  (+ проверка платных полей Foursquare,
                                                            тратит 1 Premium-вызов)
  • из контейнера на сервере: docker compose exec telegram-bot python -m bot.diag [premium]

Проверяет: Redis, Telegram, Foursquare (базовый уровень и, по запросу, Premium),
каждое зеркало Overpass (статус слотов + реальный маленький запрос) и Nominatim.
"""

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from bot.config import settings
from bot.utils import overpass_api as osm
from bot.utils.foursquare_api import fetch_places
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


async def _main() -> None:
    logging.basicConfig(level=logging.WARNING)
    try:
        print(format_report(await run_diagnostics(premium="premium" in sys.argv[1:])))
    finally:
        await close_client()


if __name__ == "__main__":
    asyncio.run(_main())
