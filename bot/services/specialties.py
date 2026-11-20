"""Местные специалитеты: «что попробовать здесь».

Статический словарь bot/data/specialties.json (составляется и вычитывается вручную). Ни внешних
запросов, ни квоты: карточка бесплатная. Город определяется по таблице «центр + радиус»
из того же файла. Непроверенные блюда (verified=false) скрыты, пока не включён
SPECIALTIES_SHOW_UNVERIFIED — словарь, написанный без вычитки, пользователям не показываем.
"""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from bot.config import settings
from bot.services.translator import get_string
from bot.utils.geospatial import calculate_distance
from bot.utils.textfmt import clean, esc, truncate

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "specialties.json"

KINDS = ("street", "restaurant", "cafe", "dessert", "drink")
MAX_DISHES = 8  # в одной карточке (сообщение ≤ 4096 символов)
DESC_LIMIT = 220
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_CITY_KEYS = {"id", "country", "lat", "lon", "radius_km", "name", "dishes"}
_DISH_KEYS = {"id", "verified", "kind", "name", "desc", "aliases", "sources"}


def validate(data: Any) -> list[str]:
    """Проверка структуры словаря. Возвращает список ошибок (пусто — файл корректен)."""
    errs: list[str] = []
    if not isinstance(data, dict) or data.get("version") != 1:
        return ["корень: нужен объект с version=1"]
    cities = data.get("cities")
    if not isinstance(cities, list) or not cities:
        return ["cities: нужен непустой список"]
    extra = set(data) - {"version", "cities"}
    if extra:
        errs.append(f"корень: лишние ключи {sorted(extra)}")

    seen_cities: set[str] = set()
    for c in cities:
        cid = c.get("id") if isinstance(c, dict) else None
        tag = f"город {cid!r}"
        if not isinstance(c, dict):
            errs.append("город: ожидался объект")
            continue
        if not isinstance(cid, str) or not _SLUG.match(cid):
            errs.append(f"{tag}: id — латиница/цифры/дефис")
        elif cid in seen_cities:
            errs.append(f"{tag}: дубль id")
        seen_cities.add(str(cid))
        if set(c) - _CITY_KEYS:
            errs.append(f"{tag}: лишние ключи {sorted(set(c) - _CITY_KEYS)}")
        if not (isinstance(c.get("country"), str) and re.fullmatch(r"[A-Z]{2}", c["country"])):
            errs.append(f"{tag}: country — код ISO из 2 заглавных букв")
        lat, lon, rad = c.get("lat"), c.get("lon"), c.get("radius_km")
        if not (isinstance(lat, (int, float)) and -90 <= lat <= 90):
            errs.append(f"{tag}: lat вне диапазона")
        if not (isinstance(lon, (int, float)) and -180 <= lon <= 180):
            errs.append(f"{tag}: lon вне диапазона")
        if not (isinstance(rad, (int, float)) and 2 <= rad <= 150):
            errs.append(f"{tag}: radius_km должен быть 2..150")
        name = c.get("name")
        if not isinstance(name, dict) or not all(
            isinstance(name.get(k), str) and name[k].strip() for k in ("ru", "en")
        ):
            errs.append(f"{tag}: name.ru и name.en обязательны")
        dishes = c.get("dishes")
        if not isinstance(dishes, list) or not 1 <= len(dishes) <= 12:
            errs.append(f"{tag}: dishes — от 1 до 12 блюд")
            continue
        seen: set[str] = set()
        for d in dishes:
            errs += _validate_dish(cid, d, seen)
    return errs


def _validate_dish(cid: Any, d: Any, seen: set[str]) -> list[str]:
    errs: list[str] = []
    did = d.get("id") if isinstance(d, dict) else None
    tag = f"{cid}/{did}"
    if not isinstance(d, dict):
        return [f"{cid}: блюдо — ожидался объект"]
    if not isinstance(did, str) or not _SLUG.match(did):
        errs.append(f"{tag}: id — латиница/цифры/дефис")
    elif did in seen:
        errs.append(f"{tag}: дубль id в городе")
    seen.add(str(did))
    if set(d) - _DISH_KEYS:
        errs.append(f"{tag}: лишние ключи {sorted(set(d) - _DISH_KEYS)}")
    if not isinstance(d.get("verified"), bool):
        errs.append(f"{tag}: verified — true/false")
    if d.get("kind") not in KINDS:
        errs.append(f"{tag}: kind — одно из {list(KINDS)}")
    name = d.get("name")
    if not isinstance(name, dict) or not all(
        isinstance(name.get(k), str) and name[k].strip() for k in ("local", "ru", "en")
    ):
        errs.append(f"{tag}: name.local, name.ru, name.en обязательны")
    desc = d.get("desc")
    if not isinstance(desc, dict) or not all(
        isinstance(desc.get(k), str) and 10 <= len(desc[k].strip()) <= 300 for k in ("ru", "en")
    ):
        errs.append(f"{tag}: desc.ru и desc.en — строки 10..300 символов")
    al = d.get("aliases")
    if not (
        isinstance(al, list)
        and 1 <= len(al) <= 8
        and all(isinstance(a, str) and 2 <= len(a.strip()) <= 60 for a in al)
    ):
        errs.append(f"{tag}: aliases — от 1 до 8 строк (написания для поиска)")
    src = d.get("sources")
    if not (isinstance(src, list) and all(isinstance(u, str) and u.startswith("http") for u in src)):
        errs.append(f"{tag}: sources — список ссылок http(s) (может быть пустым)")
    elif d.get("verified") is True and not src:
        errs.append(f"{tag}: verified=true требует хотя бы одну ссылку в sources")
    return errs


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    """Словарь из файла; при ошибке — пустой (фича просто не показывается)."""
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("specialties: cannot load %s: %s", DATA_PATH, type(e).__name__)
        return {"version": 1, "cities": []}
    errs = validate(data)
    if errs:
        logger.error("specialties: invalid dictionary (%s errors), first: %s", len(errs), errs[0])
        return {"version": 1, "cities": []}
    return data


def detect_city(lat: float, lon: float, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Город, в радиус которого попала точка; при нескольких — с наименьшим расстоянием/радиус."""
    best, best_ratio = None, 1.0
    for c in (data or load())["cities"]:
        ratio = calculate_distance(lat, lon, c["lat"], c["lon"]) / (c["radius_km"] * 1000)
        if ratio <= best_ratio:
            best, best_ratio = c, ratio
    return best


def visible_dishes(city: dict[str, Any], show_unverified: bool | None = None) -> list[dict[str, Any]]:
    show = settings.SPECIALTIES_SHOW_UNVERIFIED if show_unverified is None else show_unverified
    return [d for d in city["dishes"] if show or d["verified"]][:MAX_DISHES]


def has_dishes(lat: float, lon: float) -> bool:
    city = detect_city(lat, lon)
    return bool(city and visible_dishes(city))


def _pick(d: dict[str, str], lang: str) -> str:
    return clean(d.get(lang) or d.get("en") or d.get("ru") or "")


def render(lang: str, city: dict[str, Any], show_unverified: bool | None = None) -> str | None:
    """HTML-текст карточки или None, если показывать нечего."""
    dishes = visible_dishes(city, show_unverified)
    if not dishes:
        return None
    blocks = [get_string("try_here_title", lang=lang).format(city=esc(_pick(city["name"], lang)))]
    for d in dishes:
        local = clean(d["name"]["local"])
        title = f"<b>{esc(local)}</b>"
        alt = _pick(d["name"], lang)
        if alt and alt.casefold() != local.casefold():
            title += f" — {esc(alt)}"
        blocks.append(f"{title}\n{esc(truncate(_pick(d['desc'], lang), DESC_LIMIT))}")
    blocks.append(f"<i>{esc(get_string('try_here_footer', lang=lang))}</i>")
    return "\n\n".join(blocks)


def render_for_point(
    lang: str, lat: float, lon: float, show_unverified: bool | None = None
) -> str | None:
    city = detect_city(lat, lon)
    return render(lang, city, show_unverified) if city else None
