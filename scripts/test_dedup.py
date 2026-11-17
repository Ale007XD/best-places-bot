"""python scripts/test_dedup.py — _deduplicate: нечёткие дубли (стабы вместо bot.*)."""
import math
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def stub(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m
def dist(a, b, c, d):
    x = math.sin(math.radians(c - a) / 2) ** 2 + math.cos(math.radians(a)) * math.cos(math.radians(c)) * math.sin(math.radians(d - b) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(x))
for n in ("redis", "redis.exceptions"): stub(n, RedisError=Exception)
stub("bot", __path__=[str(ROOT / "bot")]); stub("bot.config", settings=None); stub("bot.services"); stub("bot.services.community", attach_votes=None)
stub("bot.utils", __path__=[str(ROOT / "bot" / "utils")]); stub("bot.utils.foursquare_api", FoursquareError=Exception, ratings_available=None, fetch_places=None)
stub("bot.utils.geospatial", calculate_distance=dist)
stub("bot.utils.mapbox_api", find_places_mapbox=None); stub("bot.utils.overpass_api", find_places_overpass=None); stub("bot.utils.vietmap_api", find_places_vietmap=None)
from bot.utils.places_service import _deduplicate as dd

def P(name, lat, lon, pid, src="fsq", rating=None):
    return dict(name=name, lat=lat, lon=lon, place_id=pid, source=src, rating=rating)

# кейс со скрина: разные id одного провайдера, имена с суффиксом, ~100 м друг от друга
a = P("Тосты", 56.01000, 92.85000, "1"); b = P("Тосты, винное бистро", 56.01000, 92.85160, "2")
assert [p["name"] for p in dd([a, b])] == ["Тосты"], dd([a, b])
# рейтинг у второй записи — остаётся она
b2 = P("Тосты, винное бистро", 56.01000, 92.85160, "2", rating=4.5)
assert dd([a, b2])[0]["rating"] == 4.5
# сеть: одинаковое имя, разные id одного провайдера — НЕ склеиваем
n1 = P("Coffee Like", 56.0, 92.8, "1"); n2 = P("Coffee Like", 56.0, 92.8001, "2")
assert len(dd([n1, n2])) == 2
# разные заведения / далеко — не склеиваем
assert len(dd([a, P("Кофе Тост", 56.01, 92.8501, "3")])) == 2
assert len(dd([a, P("Тосты", 56.03, 92.9, "4")])) == 2
print("ok")
