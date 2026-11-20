"""Местные специалитеты: «что попробовать здесь».

Статический словарь bot/data/specialties.json (составляется и вычитывается вручную). Ни внешних
запросов, ни квоты: карточка бесплатная. Город определяется по таблице «центр + радиус»
из того же файла. Непроверенные блюда (verified=false) скрыты, пока не включён
SPECIALTIES_SHOW_UNVERIFIED — словарь, написанный без вычитки, пользователям не показываем.

Схема (schema_version "1.0"): cities — объект «id города → город»; у города name_ru, name_en,
country_code, center{lat,lon}, radius_km, specialties[]; у блюда id, title_ru, title_local,
title_ascii, short_desc, how_to_eat, search_tokens[], cuisine_tags[], verified. Необязательные
поля: title_en, short_desc_en, how_to_eat_en (для en/zh) и sources[] (ссылки; обязательны
для verified=true).
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

SCHEMA_VERSION = "1.0"
MAX_DISHES = 8  # в одной карточке (сообщение ≤ 4096 символов)
DESC_LIMIT = 220
TIP_LIMIT = 160
_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

_CITY_KEYS = {"name_ru", "name_en", "country_code", "center", "radius_km", "specialties"}
_DISH_REQUIRED = {
    "id", "title_ru", "title_local", "title_ascii", "short_desc", "how_to_eat",
    "search_tokens", "cuisine_tags", "verified",
}  # fmt: skip
_DISH_OPTIONAL = {"title_en", "short_desc_en", "how_to_eat_en", "sources"}


def _is_text(v: Any, lo: int = 1, hi: int = 300) -> bool:
    return isinstance(v, str) and lo <= len(v.strip()) <= hi


def validate(data: Any) -> list[str]:
    """Проверка структуры словаря. Возвращает список ошибок (пусто — файл корректен)."""
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return [f"корень: нужен объект со schema_version={SCHEMA_VERSION!r}"]
    cities = data.get("cities")
    if not isinstance(cities, dict) or not cities:
        return ["cities: нужен непустой объект «id города → город»"]
    errs: list[str] = []
    extra = set(data) - {"schema_version", "cities"}
    if extra:
        errs.append(f"корень: лишние ключи {sorted(extra)}")
    for cid, c in cities.items():
        errs += _validate_city(cid, c)
    return errs


def _validate_city(cid: Any, c: Any) -> list[str]:
    tag = f"город {cid!r}"
    if not isinstance(c, dict):
        return [f"{tag}: ожидался объект"]
    errs: list[str] = []
    if not (isinstance(cid, str) and _SLUG.match(cid)):
        errs.append(f"{tag}: id — латиница/цифры/подчёркивание")
    if set(c) - _CITY_KEYS:
        errs.append(f"{tag}: лишние ключи {sorted(set(c) - _CITY_KEYS)}")
    for k in ("name_ru", "name_en"):
        if not _is_text(c.get(k), 1, 80):
            errs.append(f"{tag}: {k} обязателен")
    if not (isinstance(c.get("country_code"), str) and re.fullmatch(r"[A-Z]{2}", c["country_code"])):
        errs.append(f"{tag}: country_code — 2 заглавные буквы")
    center = c.get("center")
    if not (
        isinstance(center, dict)
        and isinstance(center.get("lat"), (int, float))
        and isinstance(center.get("lon"), (int, float))
        and -90 <= center["lat"] <= 90
        and -180 <= center["lon"] <= 180
    ):
        errs.append(f"{tag}: center — {{lat, lon}} в допустимых пределах")
    rad = c.get("radius_km")
    if not (isinstance(rad, (int, float)) and 2 <= rad <= 150):
        errs.append(f"{tag}: radius_km должен быть 2..150")
    dishes = c.get("specialties")
    if not isinstance(dishes, list) or not 1 <= len(dishes) <= 12:
        errs.append(f"{tag}: specialties — от 1 до 12 блюд")
        return errs
    seen: set[str] = set()
    for d in dishes:
        errs += _validate_dish(cid, d, seen)
    return errs


def _validate_dish(cid: Any, d: Any, seen: set[str]) -> list[str]:
    if not isinstance(d, dict):
        return [f"{cid}: блюдо — ожидался объект"]
    did = d.get("id")
    tag = f"{cid}/{did}"
    errs: list[str] = []
    if not (isinstance(did, str) and _SLUG.match(did)):
        errs.append(f"{tag}: id — латиница/цифры/подчёркивание")
    elif did in seen:
        errs.append(f"{tag}: дубль id в городе")
    seen.add(str(did))
    missing = _DISH_REQUIRED - set(d)
    if missing:
        errs.append(f"{tag}: нет полей {sorted(missing)}")
    extra = set(d) - _DISH_REQUIRED - _DISH_OPTIONAL
    if extra:
        errs.append(f"{tag}: лишние ключи {sorted(extra)}")
    for k in ("title_ru", "title_local", "title_ascii"):
        if k in d and not _is_text(d[k], 1, 120):
            errs.append(f"{tag}: {k} — строка до 120 символов")
    for k in ("short_desc", "how_to_eat", "short_desc_en", "how_to_eat_en"):
        if k in d and not _is_text(d[k], 10, 300):
            errs.append(f"{tag}: {k} — строка 10..300 символов")
    if "title_en" in d and not _is_text(d["title_en"], 1, 120):
        errs.append(f"{tag}: title_en — строка до 120 символов")
    tok = d.get("search_tokens")
    if "search_tokens" in d and not (
        isinstance(tok, list) and 1 <= len(tok) <= 8 and all(_is_text(t, 2, 60) for t in tok)
    ):
        errs.append(f"{tag}: search_tokens — от 1 до 8 строк (написания для поиска)")
    tags = d.get("cuisine_tags")
    if "cuisine_tags" in d and not (
        isinstance(tags, list) and len(tags) <= 6 and all(_is_text(t, 2, 40) for t in tags)
    ):
        errs.append(f"{tag}: cuisine_tags — до 6 коротких строк")
    if "verified" in d and not isinstance(d["verified"], bool):
        errs.append(f"{tag}: verified — true/false")
    src = d.get("sources")
    if "sources" in d and not (
        isinstance(src, list) and all(isinstance(u, str) and u.startswith("http") for u in src)
    ):
        errs.append(f"{tag}: sources — список ссылок http(s)")
    elif d.get("verified") is True and not src:
        errs.append(f"{tag}: verified=true требует хотя бы одну ссылку в sources")
    return errs


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    """Словарь из файла; при ошибке — пустой (фича просто не показывается)."""
    empty: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "cities": {}}
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("specialties: cannot load %s: %s", DATA_PATH, type(e).__name__)
        return empty
    errs = validate(data)
    if errs:
        logger.error("specialties: invalid dictionary (%s errors), first: %s", len(errs), errs[0])
        return empty
    return data


def detect_city(lat: float, lon: float, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Город, в радиус которого попала точка; при нескольких — с наименьшим расстоянием/радиус.

    Возвращает копию города с добавленным ключом "id"."""
    best, best_id, best_ratio = None, "", 1.0
    for cid, c in (data or load())["cities"].items():
        dist = calculate_distance(lat, lon, c["center"]["lat"], c["center"]["lon"])
        ratio = dist / (c["radius_km"] * 1000)
        if ratio <= best_ratio:
            best, best_id, best_ratio = c, cid, ratio
    return {**best, "id": best_id} if best else None


def visible_dishes(city: dict[str, Any], show_unverified: bool | None = None) -> list[dict[str, Any]]:
    show = settings.SPECIALTIES_SHOW_UNVERIFIED if show_unverified is None else show_unverified
    return [d for d in city["specialties"] if show or d["verified"]][:MAX_DISHES]


def has_dishes(lat: float, lon: float) -> bool:
    city = detect_city(lat, lon)
    return bool(city and visible_dishes(city))


def render(lang: str, city: dict[str, Any], show_unverified: bool | None = None) -> str | None:
    """HTML-текст карточки или None, если показывать нечего.

    ru — русские поля; en/zh — поля *_en, а если их нет, то русские (пока нет переводов)."""
    dishes = visible_dishes(city, show_unverified)
    if not dishes:
        return None
    ru = lang == "ru"
    city_name = city["name_ru"] if ru else city["name_en"]
    blocks = [get_string("try_here_title", lang=lang).format(city=esc(clean(city_name)))]
    for d in dishes:
        local = clean(d["title_local"])
        alt = clean(d["title_ru"] if ru else d.get("title_en") or d["title_ru"])
        title = f"<b>{esc(local)}</b>"
        if alt and alt.casefold() != local.casefold():
            title += f" — {esc(alt)}"
        desc = clean(d["short_desc"] if ru else d.get("short_desc_en") or d["short_desc"])
        tip = clean(d["how_to_eat"] if ru else d.get("how_to_eat_en") or d["how_to_eat"])
        blocks.append(
            f"{title}\n{esc(truncate(desc, DESC_LIMIT))}\n💡 {esc(truncate(tip, TIP_LIMIT))}"
        )
    blocks.append(f"<i>{esc(get_string('try_here_footer', lang=lang))}</i>")
    return "\n\n".join(blocks)


def render_for_point(
    lang: str, lat: float, lon: float, show_unverified: bool | None = None
) -> str | None:
    city = detect_city(lat, lon)
    return render(lang, city, show_unverified) if city else None
