"""python -m scripts.test_dish_matching — «Найти блюдо»: ложные матчи («Phố» ≠ «Phở») и глубокая выборка Geoapify.

Проверяет без сети: (1) таблицу «название заведения → найдено ли блюдо» с диакритикой;
(2) весь путь search_places → select_places на реальном блюде словаря hanoi:pho_bo_hanoi
(улица «Phố» не должна попадать в выдачу «Phở bò»); (3) лимит Geoapify в поиске блюда выше,
чем в обычном, кэш у них раздельный, признак обрезки набора; (4) inline-фильтр и обработчик
«Найти блюдо» получают написания с диакритикой.
Запуск: BOT_TOKEN=x FSQ_API_KEY=x python -m scripts.test_dish_matching
"""

import asyncio
import inspect

import fakeredis.aioredis as fakeredis

from bot.config import settings
from bot.handlers import inline_handlers, user_handlers
from bot.services import specialties as sp
from bot.utils import places_service as ps
from bot.utils.textnorm import matches

LAT, LON = 21.0285, 105.8542  # центр Ханоя; ячейка кэша — как в продакшене
SPELL = ("Phở bò", "phở", "pho")

# (название, ожидание для блюда «phở») — из реального вывода diag dish hanoi:pho_bo_hanoi
TABLE = [
    ("Pho 10", True),
    ("Phở Hòa", True),
    ("PHỞ THÌN", True),
    ("Quán Phở-Ngon", True),
    ("Pho ga ta", True),
    ("Cửa Hàng Bánh Mì Phố", False),  # «Phố» — улица
    ("Phố Cổ Quán", False),
    ("Phó Quán", False),
    ("Phong Lan Cafe", False),
]


def _place(i: int, name: str, dlat: float) -> dict:
    return {
        "place_id": f"osm:node/{i}",
        "name": name,
        "unnamed": False,
        "lat": LAT + dlat,
        "lon": LON,
        "primary_type": "Restaurant",
        "rating": None,
        "user_ratings_total": 0,
        "source": "osm",
    }


def _install(calls: list[dict], places: list[dict]) -> None:
    async def fake_gp(lat, lon, radius, api_key, kind="all", lang_code="en", limit=100, **_kw):
        calls.append({"radius": radius, "kind": kind, "limit": limit})
        return list(places)

    async def fake_reverse(lat, lon, api_key, **_kw):
        return None

    ps.find_places_geoapify = fake_gp
    ps.reverse_region = fake_reverse


def _names(result: ps.SearchResult) -> set[str]:
    return {p["name"] for p in result.places}


async def main() -> None:
    # 1. таблица: с написаниями диакритика различается, без них — прежнее поведение
    for name, want in TABLE:
        assert matches(name, ("pho",), SPELL) is want, (name, want)
    assert matches("Cửa Hàng Bánh Mì Phố", ("pho",))  # без spellings — как раньше (обратная совместимость)
    assert matches("Bún Chả Hương Liên", ("bun cha",), ("Bún chả",)) and matches("Bun Cha 34", ("bun cha",), ("Bún chả",))
    assert not matches("Bún Chã", ("bun cha",), ("Bún chả",))
    assert matches("ร้านผัดไทยประตูผี", ("ผัดไทย",), ("ผัดไทย",))  # тайский — подстрокой, без проверки знаков
    assert matches("Phố Phở", ("pho",), SPELL)  # хотя бы одно «своё» слово — совпадение

    # 2. реальное блюдо словаря: spellings берутся из словаря, а не из теста
    _city, dish = sp.get_dish("hanoi:pho_bo_hanoi")
    needles, spellings = sp.venue_needles(dish), sp.venue_spellings(dish)
    assert needles == ("pho",) and any("ở" in s for s in spellings), spellings
    for name, want in TABLE:
        assert matches(name, needles, spellings) is want, (name, want, spellings)

    # 3. сквозной путь search_places: 100–200 м от точки, Phố не попадает в выдачу блюда
    redis = fakeredis.FakeRedis(decode_responses=True)
    settings.GEOAPIFY_API_KEY = "secret-key"
    settings.BASE_PROVIDERS = "geoapify"
    settings.OVERPASS_ENABLED = False
    settings.GEOAPIFY_DISH_LIMIT = 300
    places = [_place(i, n, 0.0004 * (i + 1)) for i, (n, _w) in enumerate(TABLE)]
    calls: list[dict] = []
    _install(calls, places)
    good = {n for n, w in TABLE if w}
    bad = {n for n, w in TABLE if not w}
    res = await ps.search_places(
        LAT, LON, 500, 0, "en", redis, with_ratings=False,
        dish_tokens=needles, dish_spellings=spellings,
    )  # fmt: skip
    assert _names(res) == good, _names(res) ^ good
    assert not _names(res) & bad
    # контроль: без spellings улица попала бы в выдачу — тест ловит откат исправления
    old = await ps.search_places(
        LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=needles
    )
    assert "Cửa Hàng Bánh Mì Phố" in _names(old) and "Phố Cổ Quán" in _names(old)

    # 4. глубокая выборка: поиск блюда берёт лимит GEOAPIFY_DISH_LIMIT, обычный — 100; кэш раздельный
    await redis.flushall()
    calls.clear()
    await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)
    assert [c["limit"] for c in calls] == [100], calls
    await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=needles)
    assert [c["limit"] for c in calls] == [100, 300], calls  # набор из 100 мест не подменяет глубокий
    await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=needles)
    await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)
    assert len(calls) == 2, calls  # оба набора закэшированы по отдельности
    settings.GEOAPIFY_DISH_LIMIT = 9999  # больше потолка Geoapify — режется до 500
    assert ps.provider_limit("geoapify", deep=True) == 500
    settings.GEOAPIFY_DISH_LIMIT = 10  # меньше обычного — не ниже обычного
    assert ps.provider_limit("geoapify", deep=True) == 100
    settings.GEOAPIFY_DISH_LIMIT = 300
    assert ps.provider_limit("geoapify") == 100 and ps.provider_limit("fsq", deep=True) == 0

    # 5. признак обрезки: набор у лимита — truncated, и в обычном, и в глубоком
    assert ps.is_truncated("geoapify", 100) and not ps.is_truncated("geoapify", 60)
    assert not ps.is_truncated("geoapify", 100, deep=True) and ps.is_truncated("geoapify", 298, deep=True)
    assert ps.is_truncated("overpass", 300) and not ps.is_truncated("fsq", 999)
    await redis.flushall()
    many = [_place(i, f"Quán {i}", 0.00001 * i) for i in range(300)]
    _install(calls, many)
    full = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=("quan",))
    assert full.truncated, "набор из 300 мест при лимите 300 — обрезан"
    await redis.flushall()
    _install(calls, many[:20])
    short = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=("quan",))
    assert not short.truncated
    again = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=("quan",))
    assert again.from_cache and not again.truncated  # признак считается и для набора из кэша

    # 6. проводка: inline-фильтр и обработчик «Найти блюдо» передают написания
    assert not inline_handlers._matches({"name": "Cửa Hàng Bánh Mì Phố"}, needles, spellings)
    assert inline_handlers._matches({"name": "Phở Hòa"}, needles, spellings)
    inline_src = inspect.getsource(inline_handlers.on_inline)
    assert "venue_spellings" in inline_src and "_matches(p, needles, spellings)" in inline_src
    run_src = inspect.getsource(user_handlers._run_search)
    assert "dish_spellings=spellings" in run_src and "venue_spellings" in run_src

    print("dish_matching ok")


if __name__ == "__main__":
    asyncio.run(main())
