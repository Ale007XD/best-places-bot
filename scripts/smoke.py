"""Smoke-тест без pytest: импорты, локали, гео, кэш-ключи, Redis/аналитика.

Требует env-переменных для bot.config.Settings() — в CI их задаёт workflow
(job smoke), локально: BOT_TOKEN=x FSQ_API_KEY=x MAPBOX_TOKEN=x VIETMAP_API_KEY=x ADMIN_ID=1.
"""

import asyncio

import redis.asyncio as aioredis

import bot.main  # noqa: F401 — импорт и есть проверка модульного уровня
from bot.services.translator import TRANSLATIONS, get_string
from bot.utils.analytics import Analytics
from bot.utils.geospatial import calculate_bearing, calculate_distance
from bot.utils.places_service import _make_cache_key


def check_locales() -> None:
    langs = set(TRANSLATIONS)
    assert {"ru", "en", "zh"} <= langs, f"locales missing: {langs}"

    base = set(TRANSLATIONS["ru"])
    for lang in sorted(langs):
        missing = base - set(TRANSLATIONS[lang])
        assert not missing, f"[{lang}] missing keys: {sorted(missing)}"

    assert get_string("no_results", lang="en"), "translation fallback failed"


def check_geospatial() -> None:
    assert calculate_distance(0, 0, 0, 0) == 0
    assert (
        abs(calculate_distance(0, 0, 0, 1) - 111_320) < 1_000
    )  # ~1° долготы на экваторе
    brg = calculate_bearing(0, 0, 1, 0)
    assert abs(brg - 90) < 1  # строго на восток


def check_cache_key() -> None:
    k_ru = _make_cache_key(10.0, 20.0, 500, 4.5, 4.7, "ru")
    k_ru2 = _make_cache_key(10.0, 20.0, 500, 4.5, 4.7, "ru")
    k_en = _make_cache_key(10.0, 20.0, 500, 4.5, 4.7, "en")
    assert k_ru == k_ru2, "same args -> different key"
    assert k_ru != k_en, "lang must be part of the cache key"


async def check_redis() -> None:
    r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    try:
        await r.set("smoke:ping", "pong")
        assert await r.get("smoke:ping") == "pong"

        a = Analytics(redis_conn=r)
        await a.track_user(42)
        await a.track_search_request()
        stats = await a.get_today_stats()
        assert stats["searches"] >= 1 and stats["active_users"] >= 1, stats
    finally:
        await r.aclose()


if __name__ == "__main__":
    check_locales()
    check_geospatial()
    check_cache_key()
    asyncio.run(check_redis())
    print("SMOKE OK")
