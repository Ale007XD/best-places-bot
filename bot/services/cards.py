# bot/services/cards.py
"""
Карточки мест: фото (или заглушка) + подпись + кнопки. Одно сообщение на место.

Чтобы карточки «не плыли»:
  • название — одна строка, обрезается до NAME_MAX; полное название при обрезке уходит
    в сворачиваемую цитату (<blockquote expandable> — «под кат»);
  • адрес/категория/полное название ограничены по длине, весь текст — не больше 1024
    символов (лимит подписи Telegram);
  • внешние строки чистятся (bidi/нулевой ширины/переводы строк) и экранируются;
  • кнопки без названий мест, поэтому длинные имена на них не влияют.
"""

import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.services.translator import get_string
from bot.utils import links
from bot.utils.geospatial import (
    bearing_to_direction,
    calculate_bearing,
    calculate_distance,
)
from bot.utils.textfmt import clean, esc, truncate

logger = logging.getLogger(__name__)

NAME_MAX = 38
CATEGORY_MAX = 28
ADDRESS_MAX = 140
FULLNAME_MAX = 200
CAPTION_LIMIT = 1024

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_CAFE = re.compile(r"caf[eé]|coffee|tea\b|bakery|dessert|juice|ice cream|кофе|чай|кондитер", re.IGNORECASE)
_BAR = re.compile(r"\bbar\b|pub\b|cocktail|brewery|wine|lounge|nightclub|бар\b|паб", re.IGNORECASE)


def placeholder_name(primary_type: str | None) -> str:
    t = primary_type or ""
    if _BAR.search(t):
        return "bar"
    if _CAFE.search(t):
        return "cafe"
    return "restaurant"


def build_caption(
    lang_code: str,
    idx: int | None,
    place: dict[str, Any],
    user_lat: float,
    user_lon: float,
    compact: bool = False,
) -> str:
    """
    Подпись карточки (idx=None — без номера, для inline). ≤ CAPTION_LIMIT, валидный HTML.
    compact=True — карточка целиком в одной сворачиваемой цитате (для режима «одно сообщение»:
    видны имя, рейтинг·расстояние и категория, остальное — «под катом»).
    """
    def _(key: str) -> str:
    return get_string(key, lang=lang_code)

    full_name = clean(place.get("name")) or "—"
    name = truncate(full_name, NAME_MAX)
    lines = [f"<b>{f'{idx}. ' if idx is not None else ''}{esc(name)}</b>"]

    # ⭐/🥷 рейтинг · 🚶 расстояние
    meta = []
    rating = place.get("rating")
    if isinstance(rating, int | float):
        total = int(place.get("user_ratings_total") or 0)
        icon = "🥷" if place.get("rating_src") == "community" else "⭐️"
        meta.append(f"{icon} {rating:.1f}" + (f" ({total})" if total else ""))

    direction = ""
    if place.get("lat") is not None and place.get("lon") is not None:
        plat, plon = float(place["lat"]), float(place["lon"])
        dist = calculate_distance(user_lat, user_lon, plat, plon)
        meta.append(f"🚶 {dist} {_('unit_m')}")
        direction = bearing_to_direction(_, calculate_bearing(user_lat, user_lon, plat, plon))
    if meta:
        lines.append(" · ".join(meta))

    category = truncate(clean(place.get("primary_type")), CATEGORY_MAX)
    if category:
        lines.append(f"🍴 {esc(category)}")

    # «Под кат»: полное название (если обрезали), направление, адрес.
    extra = []
    if full_name != name:
        extra.append(esc(truncate(full_name, FULLNAME_MAX)))
    address = truncate(clean(place.get("vicinity")), ADDRESS_MAX)
    if direction:
        extra.append(f"🧭 {esc(direction)}")
    if address:
        extra.append(f"📍 {esc(address)}")
    if extra:
        lines.append("<blockquote expandable>" + "\n".join(extra) + "</blockquote>")

    if compact:
        # без вложенных цитат: всё тело — одна expandable-цитата
        body = "\n".join(
            [ln for ln in lines if not ln.startswith("<blockquote")] + extra
        )
        return f"<blockquote expandable>{body}</blockquote>"

    caption = "\n".join(lines)
    if len(caption) > CAPTION_LIMIT:  # страховка: режем сворачиваемую часть
        caption = "\n".join(lines[:-1]) if extra else truncate(caption, CAPTION_LIMIT)
    return caption


def card_keyboard(
    lang_code: str,
    place: dict[str, Any],
    ref_link: str,
    up: int = 0,
    down: int = 0,
    mine: str | None = None,
) -> InlineKeyboardMarkup:
    """🗺 Карта · 📤 Поделиться / 👍 · 👎 (со счётчиками; mine — «up»/«down» отмечает ✅)."""
    def _(key: str) -> str:
    return get_string(key, lang=lang_code)
    rows = [
        [
            InlineKeyboardButton(text=_("btn_map"), url=links.map_url(place)),
            InlineKeyboardButton(
                text=_("btn_share"), url=links.share_url(place, ref_link, lang_code)
            ),
        ]
    ]
    pid = place.get("place_id")
    # callback_data ≤ 64 байт; у экзотических id голосование просто не показываем
    if pid and len(f"vu:{pid}".encode()) <= 64:
        rows.append(
            [
                InlineKeyboardButton(
                    text=vote_label("👍", up, mine == "up"), callback_data=f"vu:{pid}"
                ),
                InlineKeyboardButton(
                    text=vote_label("👎", down, mine == "down"), callback_data=f"vd:{pid}"
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


LAYOUTS = ("cards", "compact")
_UI_KEY = "cfg:ui"


async def get_layout(r) -> str:
    """Вид выдачи: настройка админа (Redis) → иначе RESULTS_LAYOUT из окружения."""
    try:
        value = await r.hget(_UI_KEY, "layout")
    except Exception:  # noqa: BLE001 — выдача не должна зависеть от Redis-настроек
        value = None
    value = value or settings.RESULTS_LAYOUT
    return value if value in LAYOUTS else "cards"


async def set_layout(r, layout: str) -> None:
    await r.hset(_UI_KEY, "layout", layout)


def vote_label(icon: str, count: int, chosen: bool = False) -> str:
    text = f"{icon} {count}" if count else icon
    return f"✅ {text}" if chosen else text


async def _placeholder_sources(redis_conn, name: str) -> list[Any]:
    """Сначала file_id (мгновенно), затем файл с диска (первая отправка / file_id устарел)."""
    sources: list[Any] = []
    try:
        file_id = await redis_conn.get(f"asset:ph:{name}")
        if file_id:
            sources.append(file_id)
    except Exception:  # noqa: BLE001 — кэш file_id не должен ломать отправку
        logger.warning("placeholder file_id lookup failed")
    sources.append(FSInputFile(_ASSETS / f"placeholder_{name}.jpg"))
    return sources


async def send_place_card(
    bot: Bot,
    chat_id: int,
    redis_conn,
    lang_code: str,
    idx: int,
    place: dict[str, Any],
    user_lat: float,
    user_lon: float,
    ref_link: str,
) -> Message | None:
    """Отправляет карточку: фото места → заглушка → просто текст. Никогда не роняет выдачу."""
    caption = build_caption(lang_code, idx, place, user_lat, user_lon)
    votes = place.get("votes") or {}
    markup = card_keyboard(lang_code, place, ref_link, votes.get("up", 0), votes.get("down", 0))

    name = placeholder_name(place.get("primary_type"))
    photo_url = place.get("photo_url") if settings.PHOTOS_ENABLED else None
    sources: list[Any] = [photo_url] if photo_url else []
    sources += await _placeholder_sources(redis_conn, name)

    for src in sources:
        for attempt in (1, 2):
            try:
                msg = await bot.send_photo(
                    chat_id, src, caption=caption, parse_mode="HTML", reply_markup=markup
                )
            except TelegramRetryAfter as e:
                if attempt == 1:
                    import asyncio

                    await asyncio.sleep(min(e.retry_after, 5))
                    continue
                break
            except TelegramBadRequest as e:  # битый URL фото, устаревший file_id и т.п.
                logger.warning("send_photo failed (%s): %s", type(src).__name__, e)
                break
            except TelegramAPIError as e:
                logger.error("send_photo API error: %s", e)
                break
            if isinstance(src, FSInputFile) and msg.photo:
                try:  # запоминаем file_id заглушки — следующие отправки без загрузки файла
                    await redis_conn.set(f"asset:ph:{name}", msg.photo[-1].file_id)
                except Exception:  # noqa: BLE001
                    logger.warning("placeholder file_id save failed")
            return msg

    try:
        return await bot.send_message(
            chat_id, caption, parse_mode="HTML", reply_markup=markup,
            disable_web_page_preview=True,
        )
    except TelegramAPIError as e:
        logger.error("card text fallback failed: %s", e)
        return None
