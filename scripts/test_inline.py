"""python scripts/test_inline.py — inline v4.2 и категории (без сети, fakeredis).

Проверяет: 3 ближайших, радиус токеном и «прилипание», радиус по умолчанию из user_prefs,
фолбэк на user_lastloc, кнопку inline_help, фильтр категории, запись user_lastloc/kind.
Запуск как smoke.py: PYTHONPATH=. BOT_TOKEN=x FSQ_API_KEY=x python scripts/test_inline.py
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis as fakeredis

from bot.handlers import account_handlers as acc
from bot.handlers import inline_handlers as ih
from bot.handlers import user_handlers as uh
from bot.utils import geocoder
from bot.utils.places_service import SearchResult, select_places
from bot.utils.querytools import classify_kind, parse_query, snap_radius

LAT, LON = 10.7769, 106.7009


def _place(i: int, dlat: float, ptype: str = "Cafe") -> dict:
    return {
        "place_id": f"p{i}",
        "name": f"Place {i}",
        "lat": LAT + dlat,
        "lon": LON,
        "primary_type": ptype,
        "rating": None,
        "source": "fsq",
    }


# разные расстояния, порядок НЕ по близости (как после ранжирования по рейтингу)
PLACES = [_place(1, 0.0030), _place(2, 0.0005), _place(3, 0.0020), _place(4, 0.0010), _place(5, 0.0040)]


class FakeInline:
    def __init__(self, text: str, uid: int = 1, loc=None):
        self.query = text
        self.from_user = SimpleNamespace(id=uid)
        self.location = loc
        self.answer = AsyncMock()


async def run_inline(redis, text: str, *, uid: int = 1, loc=None, places=PLACES):
    calls: list[dict] = []

    async def fake_search(**kw):
        calls.append(kw)
        return SearchResult(list(places), False, False)

    ih.search_places = fake_search
    acc.bot_username = AsyncMock(return_value="NearbyNinjaBot")
    acc.ref_link = AsyncMock(return_value="https://t.me/NearbyNinjaBot?start=ref_1")
    q = FakeInline(text, uid, loc)
    await ih.on_inline(q, SimpleNamespace(), "en", redis, None)
    kw = q.answer.await_args.kwargs
    return calls, kw


async def main() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    geocoder.parse_point = AsyncMock(return_value=None)  # без сети
    loc = SimpleNamespace(latitude=LAT, longitude=LON)

    # 1. 3 ближайших, по возрастанию расстояния; радиус по умолчанию 200
    calls, kw = await run_inline(redis, "", loc=loc)
    assert calls[0]["radius"] == 200 and calls[0]["kind"] == "all", calls
    assert [r.title for r in kw["results"]] == ["Place 2", "Place 4", "Place 3"], kw["results"]
    assert kw["button"].start_parameter == "inline_help" and "200" in kw["button"].text

    # 2. радиус и категория токенами; «прилипание»
    calls, kw = await run_inline(redis, "кафе 1км", loc=loc)
    assert (calls[0]["radius"], calls[0]["kind"]) == (1000, "cafe"), calls
    assert "1000" in kw["button"].text
    for text, want in (("r2000", 1000), ("1.5км", 1000), ("10км", 1000), ("300m", 500), ("120m", 200), ("60m", 100)):
        calls, _ = await run_inline(redis, text, loc=loc)
        assert calls[0]["radius"] == want, (text, calls)

    # 3. радиус по умолчанию — из user_prefs (только допустимые значения)
    await redis.hset("user_prefs:7", mapping={"radius": 500})
    calls, _ = await run_inline(redis, "бар", uid=7, loc=loc)
    assert (calls[0]["radius"], calls[0]["kind"]) == (500, "bar"), calls
    await redis.hset("user_prefs:10", mapping={"radius": 2000})  # устаревшее значение (до смены набора)
    calls, _ = await run_inline(redis, "", uid=10, loc=loc)
    assert calls[0]["radius"] == 200, calls
    await redis.hset("user_prefs:8", mapping={"radius": 777})
    calls, _ = await run_inline(redis, "", uid=8, loc=loc)
    assert calls[0]["radius"] == 200, calls

    # 4. нет геопозиции и нет lastloc → подсказка; есть lastloc → поиск от неё
    calls, kw = await run_inline(redis, "", uid=9)
    assert not calls and kw["results"][0].id == "note-no_location", kw
    await redis.set("user_lastloc:9", f"{LAT},{LON}")
    calls, kw = await run_inline(redis, "", uid=9)
    assert (calls[0]["lat"], calls[0]["lon"]) == (LAT, LON), calls
    assert len(kw["results"]) == 3

    # 5. пусто после фильтра → «ничего не нашлось»
    _, kw = await run_inline(redis, "", loc=loc, places=[])
    assert kw["results"][0].id == "note-nothing"

    # 6. парсер: остаток текста сохраняется для названия/геокодера
    assert parse_query("бар Тост 500м") == (500, "bar", "Тост")
    # «рядом» — служебное слово: не попадает в название и не ломает радиус/категорию
    assert parse_query("рядом 1км") == (1000, "all", ""), parse_query("рядом 1км")
    assert parse_query("Nearby cafe 500m") == (500, "cafe", "")
    assert parse_query("рядом") == (None, "all", "")
    assert snap_radius(1) == 100 and snap_radius(101) == 200 and snap_radius(999) == 1000
    assert snap_radius(10**6) == 1000 and snap_radius(10**6, 500) == 500

    # 7. категории в select_places: фильтр до ранжирования, other — только в «all»
    mixed = [_place(1, 0.0005, "Wine Bar"), _place(2, 0.0006, "Coffee Shop"),
             _place(3, 0.0007, "Noodle Restaurant"), _place(4, 0.0008, "Hotel")]
    assert [classify_kind(p) for p in mixed] == ["bar", "cafe", "restaurant", "other"]
    for kind, want in (("bar", 1), ("cafe", 1), ("restaurant", 1), ("all", 4)):
        res = select_places(mixed, LAT, LON, 500, 0.0, kind)
        assert len(res.places) == want, (kind, res.places)

    # 8. user_lastloc пишется только для «моей» точки; kind сохраняется в prefs
    msg = SimpleNamespace(answer=AsyncMock())
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=5, user_id=5))
    await uh._apply_point(msg, state, "en", redis, 5, LAT, LON, None, as_home=True)
    assert await redis.get("user_lastloc:5") == f"{LAT},{LON}"
    assert 0 < await redis.ttl("user_lastloc:5") <= 6 * 3600
    await uh._apply_point(msg, state, "en", redis, 5, 1.0, 2.0, "pin", as_home=False)
    assert await redis.get("user_lastloc:5") == f"{LAT},{LON}"  # временная точка не затирает

    await uh._save_prefs(redis, 5, 1000, 4.0, "bar")
    assert await uh._load_prefs(redis, 5) == (1000, 4.0, "bar")
    assert await uh._load_prefs(redis, 6) == (200, 4.0, "all")  # дефолты
    await redis.hset("user_prefs:6", mapping={"kind": "hack"})
    assert (await uh._load_prefs(redis, 6))[2] == "all"  # мусор из Redis игнорируется

    # 9. радиус: устаревшее значение → дефолт; точность геопозиции в FSM; пометки в заголовке
    assert [uh._radius_of(d) for d in ({"radius": 2000}, {"radius": "500"}, {"radius": "x"}, {})] == [200, 500, 200, 200]
    await uh._apply_point(msg, state, "en", redis, 5, LAT, LON, None, as_home=True, accuracy=35.0)
    data = await state.get_data()
    assert data["accuracy"] == 35.0 and data["home_accuracy"] == 35.0
    await uh._apply_point(msg, state, "en", redis, 5, 1.0, 2.0, "pin", as_home=False)
    data = await state.get_data()
    assert data["accuracy"] is None and data["home_accuracy"] == 35.0  # точка с карты — без точности
    from bot.utils.places_service import SearchResult

    expanded = SearchResult([], False, True, radius_asked=100, radius_used=300)
    head = uh._format_header("en", expanded, 0.0, None, False)
    assert "🔎" in head and "100" in head and "300" in head, head
    by_acc = SearchResult([], False, True, radius_asked=100, radius_used=200, by_accuracy=True)
    assert "imprecise" in uh._format_header("en", by_acc, 0.0, None, False)
    plain = SearchResult([], False, True, radius_asked=200, radius_used=200)
    assert "🔎" not in uh._format_header("en", plain, 0.0, None, False)

    # 10. «Найти блюдо»: текст-блюдо города → поиск с блюдом; кнопка блюда; inline без диакритики
    from bot.config import settings

    settings.SPECIALTIES_SHOW_UNVERIFIED = True
    real_run = uh._run_search
    runs: list = []

    async def fake_run(msg_, uid_, state_, bot_, lang_, redis_, analytics_=None, dish_ref=None):
        runs.append(dish_ref)

    def fake_message_of(callback):
        return callback.message

    real_message_of, uh._message_of = uh._message_of, fake_message_of
    uh._run_search = fake_run
    try:
        await state.update_data(latitude=21.03, longitude=105.85)  # Ханой
        m = SimpleNamespace(text="PHỞ bò", from_user=SimpleNamespace(id=42), answer=AsyncMock())
        await uh.got_text_point(m, state, SimpleNamespace(), "en", redis, None)
        assert runs == ["hanoi:pho_bo_hanoi"], runs
        await uh.got_text_point(m, state, SimpleNamespace(), "en", redis, None)  # сразу повтор — антиспам
        assert len(runs) == 1
        geocoder.geocode = AsyncMock(return_value=[])
        m2 = SimpleNamespace(text="Nguyen Hue street", from_user=SimpleNamespace(id=43), answer=AsyncMock())
        await uh.got_text_point(m2, state, SimpleNamespace(), "en", redis, None)  # адрес — по-старому
        assert len(runs) == 1 and geocoder.geocode.await_count == 1

        # кнопка блюда: неизвестное блюдо — предупреждение, известное — запуск поиска
        cb = SimpleNamespace(
            data="dish:hanoi:нет", from_user=SimpleNamespace(id=44), answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        await uh.dish_search(cb, state, SimpleNamespace(), "en", redis, None)
        assert len(runs) == 1 and cb.answer.await_args.kwargs.get("show_alert") is True
        cb.data = "dish:hanoi:bun_cha_hanoi"
        await uh.dish_search(cb, state, SimpleNamespace(), "en", redis, None)
        assert runs[-1] == "hanoi:bun_cha_hanoi", runs

        # inline: название блюда города → заведения, в названии которых оно есть (без диакритики)
        hanoi_loc = SimpleNamespace(latitude=21.03, longitude=105.85, horizontal_accuracy=None)
        pho_places = [
            {**_place(1, 0.0010), "name": "Phở Bò Hà Nội"},
            {**_place(2, 0.0005), "name": "Bún Chả 34"},
            {**_place(3, 0.0020), "name": "Cafe Moka"},
        ]
        pho_places = [{**p, "lat": 21.03 + (p["lat"] - LAT), "lon": 105.85} for p in pho_places]
        _, kw = await run_inline(redis, "pho bo", loc=hanoi_loc, places=pho_places)
        assert len(kw["results"]) == 1 and "Phở" in kw["results"][0].title, kw["results"]
    finally:
        uh._run_search, uh._message_of = real_run, real_message_of
        settings.SPECIALTIES_SHOW_UNVERIFIED = False

    # заголовок поиска блюда: без пометок про рейтинги и апселла
    res = SearchResult([], False, False, radius_asked=100, radius_used=300)
    head = uh._format_header("en", res, 4.0, "Pin", True, dish="Bún chả — Bun cha")
    assert "🍜" in head and "Bún chả" in head and "🔒" not in head and "🔎" in head and "Pin" in head, head

    # 11. _run_search с блюдом: базовый слой, радиус пользователя, настройки не затираются
    from bot.services import cards
    from bot.services import specialties as sp2

    settings.SPECIALTIES_SHOW_UNVERIFIED = True
    real_search, real_ref = uh.search_places, acc.ref_link
    try:
        await redis.flushall()
        await cards.set_layout(redis, "compact")  # без фото-карточек: один пост
        acc.ref_link = AsyncMock(return_value="https://t.me/NearbyNinjaBot?start=ref_1")
        seen: dict = {}
        hanoi_place = {**_place(1, 0.0), "name": "Phở Bò Hà Nội", "lat": 21.0305, "lon": 105.85}

        async def fake_search_dish(**kw):
            seen.update(kw)
            return SearchResult([hanoi_place], False, False, radius_asked=100, radius_used=300, provider="geoapify")

        uh.search_places = fake_search_dish
        await state.update_data(latitude=21.03, longitude=105.85, radius=100, kind="bar", min_rating=4.5,
                                accuracy=None, point_label=None)
        await redis.hset("user_prefs:50", mapping={"radius": 500, "kind": "cafe", "min_rating": 3.0})
        status = SimpleNamespace(edit_text=AsyncMock())
        m = SimpleNamespace(answer=AsyncMock(return_value=status), chat=SimpleNamespace(id=1))
        await uh._run_search(m, 50, state, SimpleNamespace(), "en", redis, None, dish_ref="hanoi:pho_bo_hanoi")
        dish = sp2.get_dish("hanoi:pho_bo_hanoi")[1]
        assert seen["dish_tokens"] == sp2.venue_needles(dish), seen
        assert (seen["kind"], seen["with_ratings"], seen["min_rating"], seen["radius"]) == ("all", False, 0.0, 100)
        text = status.edit_text.await_args.args[0]
        assert "Phở bò" in text and "100" in text and "300" in text and "🔒" not in text, text
        assert await redis.hget("user_prefs:50", "radius") == "500"  # поиск блюда настройки не меняет
        assert await redis.hget("user_prefs:50", "kind") == "cafe"

        # ничего не нашли: сообщение про блюдо и радиус, до которого дошли
        async def fake_search_none(**kw):
            return SearchResult([], False, False, radius_asked=100, radius_used=500, provider="geoapify")

        uh.search_places = fake_search_none
        status = SimpleNamespace(edit_text=AsyncMock())
        m = SimpleNamespace(answer=AsyncMock(return_value=status), chat=SimpleNamespace(id=1))
        await uh._run_search(m, 51, state, SimpleNamespace(), "en", redis, None, dish_ref="hanoi:pho_bo_hanoi")
        none_text = status.edit_text.await_args.args[0]
        assert "500 m" in none_text and "Phở bò" in none_text, none_text
    finally:
        uh.search_places, acc.ref_link = real_search, real_ref
        settings.SPECIALTIES_SHOW_UNVERIFIED = False

    # 12. переход с сайта: /start site_* учитывается в статистике, посторонние payload — нет
    stats = SimpleNamespace(track_user=AsyncMock(), track_feature_use=AsyncMock())

    async def start_with(args: str):
        m = SimpleNamespace(
            from_user=SimpleNamespace(id=77), answer=AsyncMock(),
        )
        cmd = SimpleNamespace(args=args)
        await uh.handle_start(m, state, SimpleNamespace(), cmd, "en", True, redis, stats)

    await start_with("site_guide_ru")
    assert stats.track_feature_use.await_args.args == ("site", "site_guide_ru")
    stats.track_feature_use.reset_mock()
    for junk in ("site_" + "x" * 40, "SITE_RU", "ref_abc", "site_ru;drop", ""):
        await start_with(junk)
    assert stats.track_feature_use.await_count == 0, stats.track_feature_use.await_args_list

    print("inline ok")


if __name__ == "__main__":
    asyncio.run(main())
