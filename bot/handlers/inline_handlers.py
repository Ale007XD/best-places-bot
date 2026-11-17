# bot/handlers/inline_handlers.py
"""
Inline-режим: `@бот` в любом чате.

  • пустой запрос — места рядом с пользователем (нужна геопозиция inline: BotFather →
    /setinlinelocation → Enable; при первом использовании Telegram спросит разрешение);
  • текст — координаты / ссылка Google Maps / фильтр найденных рядом по названию или
    категории / адрес (геокодер) — как и в обычном диалоге.

Выдача в inline — всегда БАЗОВАЯ (без платных рейтингов и фото Foursquare): запросы идут на
каждый символ, расходовать Premium-кредиты на них нельзя. Количество обращений к API
ограничивают кэш по точке и single-flight; геокодер — свой троттлинг. Если поиск не успевает
за бюджет времени (inline-запросы быстро «протухают»), отдаётся «⏳ Ищу…», а поиск
дорабатывает в фоне и наполняет кэш — повторный запрос отвечает мгновенно.
"""

import asyncio
import hashlib
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from bot.handlers import account_handlers as acc
from bot.services import cards
from bot.services.translator import get_string
from bot.utils import geocoder, links
from bot.utils.geospatial import calculate_distance
from bot.utils.places_service import SearchUnavailable, search_places
from bot.utils.textfmt import clean, truncate

logger = logging.getLogger(__name__)

router = Router()

INLINE_RESULTS = 8
INLINE_RADIUS = 800
INLINE_BUDGET_SEC = 5.5
GEOCODE_THROTTLE_MS = 1500


def _note(lang: str, key: str) -> InlineQueryResultArticle:
    """Служебная «карточка» (нет геопозиции / ищу / ошибка / пусто)."""
    title = get_string(f"inline_{key}_title", lang=lang)
    desc = get_string(f"inline_{key}_desc", lang=lang)
    return InlineQueryResultArticle(
        id=f"note-{key}",
        title=title,
        description=desc,
        input_message_content=InputTextMessageContent(message_text=f"{title}\n{desc}"),
    )


def _result_for(
    place: dict, lang: str, lat: float, lon: float, ref: str, bot_username: str
) -> InlineQueryResultArticle:
    def _(key: str) -> str:
        return get_string(key, lang=lang)
    name = truncate(clean(place.get("name")) or "—", 60)

    meta = []
    rating = place.get("rating")
    if isinstance(rating, int | float):
        icon = "🥷" if place.get("rating_src") == "community" else "⭐️"
        meta.append(f"{icon} {rating:.1f}")
    if place.get("lat") is not None and place.get("lon") is not None:
        d = calculate_distance(lat, lon, float(place["lat"]), float(place["lon"]))
        meta.append(f"🚶 {d} {_('unit_m')}")
    if place.get("primary_type"):
        meta.append(truncate(clean(place["primary_type"]), 28))

    photo = place.get("photo_url")
    buttons = [InlineKeyboardButton(text=_("btn_map"), url=links.map_url(place))]
    if bot_username:
        buttons.append(
            InlineKeyboardButton(text=_("inline_open_in_bot"), url=f"https://t.me/{bot_username}")
        )
    key = str(place.get("place_id") or name)
    return InlineQueryResultArticle(
        id=hashlib.md5(key.encode()).hexdigest(),
        title=name,
        description=" · ".join(meta) or None,
        thumbnail_url=photo,
        input_message_content=InputTextMessageContent(
            message_text=cards.build_caption(lang, None, place, lat, lon),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=photo, show_above_text=True, prefer_large_media=True
            )
            if photo
            else LinkPreviewOptions(is_disabled=True),
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


async def _search_with_budget(lat: float, lon: float, lang: str, redis_conn):
    """Поиск с ограничением по времени; по таймауту работа продолжается и наполняет кэш."""
    task = asyncio.ensure_future(
        search_places(
            lat=lat,
            lon=lon,
            radius=INLINE_RADIUS,
            min_rating=0.0,
            lang_code=lang,
            redis_conn=redis_conn,
            with_ratings=False,
        )
    )
    # Исключение «осиротевшей» задачи нужно забрать, иначе asyncio ругнётся в лог.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return await asyncio.wait_for(asyncio.shield(task), INLINE_BUDGET_SEC)


def _matches(place: dict, q: str) -> bool:
    q = q.casefold()
    return q in clean(place.get("name")).casefold() or q in clean(place.get("primary_type")).casefold()


@router.inline_query()
async def on_inline(
    query: InlineQuery, bot: Bot, lang_code: str, redis_conn, analytics=None
):
    uid = query.from_user.id
    text = (query.query or "").strip()
    loc = query.location

    async def reply(results, *, cache: int = 20, button=None):
        try:
            await query.answer(
                results=results, cache_time=cache, is_personal=True, button=button
            )
        except TelegramAPIError as e:  # «query is too old» и т.п. — отвечать уже поздно
            logger.info("inline answer failed: %s", e)

    if analytics:
        await analytics.track_user(uid)
        await analytics.track_feature_use("inline", "query")

    open_bot = InlineQueryResultsButton(
        text=get_string("inline_open_bot", lang=lang_code), start_parameter="inline"
    )

    # --- точка поиска ---
    lat = lon = None
    from_user_location = False
    if text:
        pt = await geocoder.parse_point(text)
        if pt:
            lat, lon = pt
            text = ""  # координаты/ссылка — это точка, а не фильтр
    if lat is None and loc is not None:
        lat, lon, from_user_location = loc.latitude, loc.longitude, True
    if lat is None and not text:
        await reply([_note(lang_code, "no_location")], cache=5, button=open_bot)
        return

    # --- поиск рядом (по геопозиции пользователя / указанной точке) ---
    result = None
    if lat is not None:
        try:
            result = await _search_with_budget(lat, lon, lang_code, redis_conn)
        except TimeoutError:
            await reply([_note(lang_code, "busy")], cache=1)
            return
        except SearchUnavailable:
            await reply([_note(lang_code, "error")], cache=3)
            return

    places = list(result.places) if result else []

    # --- текст: сначала фильтр найденного рядом, иначе адрес через геокодер ---
    if text:
        places = [p for p in places if _matches(p, text)]
        if not places and len(text) >= 3:
            if not await redis_conn.set(f"throttle:ig:{uid}", 1, nx=True, px=GEOCODE_THROTTLE_MS):
                await reply([_note(lang_code, "busy")], cache=1)
                return
            near = (lat, lon) if lat is not None else None
            cands = await geocoder.geocode(text, lang_code, redis_conn, near=near, limit=1)
            if cands:
                lat, lon = cands[0].lat, cands[0].lon
                from_user_location = False
                try:
                    result = await _search_with_budget(lat, lon, lang_code, redis_conn)
                except TimeoutError:
                    await reply([_note(lang_code, "busy")], cache=1)
                    return
                except SearchUnavailable:
                    await reply([_note(lang_code, "error")], cache=3)
                    return
                places = list(result.places)

    if not places or lat is None:
        await reply([_note(lang_code, "nothing")], cache=10)
        return

    bot_username = ""
    ref = ""
    try:
        bot_username = await acc.bot_username(bot)
        ref = await acc.ref_link(bot, uid)
    except TelegramAPIError:
        logger.warning("bot username unavailable for inline")

    results = [
        _result_for(p, lang_code, lat, lon, ref, bot_username) for p in places[:INLINE_RESULTS]
    ]
    # Без геопозиции пользователя (искали по адресу) кнопку «открыть бота» не навязываем.
    await reply(results, cache=30 if from_user_location else 60)
