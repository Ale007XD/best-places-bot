"""python scripts/test_dedup.py — _deduplicate: нечёткие дубли (стабы вместо bot.*)."""

import math
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _stub(name: str, **attrs) -> None:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


def _dist(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    h = (
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 12_742_000 * math.asin(math.sqrt(h))


def _install_stubs() -> None:
    _stub("redis")
    _stub("redis.exceptions", RedisError=Exception)
    _stub("bot", __path__=[str(ROOT / "bot")])
    _stub("bot.config", settings=None)
    _stub("bot.services")
    _stub("bot.services.community", attach_votes=None)
    _stub("bot.services.translator", get_string=lambda key, lang="ru": key)
    _stub("bot.utils", __path__=[str(ROOT / "bot" / "utils")])
    _stub(
        "bot.utils.foursquare_api",
        FoursquareError=Exception,
        ratings_available=None,
        fetch_places=None,
    )
    _stub("bot.utils.geospatial", calculate_distance=_dist)
    _stub("bot.utils.mapbox_api", find_places_mapbox=None)
    _stub("bot.utils.overpass_api", find_places_overpass=None)
    _stub("bot.utils.vietmap_api", find_places_vietmap=None)


def _place(name, lat, lon, pid, src="fsq", rating=None) -> dict:
    return {
        "name": name,
        "lat": lat,
        "lon": lon,
        "place_id": pid,
        "source": src,
        "rating": rating,
    }


def main() -> None:
    _install_stubs()
    from bot.utils.places_service import _deduplicate as dd

    # кейс со скрина: разные id одного провайдера, суффикс в имени, ~100 м между точками
    a = _place("Тосты", 56.01000, 92.85000, "1")
    b = _place("Тосты, винное бистро", 56.01000, 92.85160, "2")
    assert [p["name"] for p in dd([a, b])] == ["Тосты"], dd([a, b])
    # рейтинг у второй записи — остаётся она
    b2 = _place("Тосты, винное бистро", 56.01000, 92.85160, "2", rating=4.5)
    assert dd([a, b2])[0]["rating"] == 4.5
    # сеть: одинаковое имя, разные id одного провайдера — НЕ склеиваем
    n1 = _place("Coffee Like", 56.0, 92.8, "1")
    n2 = _place("Coffee Like", 56.0, 92.8001, "2")
    assert len(dd([n1, n2])) == 2
    # разные заведения / далеко — не склеиваем
    assert len(dd([a, _place("Кофе Тост", 56.01, 92.8501, "3")])) == 2
    assert len(dd([a, _place("Тосты", 56.03, 92.9, "4")])) == 2

    # B: пороги. То же имя — дубль ближе 40 м; дальше (до 120 м) — по правилу «улицы»
    def at(name, dlat, pid, vic=None, src="osm"):
        return {**_place(name, 56.0 + dlat, 92.8, pid, src=src), "vicinity": vic}

    base = at("Cafe Mono", 0, "1", src="fsq")
    assert len(dd([base, at("Cafe Mono", 0.00027, "2")])) == 1  # ≈30 м
    assert len(dd([base, at("Cafe Mono", 0.00054, "3")])) == 1  # ≈60 м, адресов нет → не противоречат
    a1 = at("Cafe Mono", 0, "1", "1 Nguyen Hue, Ho Chi Minh", src="fsq")
    assert len(dd([a1, at("Cafe Mono", 0.00054, "4", "5 Le Loi, Ho Chi Minh")])) == 2  # разные улицы
    assert len(dd([a1, at("Cafe Mono", 0.00054, "5", "9 Nguyen Hue, Ho Chi Minh")])) == 1  # та же улица
    assert len(dd([a1, at("Cafe Mono", 0.00054, "6")])) == 1  # у одного места адреса нет
    # разные улицы, но совсем рядом (≤40 м) — всё равно один объект (пины неточные)
    assert len(dd([a1, at("Cafe Mono", 0.00027, "7", "5 Le Loi, Ho Chi Minh")])) == 1
    # нечёткое имя: ≤45 м — дубль; дальше — только при той же улице
    t1 = at("Тосты", 0, "1", "10 Ленина, Красноярск", src="fsq")
    assert len(dd([t1, at("Тосты, винное бистро", 0.0004, "2", "5 Мира, Красноярск")])) == 1  # ≈44 м
    assert len(dd([t1, at("Тосты, винное бистро", 0.0009, "3", "5 Мира, Красноярск")])) == 2  # ≈100 м, улицы разные
    assert len(dd([t1, at("Тосты, винное бистро", 0.0009, "4", "12 Ленина, Красноярск")])) == 1  # та же улица
    # короткое ядро имени (≤3 символов) на дальней дистанции не склеиваем
    assert len(dd([at("Lu", 0, "1"), at("Lu Bar", 0.0009, "2")])) == 2
    assert len(dd([at("Lu", 0, "1"), at("Lu Bar", 0.00027, "2")])) == 1  # ≈30 м
    # вьетнамское «quán» — слово-генерик: «Quán Cơm Niêu» = «Cơm Niêu»
    assert len(dd([at("Quán Cơm Niêu", 0, "1"), at("Cơm Niêu", 0.00036, "2")])) == 1
    print("ok")


if __name__ == "__main__":
    main()
