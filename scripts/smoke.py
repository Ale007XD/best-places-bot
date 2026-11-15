"""Smoke-тест без pytest: импорты, локали, гео, фильтрация/ранжирование,
нормализация ответа Foursquare, кэш-ключи, Redis/аналитика.

Требует env-переменных для bot.config.Settings() — в CI их задаёт workflow
(job smoke), локально: BOT_TOKEN=x FSQ_API_KEY=x.
Сетевых запросов не делает.
"""

import asyncio

import redis.asyncio as aioredis

import bot.main  # noqa: F401 — импорт и есть проверка модульного уровня
from bot.config import settings
from bot.keyboards import inline_keyboards as kb
from bot.services import community, products, quota
from bot.services.translator import TRANSLATIONS, detect_language, get_string
from bot.utils import geocoder, links
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

    # два РАЗНЫХ заведения одного провайдера с одним названием рядом — не дубль (сеть в одном здании)
    x = {**_p("Highlands", 4.0), "place_id": "a"}
    y = {**_p("Highlands", 4.2, 0.0002), "place_id": "b"}
    assert len(_deduplicate([x, y])) == 2
    # а запись из другого источника с тем же названием рядом — дубль
    z = {**_p("Highlands", None, 0.0001, source="osm"), "place_id": "osm:node/1"}
    assert len(_deduplicate([x, z])) == 1


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


def check_links_and_points() -> None:
    place = {"name": "Olivia", "vicinity": "14 Tran Quang Khai, Nha Trang", "lat": 12.2335, "lon": 109.1963}
    url = links.map_url(place)
    # открывает заведение по названию+адресу и центрирует карту, а не просто «голую» точку
    assert "Olivia" in url and "/@12.2335,109.1963,18z" in url and "query=12.2335" not in url, url
    assert links.map_url({"name": "X", "lat": None, "lon": None}).endswith("query=X")
    assert "query=1.0,2.0" in links.map_url({"lat": 1.0, "lon": 2.0})
    sh = links.share_url(place, "https://t.me/bot?start=ref_1", "ru")
    assert sh.startswith("https://t.me/share/url?url=") and "ref_1" in sh.replace("%3D", "=")

    P = geocoder._parse_url_coords
    assert P("https://www.google.com/maps/place/X/@12.2,109.1,17z/data=!3d12.2335!4d109.1963") == (12.2335, 109.1963)
    assert P("https://www.google.com/maps/@12.2,109.1,17z") == (12.2, 109.1)
    assert P("https://maps.google.com/?q=12.2335,109.1963") == (12.2335, 109.1963)
    assert P("https://www.openstreetmap.org/#map=18/12.2335/109.1963") == (12.2335, 109.1963)
    assert P("https://consent.google.com/?continue=https%3A%2F%2Fwww.google.com%2Fmaps%2F%4012.2%2C109.1%2C17z") == (12.2, 109.1)
    assert P("https://example.com/?q=95.5,10.1") is None  # вне диапазона широты


async def check_parse_point() -> None:
    assert await geocoder.parse_point("12.2335, 109.1963") == (12.2335, 109.1963)
    assert await geocoder.parse_point("кафе тут: 12.2335 109.1963 спасибо") == (12.2335, 109.1963)
    assert await geocoder.parse_point("geo:12.2335,109.1963") == (12.2335, 109.1963)
    assert await geocoder.parse_point("Hilton Nha Trang") is None
    assert await geocoder.parse_point("4.5 10.2") is None  # не координаты: мало знаков
    # ссылка не на белый список и без координат — сеть не трогаем
    assert await geocoder.parse_point("https://evil.example.com/x") is None


async def check_community_and_money() -> None:
    r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    uid, pid = 900_010, "smoke:place:1"
    try:
        await _wipe(r, uid, 900_011)
        await r.delete(f"place:votes:{pid}", f"place:voters:{pid}", quota.CFG_KEY)

        res = await community.record_vote(r, uid, pid, "up")
        assert (res.up, res.down, res.first_vote, res.changed) == (1, 0, True, True)
        again = await community.record_vote(r, uid, pid, "up")
        assert (again.up, again.changed, again.first_vote) == (1, False, False)  # повтор не копится
        flip = await community.record_vote(r, uid, pid, "down")
        assert (flip.up, flip.down) == (0, 1)  # смена мнения переносит голос
        await community.record_vote(r, 900_011, pid, "down")

        # «народный» рейтинг: только если нет рейтинга провайдера
        for i in range(3):
            await community.record_vote(r, 900_100 + i, pid, "up")
        got = await community.attach_votes(r, [{"place_id": pid, "rating": None}, {"place_id": pid, "rating": 4.8}])
        assert got[0]["rating_src"] == "community" and got[0]["votes"]["up"] == 3
        assert got[1]["rating"] == 4.8 and "rating_src" not in got[1]
        assert community.community_rating(0, 0) == 2.5 and community.community_rating(10, 0) > 4.5

        # награда: каждая 3-я новая отметка = +1 бонусный поиск
        rewards = 0
        for i in range(6):
            rewards += int((await community.record_vote(r, 900_011, f"smoke:p{i}", "up")).rewarded)
        assert rewards == 2, rewards
        await _wipe_pattern(r, "place:*smoke:p*")

        # переключатель монетизации (команда /money админа)
        assert (await quota.get_config(r)).enabled is settings.QUOTA_ENABLED is False  # дефолт: выкл
        await quota.set_config(r, enabled=False)
        st = await quota.get_status(r, 900_012)
        assert st.unlimited and st.can_search and not st.money_enabled
        await quota.set_config(r, enabled=True, free=1)
        st = await quota.get_status(r, 900_012)
        assert not st.unlimited and st.limit == 1 and st.money_enabled
        await quota.consume(r, 900_012, st)
        assert not (await quota.get_status(r, 900_012)).can_search
    finally:
        await r.delete(quota.CFG_KEY)
        await _wipe_pattern(r, "place:*smoke:p*")
        await _wipe(r, uid, 900_011, 900_012)
        await r.aclose()


def check_keyboards() -> None:
    _ = lambda k: get_string(k, lang="ru")
    m = kb.get_params_keyboard(_, 500, 4.0)
    texts = [b.text for row in m.inline_keyboard for b in row]
    assert sum(t.startswith("✅") for t in texts) == 2, texts
    for row in m.inline_keyboard:
        for b in row:
            assert len((b.callback_data or "").encode()) <= 64
    assert len(kb.get_params_keyboard(_, 500, 4.0, show_home=True).inline_keyboard[2]) == 2

    places = [{"place_id": "a" * 24, "lat": 1, "lon": 2}, {"place_id": "osm:relation/123456789012", "lat": 1, "lon": 2}]
    rk = kb.get_results_keyboard(_, places, ["https://m/1", "https://m/2"], ["https://s/1", "https://s/2"])
    assert [len(r) for r in rk.inline_keyboard] == [4, 4, 2]
    for row in rk.inline_keyboard:
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


async def _wipe(r, *uids: int) -> None:
    for uid in uids:
        async for k in r.scan_iter(match=f"*{uid}*"):
            await r.delete(k)


async def _wipe_pattern(r, pattern: str) -> None:
    async for k in r.scan_iter(match=pattern):
        await r.delete(k)


async def check_quota() -> None:
    """Квоты, кредиты, Pro, серия, рефералы — на реальном Redis (ключи тестовых uid)."""
    r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    uid, ref = 900_001, 900_002
    free = settings.FREE_SEARCHES_PER_DAY
    try:
        await _wipe(r, uid, ref)
        await quota.set_config(r, enabled=True)  # по умолчанию монетизация выключена

        st = await quota.get_status(r, uid)
        assert st.limit == free and st.can_search and not st.is_pro, st

        for _ in range(free):
            assert await quota.consume(r, uid, st) == "free"
        st = await quota.get_status(r, uid)
        assert not st.can_search and await quota.consume(r, uid, st) is None

        await quota.add_credits(r, uid, 2)
        st = await quota.get_status(r, uid)
        assert st.can_search and await quota.consume(r, uid, st) == "credit"
        assert (await quota.get_status(r, uid)).credits == 1

        await quota.grant_pro(r, uid, 7)
        st = await quota.get_status(r, uid)
        assert st.is_pro and st.limit == settings.PRO_DAILY_LIMIT

        # серия: сегодня идемпотентно; вчерашняя серия 2 → 3-й день даёт награду
        up = await quota.touch_streak(r, uid)
        assert up.days == 1 and up.is_new_day
        assert not (await quota.touch_streak(r, uid)).is_new_day
        from datetime import timedelta

        y = (quota.local_today() - timedelta(days=1)).isoformat()
        await r.hset(f"streak:{uid}", mapping={"days": 2, "last": y, "freezes": 0})
        before = (await quota.get_status(r, uid)).credits
        up = await quota.touch_streak(r, uid)
        assert up.days == 3 and up.milestone_bonus == quota.STREAK_MILESTONES[3]
        assert (await quota.get_status(r, uid)).credits == before + up.milestone_bonus

        # рефералы: себя нельзя; награда один раз и только за первый поиск
        assert not await quota.register_referrer(r, uid, uid)
        assert await quota.register_referrer(r, ref, uid)
        assert await quota.reward_referral_if_any(r, ref) == uid
        assert await quota.reward_referral_if_any(r, ref) is None
        assert (await quota.get_status(r, ref)).credits == settings.REFERRAL_BONUS

        # каталог товаров: цены заданы, выдача работает
        assert all(products.price(k) > 0 for k in products.PRODUCT_KEYS)
        assert (await products.grant(r, ref, "pack20"))["amount"] == products.PACK_SIZE
    finally:
        await r.delete(quota.CFG_KEY)
        await _wipe(r, uid, ref)
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
    check_links_and_points()
    asyncio.run(check_parse_point())
    asyncio.run(check_redis())
    asyncio.run(check_quota())
    asyncio.run(check_community_and_money())
    print("SMOKE OK")
