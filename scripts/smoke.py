"""Smoke-тест без pytest: импорты, локали, гео, фильтрация/ранжирование,
нормализация ответа Foursquare, кэш-ключи, Redis/аналитика.

Требует env-переменных для bot.config.Settings() — в CI их задаёт workflow
(job smoke), локально: BOT_TOKEN=x FSQ_API_KEY=x.
Сетевых запросов не делает.
"""

import asyncio

import redis.asyncio as aioredis

import bot.main  # noqa: F401 — импорт и есть проверка модульного уровня
from bot.keyboards import inline_keyboards as kb
from bot.services.translator import TRANSLATIONS, detect_language, get_string
from bot.utils.analytics import Analytics
from bot.utils.foursquare_api import _normalize_place
from bot.utils.geospatial import calculate_bearing, calculate_distance
from bot.utils.places_service import _deduplicate, _make_cache_key, select_places


def check_locales() -> None:
    langs = set(TRANSLATIONS)
    assert {"ru", "en", "zh"} <= langs, f"locales missing: {langs}"

    base = set(TRANSLATIONS["ru"])
    for lang in sorted(langs):
        missing = base - set(TRANSLATIONS[lang])
        assert not missing, f"[{lang}] missing keys: {sorted(missing)}"

    assert get_string("no_results", lang="en"), "translation fallback failed"
    assert detect_language("en-US") == "en" and detect_language("zh-hans") == "zh"
    assert detect_language("vi") is None and detect_language(None) is None


def check_geospatial() -> None:
    assert calculate_distance(0, 0, 0, 0) == 0
    assert (
        abs(calculate_distance(0, 0, 0, 1) - 111_320) < 1_000
    )  # ~1° долготы на экваторе
    brg = calculate_bearing(0, 0, 1, 0)
    assert abs(brg - 0) < 1 or abs(brg - 360) < 1  # строго на север


def check_cache_key() -> None:
    k_ru = _make_cache_key(10.0, 20.0, 500, "ru")
    k_ru2 = _make_cache_key(10.0, 20.0, 500, "ru")
    k_en = _make_cache_key(10.0, 20.0, 500, "en")
    assert k_ru == k_ru2, "same args -> different key"
    assert k_ru != k_en, "lang must be part of the cache key"


def _p(name, rating, dlat=0.0, source="fsq"):
    return {
        "name": name,
        "rating": rating,
        "lat": 10.0 + dlat,
        "lon": 106.0,
        "user_ratings_total": 10,
        "source": source,
    }


def check_fsq_normalize() -> None:
    n = _normalize_place(
        {
            "fsq_place_id": "abc",
            "name": "Cafe X",
            "latitude": 10.1,
            "longitude": 106.2,
            "rating": 9.0,
            "stats": {"total_ratings": 42},
            "categories": [{"name": "Coffee Shop"}],
            "location": {"formatted_address": "1 Main St"},
        }
    )
    assert n["place_id"] == "abc" and n["lat"] == 10.1 and n["lon"] == 106.2
    assert n["rating"] == 4.5 and n["user_ratings_total"] == 42
    assert n["primary_type"] == "Coffee Shop" and n["vicinity"] == "1 Main St"
    # без премиум-полей рейтинг — None, а не 0
    assert _normalize_place({"fsq_place_id": "z", "name": "N"})["rating"] is None


def check_select() -> None:
    raw = [
        _p("A", 4.8, 0.0005),
        _p("B", 4.6, 0.0010),
        _p("C", 4.1, 0.0002),
        _p("D", 3.0, 0.0003),
        _p("far", 5.0, 0.5),  # вне радиуса
    ]
    res = select_places(raw, 10.0, 106.0, 500, 4.5)
    names = [p["name"] for p in res.places]
    assert names[:2] == ["A", "B"], names
    assert "far" not in names
    assert res.relaxed and names[2] == "C", (names, res.relaxed)  # добор до TOP_N

    strict = select_places(raw[:3], 10.0, 106.0, 500, 4.0)
    assert not strict.relaxed and len(strict.places) == 3

    # рейтингов нет вовсе → ближайшие, флаг rating_available=False
    nr = select_places([_p("U1", None, 0.001), _p("U2", None, 0.0001)], 10, 106, 500, 4.5)
    assert not nr.rating_available and nr.places

    # «любой» рейтинг: места без рейтинга не теряются
    anyr = select_places([_p("R", 4.0), _p("U", None)], 10.0, 106.0, 500, 0.0)
    assert {p["name"] for p in anyr.places} == {"R", "U"}


def check_dedup() -> None:
    a = _p("Cafe  Moka!", 4.5)
    b = _p("cafe moka", None, 0.0002, source="mapbox")  # ~22 м, то же имя
    c = _p("Cafe Moka", 4.0, 0.01)  # то же имя, но ~1.1 км — другое место
    out = _deduplicate([b, a, c])
    assert len(out) == 2, out
    assert any(p["rating"] == 4.5 for p in out), "должна остаться запись с рейтингом"


def check_overpass_and_fill() -> None:
    from bot.utils.overpass_api import _normalize as osm_norm

    n = osm_norm(
        {
            "type": "way",
            "id": 7,
            "center": {"lat": 1.0, "lon": 2.0},
            "tags": {"amenity": "cafe", "name": "X", "addr:street": "S", "addr:housenumber": "5"},
        }
    )
    assert n and n["lat"] == 1.0 and n["rating"] is None and n["vicinity"] == "5 S"
    assert n["primary_type"] == "Café" and n["place_id"] == "osm:way/7"
    assert osm_norm({"type": "node", "id": 1, "lat": 1, "lon": 2, "tags": {"amenity": "bar"}}) is None

    # мало мест с рейтингом → добираем ближайшими без рейтинга (флаг relaxed)
    raw = [_p("R", 4.8, 0.0003), _p("U1", None, 0.0002), _p("U2", None, 0.0004)]
    res = select_places(raw, 10.0, 106.0, 500, 4.5)
    assert res.places[0]["name"] == "R" and len(res.places) == 3 and res.relaxed


def check_keyboards() -> None:
    _ = lambda k: get_string(k, lang="ru")
    m = kb.get_params_keyboard(_, 500, 4.0)
    texts = [b.text for row in m.inline_keyboard for b in row]
    assert sum(t.startswith("✅") for t in texts) == 2, texts
    for row in m.inline_keyboard:
        for b in row:
            assert len((b.callback_data or "").encode()) <= 64


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
    check_fsq_normalize()
    check_select()
    check_dedup()
    check_keyboards()
    check_overpass_and_fill()
    asyncio.run(check_redis())
    print("SMOKE OK")
