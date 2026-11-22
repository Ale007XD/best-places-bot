# bot/handlers/inline_handlers.py
"""
Inline-режим: `@бот` в любом чате.

  • пустой запрос — 3 ближайших места от пользователя. Точка: геопозиция inline (BotFather →
    /setinlinegeo → Enable; её передают, по имеющимся сведениям, только мобильные клиенты после
    разрешения, Desktop — нет) → иначе последняя геопозиция, присланная боту (user_lastloc,
    TTL LASTLOC_TTL_HOURS) → иначе подсказка;
  • токены в запросе: радиус («1км», «500м», «r2000» — «прилипает» к 200/500/1000/2000/3000;
    без него — радиус из user_prefs, иначе INLINE_DEFAULT_RADIUS) и категория («кафе», «бар»,
    «ресторан», «cafe», «pub»…);
  • остальной текст — координаты / ссылка Google Maps / фильтр найденных рядом по названию /
    адрес (геокодер) — как и в обычном диалоге.

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
from redis.exceptions import RedisError

from bot.config import settings
from bot.handlers import account_handlers as acc
from bot.services import cards
from bot.services.translator import get_string
from bot.utils import geocoder, links
from bot.utils.attribution import attribution
from bot.utils.geospatial import calculate_distance, fmt_distance
from bot.utils.places_service import SearchUnavailable, search_places
from bot.utils.querytools import parse_query, snap_radius
from bot.utils.textfmt import clean, truncate

logger = logging.getLogger(__name__)

router = Router()

INLINE_RADII = (100, 200, 500, 1000)  # допустимые значения радиуса в user_prefs
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
    place: dict,
    lang: str,
    lat: float,
    lon: float,
    ref: str,
    bot_username: str,
    credit: str = "",
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
        meta.append(f"🚶 {fmt_distance(d)} {_('unit_m')}")
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
            message_text=cards.build_caption(lang, None, place, lat, lon)
            + (f"\n\n{credit}" if credit else ""),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=photo, show_above_text=True, prefer_large_media=True
            )
            if photo
            else LinkPreviewOptions(is_disabled=True),
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


async def _default_radius(redis_conn, uid: int) -> int:
    """Радиус из настроек пользователя (user_prefs), иначе INLINE_DEFAULT_RADIUS."""
    try:
        raw = await redis_conn.hget(f"user_prefs:{uid}", "radius")
    except RedisError:
        raw = None
    if raw in {str(r) for r in INLINE_RADII}:
        return int(raw)
    return settings.INLINE_DEFAULT_RADIUS


async def _last_location(redis_conn, uid: int) -> tuple[float, float] | None:
    """Последняя «моя» геопозиция, присланная боту в диалоге (см. user_handlers._apply_point)."""
    try:
        raw = await redis_conn.get(f"user_lastloc:{uid}")
        if raw:
            lat, lon = str(raw).split(",", 1)
            return float(lat), float(lon)
    except (RedisError, ValueError):
        pass
    return None


async def _search_with_budget(
    lat: float, lon: float, lang: str, redis_conn, radius: int, kind: str
):
    """Поиск с ограничением по времени; по таймауту работа продолжается и наполняет кэш."""
    task = asyncio.ensure_future(
        search_places(
            lat=lat,
            lon=lon,
            radius=radius,
            min_rating=0.0,
            lang_code=lang,
            redis_conn=redis_conn,
            with_ratings=False,
            kind=kind,
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
    loc = query.location
    parsed = parse_query((query.query or "").strip())
    text, kind = parsed.rest, parsed.kind
    default_radius = await _default_radius(redis_conn, uid)
    radius = snap_radius(parsed.radius or default_radius, settings.INLINE_MAX_RADIUS)

    # Кнопка над выдачей: текущий радиус + переход в бота за подсказкой (start=inline_help).
    help_button = InlineQueryResultsButton(
        text=get_string("inline_radius_btn", lang=lang_code).format(radius=radius),
        start_parameter="inline_help",
    )

    async def reply(results, *, cache: int = 20, button=help_button):
        try:
            await query.answer(
                results=results, cache_time=cache, is_personal=True, button=button
            )
        except TelegramAPIError as e:  # «query is too old» и т.п. — отвечать уже поздно
            logger.info("inline answer failed: %s", e)

    if analytics:
        await analytics.track_user(uid)
        await analytics.track_feature_use("inline", "query")

    # --- точка поиска: координаты/ссылка в тексте → геопозиция inline → последняя точка ---
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
        last = await _last_location(redis_conn, uid)
        if last is None:
            await reply([_note(lang_code, "no_location")], cache=5)
            return
        (lat, lon), from_user_location = last, True
    elif lat is None and text:
        # Название без геопозиции: ищем рядом с последней точкой (если есть), иначе геокодер.
        last = await _last_location(redis_conn, uid)
        if last is not None:
            (lat, lon), from_user_location = last, True

    # --- поиск рядом ---
    result = None
    if lat is not None:
        try:
            result = await _search_with_budget(lat, lon, lang_code, redis_conn, radius, kind)
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
                    result = await _search_with_budget(
                        lat, lon, lang_code, redis_conn, radius, kind
                    )
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

    # Ближайшие (а не «лучшие»): в inline нет платных рейтингов, важна близость.
    def _dist(p: dict) -> float:
        if p.get("lat") is None or p.get("lon") is None:
            return float("inf")
        return calculate_distance(lat, lon, float(p["lat"]), float(p["lon"]))

    nearest = sorted(places, key=_dist)[: settings.INLINE_RESULTS]
    credit = attribution(result.provider if result else "", nearest)
    results = [_result_for(p, lang_code, lat, lon, ref, bot_username, credit) for p in nearest]
    await reply(results, cache=30 if from_user_location else 60)
