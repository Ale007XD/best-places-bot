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
from bot.utils import links, overpass_api
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

    print("providers ok")


if __name__ == "__main__":
    asyncio.run(main())
