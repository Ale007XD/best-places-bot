"""python -m scripts.test_providers — провайдеры, кэш по ячейке, изоляция источников (без сети).

Проверяет: кэш только для OSM-провайдеров (по ячейке ~100 м), Foursquare — всегда живой
запрос и ничего не пишет в Redis, один запрос = один провайдер (без смешивания), выбор
провайдера с большим числом мест, SearchUnavailable, ключ Geoapify не попадает в логи,
fallback при отказе FSQ от sort=DISTANCE.
Запуск как smoke.py: BOT_TOKEN=x FSQ_API_KEY=x python -m scripts.test_providers
"""

import asyncio
import json
import logging

import fakeredis.aioredis as fakeredis
import httpx

from bot.config import settings
from bot.utils import foursquare_api as fsq
from bot.utils import geoapify_api as gp
from bot.utils import geocoder, links, overpass_api
from bot.utils import places_service as ps
from bot.utils.attribution import attribution

LAT, LON = 10.77690, 106.70090  # центр ячейки 10.777 / 106.701 (округление до 3 знаков)


def _pl(i: int, dlat: float, source: str, rating: float | None = None) -> dict:
    return {
        "place_id": f"{source}{i}",
        "name": f"{source.upper()} {i}",
        "lat": LAT + dlat,
        "lon": LON,
        "primary_type": "Cafe",
        "rating": rating,
        "user_ratings_total": 5 if rating else 0,
        "source": source,
    }


class Calls:
    def __init__(self) -> None:
        self.gp: list[dict] = []
        self.op: list[dict] = []
        self.fsq: list[dict] = []


def install(calls: Calls, *, gp_places, op_places, fsq_places, fsq_rated=True) -> None:
    async def fake_gp(lat, lon, radius, api_key, kind="all", lang_code="en", **_kw):
        calls.gp.append({"lat": lat, "lon": lon, "radius": radius, "kind": kind})
        return None if gp_places is None else list(gp_places)

    async def fake_op(lat, lon, radius, url, limit=300, kind="all"):
        calls.op.append({"lat": lat, "lon": lon, "radius": radius, "kind": kind})
        return None if op_places is None else list(op_places)

    async def fake_fsq(api_key, version, lat, lon, radius, lang, limit=50, want_ratings=True):
        calls.fsq.append({"lat": lat, "lon": lon, "radius": radius, "ratings": want_ratings})
        if fsq_places is None:
            raise ps.FoursquareError("down")
        return fsq.FsqResult(list(fsq_places), "full" if (fsq_rated and want_ratings) else "base")

    ps.find_places_geoapify = fake_gp
    ps.find_places_overpass = fake_op
    ps.fsq_fetch = fake_fsq
    ps.ratings_available = lambda: True

    async def fake_reverse(lat, lon, api_key, **_kw):
        return None

    ps.reverse_region = fake_reverse


async def keys(redis) -> list[str]:
    return [k async for k in redis.scan_iter("places:*")]


async def main() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)
    settings.GEOAPIFY_API_KEY = "secret-key"
    settings.BASE_PROVIDERS = "geoapify,fsq,overpass"
    settings.OVERPASS_ENABLED = True
    gp5 = [_pl(i, 0.0003 * i, "osm") for i in range(1, 6)]
    fsq5 = [_pl(i, 0.0003 * i, "fsq", 4.0 + i / 10) for i in range(1, 6)]

    # 1. база: Geoapify, кэш по ячейке; другая точка в той же ячейке — без нового вызова API
    c = Calls()
    install(c, gp_places=gp5, op_places=[], fsq_places=fsq5)
    r1 = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)
    assert r1.provider == "geoapify" and not r1.from_cache and r1.used_osm and not r1.rated
    assert len(c.gp) == 1 and not c.fsq and not c.op
    assert (c.gp[0]["lat"], c.gp[0]["lon"]) == (10.777, 106.701)  # запрос из центра ячейки
    assert c.gp[0]["radius"] == 600  # 200 + запас → «ведро» 600
    r2 = await ps.search_places(LAT + 0.0004, LON + 0.0004, 200, 0, "en", redis, with_ratings=False)
    assert r2.from_cache and r2.provider == "geoapify" and len(c.gp) == 1
    await ps.search_places(LAT + 0.01, LON, 200, 0, "en", redis, with_ratings=False)  # другая ячейка
    assert len(c.gp) == 2
    await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False, kind="bar")
    assert len(c.gp) == 3 and c.gp[-1]["kind"] == "bar"  # категория — часть ключа кэша
    assert ps.fetch_radius(100) == ps.fetch_radius(500) == 600
    assert ps.fetch_radius(1000) == 1100 and ps.fetch_radius(2000) == 2100

    # 2. изоляция: Geoapify дал 2 места, FSQ — 5 → целиком FSQ, без смешивания; в Redis пусто
    await redis.flushall()
    c = Calls()
    install(c, gp_places=gp5[:2], op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)
    assert r.provider == "fsq" and {p["source"] for p in r.places} == {"fsq"}, r.places
    assert not r.used_osm and not r.rated and c.fsq[0]["ratings"] is False
    stored = [json.loads(await redis.get(k)) for k in await keys(redis)]
    assert all(p["source"] != "fsq" for lst in stored for p in lst), "ответы FSQ в Redis"

    # 3. платный поиск: FSQ живой, ничего не кэшируется, повтор снова ходит в API
    await redis.flushall()
    c = Calls()
    install(c, gp_places=gp5, op_places=[], fsq_places=fsq5)
    for _ in range(2):
        r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=True)
        assert r.provider == "fsq" and r.rated and not r.from_cache
    assert len(c.fsq) == 2 and not c.gp and not await keys(redis)

    # 4. платный поиск, FSQ дал 2 места, Geoapify — 5 → Geoapify целиком, rated=False
    c = Calls()
    install(c, gp_places=gp5, op_places=[], fsq_places=fsq5[:2])
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=True)
    assert r.provider == "geoapify" and not r.rated and len(c.fsq) == 1 and len(c.gp) == 1
    # ... но если и база пуста — остаются 2 места FSQ с рейтингом
    await redis.flushall()
    c = Calls()
    install(c, gp_places=[], op_places=[], fsq_places=fsq5[:2])
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=True)
    assert r.provider == "fsq" and r.rated and len(r.places) == 2

    # 5. сбои: все провайдеры упали → SearchUnavailable; пустой успешный ответ ≠ сбой
    await redis.flushall()
    c = Calls()
    install(c, gp_places=None, op_places=None, fsq_places=None)
    try:
        await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)
        raise AssertionError("ожидался SearchUnavailable")
    except ps.SearchUnavailable:
        pass
    assert len(c.gp) == 1 and len(c.fsq) == 1 and len(c.op) == 1  # прошли всю цепочку
    c = Calls()
    install(c, gp_places=[], op_places=None, fsq_places=None)
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)
    assert r.places == [] and r.fsq_ok  # Geoapify ответил: «мест нет», а не «сбой»

    # 6. без ключа Geoapify провайдер пропускается; порядок задаётся BASE_PROVIDERS
    await redis.flushall()
    settings.GEOAPIFY_API_KEY = ""
    c = Calls()
    install(c, gp_places=gp5, op_places=gp5, fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)
    assert not c.gp and r.provider == "fsq"
    settings.BASE_PROVIDERS = "overpass,fsq"
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)
    assert r.provider == "overpass" and c.op[0]["kind"] == "all"
    settings.GEOAPIFY_API_KEY = "secret-key"
    settings.BASE_PROVIDERS = "geoapify,fsq,overpass"

    # 7. атрибуция по провайдеру
    assert "Geoapify" in attribution("geoapify") and "OpenStreetMap" in attribution("geoapify")
    assert attribution("overpass") == "🗺 © OpenStreetMap contributors"
    assert attribution("fsq") == "📍 Powered by Foursquare" and attribution("") == ""
    assert "OpenStreetMap" in attribution("", [{"source": "osm"}])

    # 8. Geoapify: разбор ответа, запрос, ключ не попадает в логи
    feature = {
        "properties": {
            "name": "Тосты",
            "lat": 10.7771,
            "lon": 106.7011,
            "categories": ["catering", "catering.bar"],
            "address_line2": "105 улица Бограда",
            "place_id": "abc",
            "datasource": {"raw": {"osm_id": 77, "osm_type": "w"}},
        }
    }
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.params.get("limit") == "1":
            return httpx.Response(401, json={"message": "bad key"})
        return httpx.Response(200, json={"features": [feature, {"properties": {"lat": 1, "lon": 2}}]})

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    cap = _Capture()
    gp.logger.addHandler(cap)
    gp.logger.setLevel(logging.INFO)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gp.get_client = lambda: client
        found = await gp.find_places_geoapify(10.777, 106.701, 600, "secret-key", "bar", "ru")
        assert found and len(found) == 1 and found[0]["place_id"] == "osm:way/77"
        assert found[0]["primary_type"] == "Bar" and found[0]["vicinity"] == "105 улица Бограда"
        q = seen[0].params
        assert q["categories"] == gp.KIND_CATEGORIES["bar"] and q["lang"] == "ru"
        assert q["filter"] == "circle:106.701,10.777,600" and q["bias"] == "proximity:106.701,10.777"
        assert await gp.find_places_geoapify(10.0, 106.0, 100, "secret-key", limit=1) is None
        assert await gp.find_places_geoapify(10.0, 106.0, 100, "") is None
    assert cap.lines and not any("secret-key" in ln for ln in cap.lines), cap.lines

    # 9. Overpass: фильтр категории в запросе, лимит с запасом
    q = overpass_api.build_query(10.0, 106.0, 300, kind="bar")
    assert "bar|pub" in q and "out center 300" in q and "cafe" not in q

    # 10. FSQ: отказ от sort=DISTANCE → повтор без него, параметр отключается
    sent: list[dict] = []
    answers = [
        httpx.Response(400, text='{"message":"invalid parameter: sort"}'),
        httpx.Response(200, json={"results": [{"fsq_place_id": "x", "name": "N",
                                                "categories": [{"name": "Cafe"}]}]}),
    ]

    async def fake_get(params, headers):
        sent.append(dict(params))
        return answers.pop(0)

    real_get = fsq._get
    fsq._get = fake_get
    try:
        res = await fsq.fetch_places("k", "v", 10.0, 106.0, 300, "en", want_ratings=False)
    finally:
        fsq._get = real_get
        used_sort = fsq._use_sort
        fsq._use_sort = True
    assert sent[0]["sort"] == "DISTANCE" and "sort" not in sent[1], sent
    assert len(res.places) == 1 and used_sort is False

    # 10b. FSQ: date_closed запрашивается; закрытое место помечается closed_at; отказ API от
    # поля → повтор без него, поле отключается (поиск не ломается)
    sent = []
    answers = [
        httpx.Response(400, text='{"message":"invalid field: date_closed"}'),
        httpx.Response(200, json={"results": [
            {"fsq_place_id": "a", "name": "A", "categories": [{"name": "Cafe"}], "date_closed": "2024-05-01"},
            {"fsq_place_id": "b", "name": "B", "categories": [{"name": "Cafe"}], "date_closed": ""},
        ]}),
    ]
    fsq._get = fake_get
    try:
        assert "date_closed" in fsq._TIER_FIELDS["base"]
        res = await fsq.fetch_places("k", "v", 10.0, 106.0, 300, "en", want_ratings=False)
    finally:
        fsq._get = real_get
        disabled = fsq._use_date_closed is False
        fsq._use_date_closed = True
    assert "date_closed" in sent[0]["fields"] and "date_closed" not in sent[1]["fields"], sent
    assert disabled and len(res.places) == 2
    sent = []
    answers = [httpx.Response(200, json={"results": [
        {"fsq_place_id": "a", "name": "A", "categories": [{"name": "Cafe"}], "date_closed": "2024-05-01"},
        {"fsq_place_id": "b", "name": "B", "categories": [{"name": "Cafe"}], "date_closed": ""},
    ]})]
    fsq._get = fake_get
    try:
        res = await fsq.fetch_places("k", "v", 10.0, 106.0, 300, "en", want_ratings=False)
    finally:
        fsq._get = real_get
    by_id = {p["place_id"]: p for p in res.places}
    assert by_id["a"]["closed_at"] == "2024-05-01" and by_id["b"]["closed_at"] is None, by_id

    # 11. «битые пины»: разные заведения в одной точке с адресами на разных улицах (случай:
    # три места из разных районов Хошимина, все в 431 м от точки в Кон Туме)
    assert ps._street_key("85 (Phạm Văn Chiêu), Hồ Chí Minh") == "phạm văn chiêu"
    assert ps._street_key("90 (Huỳnh Mẫn Đạt Q5), Hồ Chí Minh") == "huỳnh mẫn đạt"
    assert ps._street_key("148i (Lý Chính Thắng), Hồ Chí Minh") == "lý chính thắng"
    assert ps._street_key(None) is None and ps._street_key("12") is None

    def pin(i: int, street: str | None) -> dict:
        return {**_pl(i, 0.004, "fsq"), "vicinity": street}

    junk = [
        pin(1, "85 (Phạm Văn Chiêu), Hồ Chí Minh"),
        pin(2, "148i (Lý Chính Thắng), Hồ Chí Minh"),
        pin(3, "90 (Huỳnh Mẫn Đạt Q5), Hồ Chí Minh"),
    ]
    assert ps.drop_suspect_pins(junk) == []
    court = [pin(i, f"{i} Nguyen Hue, Ho Chi Minh") for i in range(1, 4)]  # фудкорт: одна улица
    assert ps.drop_suspect_pins(court) == court
    assert ps.drop_suspect_pins([pin(1, None), pin(2, "5 Le Loi")]) != []  # адреса нет — не трогаем
    mixed = junk + [_pl(9, 0.001, "fsq")]
    assert [p["place_id"] for p in ps.drop_suspect_pins(mixed)] == ["fsq9"]

    await redis.flushall()
    c = Calls()
    install(c, gp_places=gp5[:2], op_places=[], fsq_places=junk)
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)
    # мусор FSQ не «выигрывает» по числу мест у двух настоящих мест Geoapify
    assert r.provider == "geoapify" and len(r.places) == 2, (r.provider, r.places)

    # 12. безымянные места: берутся с типом, ранжируются после именованных, подписываются
    # на языке пользователя, не склеиваются между собой, ссылка на карту — по координатам
    nf = gp._normalize({"properties": {"lat": 1.0, "lon": 2.0, "categories": ["catering.cafe"]}})
    assert nf and nf["unnamed"] and nf["name"] == "" and nf["primary_type"] == "Café"
    assert gp._normalize({"properties": {"lat": 1.0, "lon": 2.0, "categories": ["catering"]}}) is None

    def anon(i: int, dlat: float, ptype: str) -> dict:
        return {**_pl(i, dlat, "osm"), "name": "", "unnamed": True, "primary_type": ptype}

    mixed_places = [anon(1, 0.0001, "Café"), anon(2, 0.0002, "Bar"), _pl(3, 0.0009, "osm")]
    assert len(ps._deduplicate(mixed_places)) == 3  # безымянные не склеиваются
    res = ps.select_places(mixed_places, LAT, LON, 500, 0)
    assert res.places[0]["place_id"] == "osm3", res.places  # именованное — первым
    labeled = ps._label_unnamed(res.places, "ru")
    assert [p["name"] for p in labeled[1:]] == ["Кафе без названия", "Бар без названия"], labeled
    assert ps._label_unnamed(res.places, "en")[1]["name"] == "Unnamed café"
    assert "query=" in links.map_url({**labeled[1], "name": "Кафе без названия"}) and \
        "/@" not in links.map_url(labeled[1])
    # безымянных много, именованных мало → провайдер не считается «достаточным»
    await redis.flushall()
    c = Calls()
    install(c, gp_places=[anon(i, 0.0001 * i, "Café") for i in range(1, 6)] + [_pl(9, 0.0005, "osm")],
            op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 500, 0, "ru", redis, with_ratings=False)
    assert r.provider == "fsq" and len(c.fsq) == 1, (r.provider, c.fsq)
    await redis.flushall()
    c = Calls()
    install(c, gp_places=[anon(1, 0.0001, "Café"), _pl(9, 0.0005, "osm")], op_places=[], fsq_places=[])
    r = await ps.search_places(LAT, LON, 500, 0, "ru", redis, with_ratings=False)
    assert r.provider == "geoapify" and [p["name"] for p in r.places] == ["OSM 9", "Кафе без названия"], (
        r.provider,
        r.places,
    )

    # 13. кластер «одной точки» устойчив к дрожанию координат (≈2 м), но не склеивает соседей в 10 м
    jitter = [
        {**pin(1, "85 (Phạm Văn Chiêu), Hồ Chí Minh"), "lat": LAT + 0.004},
        {**pin(2, "148i (Lý Chính Thắng), Hồ Chí Minh"), "lat": LAT + 0.00402},
        {**pin(3, "90 (Huỳnh Mẫn Đạt Q5), Hồ Chí Minh"), "lat": LAT + 0.00401},
    ]
    assert ps.drop_suspect_pins(jitter) == []
    apart = [
        {**pin(1, "85 (Phạm Văn Chiêu), Hồ Chí Minh"), "lat": LAT + 0.004},
        {**pin(2, "148i (Lý Chính Thắng), Hồ Chí Minh"), "lat": LAT + 0.00410},  # ≈11 м
    ]
    assert len(ps.drop_suspect_pins(apart)) == 2

    # 14. расстояние провайдера не сходится с координатами → место отбрасывается
    far_hcm = {**_pl(1, 0.0, "fsq"), "lat": 10.8, "lon": 106.7, "fsq_distance": 431}  # координаты за сотни км
    ok = {**_pl(2, 0.002, "fsq"), "fsq_distance": 222}  # 0.002° ≈ 222 м
    no_info = _pl(3, 0.001, "fsq")
    kept = ps.drop_distance_mismatch([{**far_hcm, "lat": 10.8}, ok, no_info], LAT, LON)
    assert {p["place_id"] for p in kept} == {"fsq2", "fsq3"}, kept
    await redis.flushall()
    c = Calls()
    install(
        c,
        gp_places=gp5[:2],
        op_places=[],
        fsq_places=[{**far_hcm, "place_id": f"far{i}", "name": f"Far {i}"} for i in range(3)],
    )
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)
    assert r.provider == "geoapify" and len(r.places) == 2, (r.provider, r.places)

    # 15. место без координат не проходит радиус; ключ кэша региона — хеш, без координат
    assert ps._within_radius({"name": "X"}, LAT, LON, 500) is False
    assert ps._within_radius({"name": "X", "lat": LAT, "lon": LON}, LAT, LON, 500) is True
    rkey = ps._region_key(10.7769, 106.7009)
    assert rkey.startswith("region:v1:") and "10.77" not in rkey and "106.70" not in rkey, rkey

    # 16. регион адреса не из региона точки → foreign_region; сверка без диакритики/city
    pin_text = "Ho Chi Minh City | Vietnam | Ho Chi Minh City, Vietnam"
    ph = {**_pl(1, 0.0, "fsq"), "vicinity": "85 (Phạm Văn Chiêu), Hồ Chí Minh"}
    kt = {**_pl(2, 0.0, "fsq"), "vicinity": "12 Nguyen Hue, Kon Tum Province"}
    noreg = {**_pl(3, 0.0, "fsq"), "vicinity": "5 Le Loi"}
    parens = {**_pl(4, 0.0, "fsq"), "vicinity": "36 (35, Linh Dong, Thu Duc), Ho Chi Minh"}
    flagged = ps.flag_foreign_region([ph, kt, noreg, parens], pin_text)
    assert [bool(p.get("foreign_region")) for p in flagged] == [False, True, False, False], flagged
    assert ps.flag_foreign_region([kt], None) == [kt]  # нет данных о регионе — не трогаем

    # 17. foreign_region идёт строго после остальных, но добирает выдачу (не удаляется)
    top_foreign = {**_pl(1, 0.0005, "fsq", 4.9), "foreign_region": True}
    low_local = _pl(2, 0.0010, "fsq", 4.0)
    sel = ps.select_places([top_foreign, low_local], LAT, LON, 500, 0.0)
    assert [p["place_id"] for p in sel.places] == ["fsq2", "fsq1"], sel.places

    # 18. поиск: место из чужого региона уходит в конец (не удаляется)
    await redis.flushall()

    async def fake_reverse_hcm(lat, lon, api_key, **_kw):
        return pin_text

    c = Calls()
    install(c, gp_places=[], op_places=[], fsq_places=[
        {**top_foreign, "foreign_region": None, "vicinity": "1 Foo, Kon Tum Province"},
        {**low_local, "vicinity": "2 Bar, Hồ Chí Minh"},
        _pl(4, 0.0015, "fsq", 4.2),
    ])
    ps.reverse_region = fake_reverse_hcm
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=True)
    ids = [p["place_id"] for p in r.places]
    assert r.provider == "fsq" and ids[-1] == "fsq1" and len(ids) == 3, (r.provider, ids)
    assert await redis.get(ps._region_key(LAT, LON)) == pin_text

    # 19. date_closed у FSQ: закрытое место убирается ДО подсчёта провайдера → выбор уходит
    # к провайдеру, у которого хватает настоящих мест (а не «засчитывается» мёртвое место)
    await redis.flushall()

    async def fake_reverse_none(lat, lon, api_key, **_kw):
        return None

    ps.reverse_region = fake_reverse_none
    closed_one = {**_pl(3, 0.0009, "fsq", 4.4), "closed_at": "2024-05-01"}
    c = Calls()
    install(c, gp_places=gp5, op_places=[], fsq_places=[
        _pl(1, 0.0003, "fsq", 4.1), _pl(2, 0.0006, "fsq", 4.2), closed_one,
    ])
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=True)
    assert r.provider == "geoapify" and len(c.fsq) == 1, (r.provider, len(c.fsq))
    assert all(not p.get("closed_at") for p in r.places)

    # 20. жалобы «🚫 Закрыто»: одна на человека; порог 2 разных людей; админ (вес=порог) сразу
    from bot.services import community
    three = [_pl(i, 0.0003 * i, "fsq", 4.0 + i / 10) for i in range(1, 4)]
    res = await community.report_closed(redis, 1, "fsq1")
    assert (res.total, res.hidden, res.first) == (1, False, True), res
    res = await community.report_closed(redis, 1, "fsq1")
    assert (res.total, res.hidden, res.first) == (1, False, False), res
    assert await community.closed_ids(redis, ["fsq1", "fsq2", None]) == set()
    c = Calls()
    install(c, gp_places=gp5, op_places=[], fsq_places=three)
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=True)
    assert r.provider == "fsq" and len(r.places) == 3  # одной жалобы мало
    res = await community.report_closed(redis, 2, "fsq1")
    assert (res.total, res.hidden) == (2, True), res
    assert await redis.ttl("place:closed:fsq1") > 0
    assert await community.closed_ids(redis, ["fsq1", "fsq2"]) == {"fsq1"}
    c = Calls()
    install(c, gp_places=gp5, op_places=[], fsq_places=three)
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=True)
    assert r.provider == "geoapify", r.provider  # у FSQ осталось 2 именованных места
    adm = await community.report_closed(redis, 99, "fsq2", weight=settings.CLOSED_REPORTS_THRESHOLD)
    assert adm.hidden, adm

    # 21. кнопка «🚫 Закрыто» в карточке и в компактной выдаче (callback_data ≤ 64 байт)
    from bot.keyboards.inline_keyboards import get_compact_keyboard
    from bot.services.cards import card_keyboard
    kb = card_keyboard("ru", three[0], "https://t.me/bot?start=ref_1")
    cl = [b for row in kb.inline_keyboard for b in row if (b.callback_data or "").startswith("cl:")]
    assert len(cl) == 1 and cl[0].callback_data == "cl:fsq1" and "Закрыто" in cl[0].text, cl
    ck = get_compact_keyboard(lambda k: k, three, ["u"] * 3, ["s"] * 3)
    assert [b.callback_data for b in ck.inline_keyboard[0]][-1] == "cl:fsq1"

    # 22. place:meta только для OSM/Geoapify: FSQ-данные (имя, координаты, адрес) не сохраняем
    await redis.flushall()
    await community.remember_places(redis, [
        {"place_id": "osm:node/1", "name": "A", "lat": 1.0, "lon": 2.0},
        {"place_id": "gp:abc", "name": "B", "lat": 1.0, "lon": 2.0},
        {"place_id": "4b5a1d2ef964a5207bb528e3", "name": "FSQ", "lat": 1.0, "lon": 2.0},
        {"place_id": "poi.123", "name": "Mapbox", "lat": 1.0, "lon": 2.0},
    ])
    metas = sorted([k async for k in redis.scan_iter("place:meta:*")])
    assert metas == ["place:meta:gp:abc", "place:meta:osm:node/1"], metas
    # чистка старых записей: без apply только считает
    await redis.hset("place:meta:4b5a1d2ef964a5207bb528e3", mapping={"name": "FSQ"})
    assert await community.purge_foreign_meta(redis) == (3, 1)
    assert await redis.exists("place:meta:4b5a1d2ef964a5207bb528e3") == 1
    assert await community.purge_foreign_meta(redis, apply=True) == (3, 1)
    assert await redis.exists("place:meta:4b5a1d2ef964a5207bb528e3") == 0
    assert await community.purge_foreign_meta(redis) == (2, 0)

    # 23. авто-расширение радиуса в памяти: 100 → 200 → 300 → 500, один запрос к провайдеру
    from bot.utils.geospatial import fmt_distance

    await redis.flushall()
    far3 = [_pl(1, 0.00117, "osm"), _pl(2, 0.00234, "osm"), _pl(3, 0.00405, "osm")]  # ≈130/260/450 м
    c = Calls()
    install(c, gp_places=far3, op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 100, 0, "en", redis, with_ratings=False)
    assert (r.radius_asked, r.radius_used, len(r.places), r.by_accuracy) == (100, 500, 3, False), r
    assert len(c.gp) == 1 and c.gp[0]["radius"] == 600
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)  # то же «ведро»: кэш
    assert (r.radius_used, r.from_cache, len(c.gp)) == (500, True, 1), r
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)  # мест хватает сразу
    assert (r.radius_used, len(r.places)) == (500, 3)

    # 24. дальше полноты данных не расширяем: у ведра 600 м полнота ≈520 м, до 1000 м нет
    await redis.flushall()
    two = far3[:2] + [_pl(9, 0.0063, "osm")]  # третье место ≈700 м
    c = Calls()
    install(c, gp_places=two, op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)
    assert (r.radius_used, len(r.places), len(c.gp)) == (500, 2, 1), r

    # 25. FSQ: живой запрос всегда на 1000 м; расширение идёт по уже полученному ответу
    await redis.flushall()
    fsq_far = [_pl(1, 0.0023, "fsq", 4.5), _pl(2, 0.0034, "fsq", 4.0), _pl(3, 0.0068, "fsq", 4.2)]  # ≈255/380/755 м
    c = Calls()
    install(c, gp_places=[], op_places=[], fsq_places=fsq_far)
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=True)
    assert len(c.fsq) == 1 and c.fsq[0]["radius"] == 1000, c.fsq
    assert (r.provider, r.radius_used, len(r.places)) == ("fsq", 1000, 3), r

    # 26. точность геопозиции больше радиуса: старт радиуса поднимается до ближайшего шага ≥ точности
    await redis.flushall()
    near3 = [_pl(1, 0.0007, "osm"), _pl(2, 0.0013, "osm"), _pl(3, 0.0016, "osm")]  # ≈78/145/178 м
    c = Calls()
    install(c, gp_places=near3, op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 100, 0, "en", redis, with_ratings=False, accuracy_m=150)
    assert (r.radius_asked, r.radius_used, r.by_accuracy, len(r.places)) == (100, 200, True, 3), r
    r = await ps.search_places(LAT, LON, 100, 0, "en", redis, with_ratings=False, accuracy_m=40)
    assert (r.radius_used, r.by_accuracy) == (200, False), r  # расширились из-за нехватки мест, не точности

    # 26b. точность хуже полноты данных (OSM-ведро 600 → до 500 м): старт — самый большой допустимый шаг
    await redis.flushall()
    c = Calls()
    install(c, gp_places=near3, op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 100, 0, "en", redis, with_ratings=False, accuracy_m=800)
    assert (r.radius_used, r.by_accuracy) == (500, True), r

    # 27. штраф расстояния абсолютный: рейтинг 4.0 в 50 м выше 4.5 в 400 м при любом радиусе
    close4, far45 = _pl(1, 0.00045, "fsq", 4.0), _pl(2, 0.0036, "fsq", 4.5)
    for rad in (500, 1000):
        sel = ps.select_places([far45, close4], LAT, LON, rad, 0.0)
        assert [p["place_id"] for p in sel.places] == ["fsq1", "fsq2"], (rad, sel.places)

    # 28. расстояние для показа — с шагом 10 м (минимум 10)
    assert (fmt_distance(3), fmt_distance(414), fmt_distance(416), fmt_distance(1000)) == (10, 410, 420, 1000)

    # 29. аварийный провайдер (Mapbox, когда пусто у всех) тоже проходит чистку:
    # жалобы «закрыто» и «битые пины» (разные улицы в одной точке)
    await redis.flushall()
    await community.report_closed(redis, 1, "mapbox2")
    await community.report_closed(redis, 2, "mapbox2")
    pin = {"lat": LAT + 0.0002, "lon": LON}

    async def fake_mapbox(**_kw):
        return [
            {**_pl(1, 0.0003, "mapbox"), "vicinity": "1 A St, X"},
            {**_pl(2, 0.0004, "mapbox"), "vicinity": "2 B St, X"},  # жалобы → скрыто
            {**_pl(3, 0.0, "mapbox"), **pin, "vicinity": "3 C St, X"},
            {**_pl(4, 0.0, "mapbox"), **pin, "vicinity": "4 D St, X"},  # тот же пин, другая улица
        ]

    c = Calls()
    install(c, gp_places=[], op_places=[], fsq_places=[])
    old_token, old_fn = settings.MAPBOX_TOKEN, ps.find_places_mapbox
    settings.MAPBOX_TOKEN, ps.find_places_mapbox = "t", fake_mapbox
    try:
        r = await ps.search_places(LAT, LON, 500, 0, "en", redis, with_ratings=False)
    finally:
        settings.MAPBOX_TOKEN, ps.find_places_mapbox = old_token, old_fn
    assert r.provider == "mapbox" and [p["place_id"] for p in r.places] == ["mapbox1"], (r.provider, r.places)

    # 30. кэш геокодера учитывает окрестность пользователя (~11 км) и limit
    await redis.flushall()
    calls_geo: list[dict] = []

    def geo_handler(request: httpx.Request) -> httpx.Response:
        calls_geo.append(dict(request.url.params))
        return httpx.Response(200, json=[{"lat": "10.77", "lon": "106.70", "display_name": "Nguyen Hue"}])

    geo_client = httpx.AsyncClient(transport=httpx.MockTransport(geo_handler))

    def fake_geo_client():
        return geo_client

    real_geo_client, geocoder.get_client = geocoder.get_client, fake_geo_client
    try:
        await geocoder.geocode("Nguyen Hue", "en", redis, near=(10.77, 106.70), limit=3)
        await geocoder.geocode("Nguyen Hue", "en", redis, near=(21.03, 105.85), limit=3)  # другой город
        await geocoder.geocode("Nguyen Hue", "en", redis, near=(10.77, 106.70), limit=1)  # другой limit
        await geocoder.geocode("Nguyen Hue", "en", redis, near=(10.771, 106.701), limit=3)  # та же ячейка
    finally:
        geocoder.get_client = real_geo_client
        await geo_client.aclose()
    assert len(calls_geo) == 3, calls_geo

    # 31. поиск блюда: гейт по названию/категории без диакритики; радиус расширяется по блюду
    await redis.flushall()

    def named(i, dlat, name, ptype="restaurant"):
        return {**_pl(i, dlat, "osm"), "name": name, "primary_type": ptype}

    pool = [
        named(1, 0.0009, "Phở Hòa"),  # ≈100 м, диакритика
        named(2, 0.0007, "Phong Lan Cafe", "cafe"),  # «pho» не целым словом
        named(3, 0.0020, "Quán PHO 24"),  # ≈220 м
        named(4, 0.0030, "Bún Bò"),  # другое блюдо
        named(5, 0.0036, "Nhà hàng X", "Pho Restaurant"),  # по категории, ≈400 м
        {**_pl(6, 0.0001, "osm"), "name": "", "unnamed": True},  # безымянное не подходит
    ]
    c = Calls()
    install(c, gp_places=pool, op_places=[], fsq_places=fsq5)
    r = await ps.search_places(LAT, LON, 100, 0, "en", redis, with_ratings=False, dish_tokens=("pho",))
    assert [p["place_id"] for p in r.places] == ["osm1", "osm3", "osm5"], r.places
    assert (r.radius_asked, r.radius_used, len(c.gp)) == (100, 500, 1), r
    r = await ps.search_places(
        LAT, LON, 500, 0, "en", redis, with_ratings=False, dish_tokens=("bun bo", "bún bò")
    )
    assert [p["place_id"] for p in r.places] == ["osm4"], r.places
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False, dish_tokens=("com tam",))
    assert r.places == [] and r.radius_used == 500, r  # таких нет: дошли до предела данных, пусто
    r = await ps.search_places(LAT, LON, 200, 0, "en", redis, with_ratings=False)  # без блюда — как раньше
    assert [p["place_id"] for p in r.places] == ["osm2", "osm1", "osm3", "osm6"], r.places
    assert r.radius_used == 300  # три именованных нашлись на 300 м; безымянное — после них

    # 32. diag dish: совпадения по радиусам и написаниям на наборе провайдера
    from bot import diag

    async def fake_load(*_a, **_kw):
        return ps.LoadInfo(pool, False, True, True, False, "geoapify")

    real_load, ps._load_raw = ps._load_raw, fake_load
    try:
        lines = await diag.dish_probe(redis, "pho", LAT, LON)
    finally:
        ps._load_raw = real_load
    joined = "\n".join(lines)
    assert "провайдер=geoapify мест в наборе=6" in joined and "≤  500 м: 3 совпад." in joined, joined
    assert "написание 'pho': 3 из 6" in joined and "ОБРЕЗАН" not in joined, joined
    assert (await diag.dish_probe(redis, "pho"))[0].startswith("Нужно")

    print("providers ok")


if __name__ == "__main__":
    asyncio.run(main())
