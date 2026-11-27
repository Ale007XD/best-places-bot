"""Местные специалитеты: «что попробовать здесь».

Статический словарь bot/data/specialties.json (составляется и вычитывается вручную). Ни внешних
запросов, ни квоты: карточка бесплатная. Город определяется по таблице «центр + радиус»
из того же файла. Непроверенные блюда (verified=false) скрыты, пока не включён
SPECIALTIES_SHOW_UNVERIFIED — словарь, написанный без вычитки, пользователям не показываем.

Схема (schema_version "1.0"): cities — объект «id города → город»; у города name_ru, name_en,
country_code, center{lat,lon}, radius_km, specialties[]; у блюда id, title_ru, title_local,
title_ascii, short_desc, how_to_eat, search_tokens[], cuisine_tags[], verified. Необязательные
поля: title_en, short_desc_en, how_to_eat_en (для en/zh), sources[] (ссылки; обязательны
для verified=true) и venue_tokens[] — слова, которые встречаются в НАЗВАНИЯХ заведений этого
блюда («pho» для «Phở Hòa»; не «pho bo»). По ним работает «Найти блюдо»; нет поля — используются
search_tokens (полнота ниже, потому что в названии заведения обычно лишь часть названия блюда).
locality_type (необязательно; одно из LOCALITY_TYPES) — связь блюда с городом: city_origin
(родом отсюда), city_specialty (отличает город), local_favorite (любимое местное, но не отсюда),
regional (характерно для региона, не города), generic (общенациональное). По нему карточка
ставит местные блюда выше общих; нет поля — как city_specialty. Поле добавлено без смены
schema_version: старые словари остаются корректными.
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
from bot.utils.textnorm import fold

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "specialties.json"

SCHEMA_VERSION = "1.0"
DISH_PREFIX = "dish:"  # callback_data кнопки поиска блюда: dish:{city_id}:{dish_id}
MAX_DISHES = 8  # в одной карточке (сообщение ≤ 4096 символов)
DESC_LIMIT = 220
TIP_LIMIT = 160
_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

_CITY_KEYS = {"name_ru", "name_en", "country_code", "center", "radius_km", "specialties"}
_DISH_REQUIRED = {
    "id", "title_ru", "title_local", "title_ascii", "short_desc", "how_to_eat",
    "search_tokens", "cuisine_tags", "verified",
}  # fmt: skip
_DISH_OPTIONAL = {"title_en", "short_desc_en", "how_to_eat_en", "sources", "venue_tokens", "locality_type"}
DISH_FIELDS = frozenset(_DISH_REQUIRED | _DISH_OPTIONAL)  # всё, что вправе лежать в словаре у блюда
LOCALITY_TYPES = ("city_origin", "city_specialty", "local_favorite", "regional", "generic")
_LOCALITY_RANK = {name: rank for rank, name in enumerate(LOCALITY_TYPES)}
_LOCALITY_DEFAULT = _LOCALITY_RANK["city_specialty"]  # поля нет — не штрафуем и не поднимаем


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
    vt = d.get("venue_tokens")
    if "venue_tokens" in d and not (
        isinstance(vt, list) and 1 <= len(vt) <= 6 and all(_is_text(t, 2, 40) for t in vt)
    ):
        errs.append(f"{tag}: venue_tokens — от 1 до 6 слов из названий заведений")
    if "locality_type" in d and d["locality_type"] not in LOCALITY_TYPES:
        errs.append(f"{tag}: locality_type — одно из {list(LOCALITY_TYPES)}")
    src = d.get("sources")
    if "sources" in d and not (
        isinstance(src, list) and all(isinstance(u, str) and u.startswith("http") for u in src)
    ):
        errs.append(f"{tag}: sources — список ссылок http(s)")
    elif d.get("verified") is True and not src:
        errs.append(f"{tag}: verified=true требует хотя бы одну ссылку в sources")
    if len(f"{DISH_PREFIX}{cid}:{did}".encode()) > 64:
        errs.append(f"{tag}: id города и блюда слишком длинные для callback_data (≤ 64 байт)")
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
    """Блюда для карточки: вычитанные (или все — SPECIALTIES_SHOW_UNVERIFIED), местные выше общих.

    Порядок по locality_type устойчивый: внутри одного типа остаётся порядок словаря. Срез
    MAX_DISHES идёт после сортировки — при переполнении отбрасываются самые «общие» блюда."""
    show = settings.SPECIALTIES_SHOW_UNVERIFIED if show_unverified is None else show_unverified
    shown = [d for d in city["specialties"] if show or d["verified"]]
    shown.sort(key=lambda d: _LOCALITY_RANK.get(d.get("locality_type", ""), _LOCALITY_DEFAULT))
    return shown[:MAX_DISHES]


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
    blocks.append(get_string("try_here_search_hint", lang=lang))
    blocks.append(f"<i>{esc(get_string('try_here_footer', lang=lang))}</i>")
    return "\n\n".join(blocks)


def render_for_point(
    lang: str, lat: float, lon: float, show_unverified: bool | None = None
) -> str | None:
    city = detect_city(lat, lon)
    return render(lang, city, show_unverified) if city else None


# --- Поиск блюда ---


def dish_ref(city_id: str, dish_id: str) -> str:
    return f"{city_id}:{dish_id}"


def get_dish(ref: str, data: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """«city_id:dish_id» → (город с ключом id, блюдо); None — такого блюда нет."""
    cid, _, did = (ref or "").partition(":")
    city = (data or load())["cities"].get(cid)
    if not city:
        return None
    dish = next((d for d in city["specialties"] if d["id"] == did), None)
    return ({**city, "id": cid}, dish) if dish else None


def dish_needles(dish: dict[str, Any]) -> tuple[str, ...]:
    """Написания блюда для поиска по названию заведения (без диакритики, без повторов)."""
    raw = [*dish["search_tokens"], dish["title_local"], dish["title_ascii"], dish.get("title_en", "")]
    return tuple(dict.fromkeys(f for f in (fold(x) for x in raw) if len(f) >= 2))


def venue_needles(dish: dict[str, Any]) -> tuple[str, ...]:
    """Написания для поиска по названию заведения: venue_tokens, а если их нет — написания блюда."""
    tokens = [f for f in (fold(x) for x in dish.get("venue_tokens", [])) if len(f) >= 2]
    return tuple(dict.fromkeys(tokens)) or dish_needles(dish)


def venue_spellings(dish: dict[str, Any]) -> tuple[str, ...]:
    """Написания блюда с диакритикой — чтобы «Phố» (улица) не засчитывалось как «Phở» (суп).

    Нужны только textnorm.matches: слово названия заведения с чужой диакритикой — другое слово."""
    raw = [
        *dish.get("venue_tokens", []), *dish["search_tokens"],
        dish["title_local"], dish["title_ascii"], dish.get("title_en", ""),
    ]  # fmt: skip
    return tuple(dict.fromkeys(x for x in raw if isinstance(x, str) and x.strip()))


def resolve_dish(
    text: str, lat: float, lon: float, show_unverified: bool | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Текст пользователя — это название блюда города, в котором он находится?

    Только точное совпадение (после fold) с одним из написаний: «pho», «Bún chả», «cơm tấm».
    Обычные слова и адреса сюда не попадают."""
    query = fold(text)
    if not query or len(query) > 60:
        return None
    city = detect_city(lat, lon)
    if not city:
        return None
    for dish in visible_dishes(city, show_unverified):
        if query in dish_needles(dish):
            return city, dish
    return None


def dish_title(dish: dict[str, Any], lang: str) -> str:
    """«Cơm tấm — Ком Там»: заголовок блюда на языке пользователя."""
    local = clean(dish["title_local"])
    alt = clean(dish["title_ru"] if lang == "ru" else dish.get("title_en") or dish["title_ru"])
    return f"{local} — {alt}" if alt and alt.casefold() != local.casefold() else local


def card_for_point(
    lang: str, lat: float, lon: float, show_unverified: bool | None = None
) -> tuple[str, dict[str, Any], list[dict[str, Any]]] | None:
    """(текст карточки, город, показанные блюда) для точки — карточка плюс кнопки поиска блюд."""
    city = detect_city(lat, lon)
    if not city:
        return None
    text = render(lang, city, show_unverified)
    return (text, city, visible_dishes(city, show_unverified)) if text else None
