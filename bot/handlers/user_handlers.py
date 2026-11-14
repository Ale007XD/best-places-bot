# bot/handlers/user_handlers.py
"""
Диалог:
  /start → (язык, только если не удалось определить) → геопозиция →
  ОДИН экран параметров (радиус + мин. рейтинг + «Искать») → одно сообщение с топ-3.

Параметры запоминаются в Redis (user_prefs:{id}) и подставляются при следующем поиске.
Геопозицию можно прислать в любой момент — это сразу открывает экран параметров.
"""

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from redis.exceptions import RedisError

from bot.config import settings
from bot.keyboards import inline_keyboards as kb
from bot.services.translator import SUPPORTED_LANGUAGES, get_string
from bot.utils.geospatial import (
    bearing_to_direction,
    calculate_bearing,
    calculate_distance,
)
from bot.utils.places_service import TOP_N, SearchResult, search_places

logger = logging.getLogger(__name__)

router = Router()

SEARCH_THROTTLE_SEC = 4


class SearchSteps(StatesGroup):
    waiting_for_location = State()


class FeedbackState(StatesGroup):
    waiting_for_feedback = State()


# --- Вспомогательное ---


def _t(lang_code: str):
    return lambda key: get_string(key, lang=lang_code)


def _message_of(callback: CallbackQuery) -> Message | None:
    """callback.message может быть None / недоступным (старое сообщение)."""
    return callback.message if isinstance(callback.message, Message) else None


async def _load_prefs(redis_conn, user_id: int) -> tuple[int, float]:
    radius, min_rating = settings.DEFAULT_RADIUS, settings.DEFAULT_MIN_RATING
    try:
        prefs = await redis_conn.hgetall(f"user_prefs:{user_id}")
        if prefs.get("radius") in {str(r) for r in kb.RADII}:
            radius = int(prefs["radius"])
        if prefs.get("min_rating") in {str(m) for m in kb.MIN_RATINGS}:
            min_rating = float(prefs["min_rating"])
    except RedisError as e:  # prefs — удобство, не критичный путь
        logger.warning("prefs load failed: %s", e)
    return radius, min_rating


async def _save_prefs(redis_conn, user_id: int, radius: int, min_rating: float):
    try:
        await redis_conn.hset(
            f"user_prefs:{user_id}",
            mapping={"radius": radius, "min_rating": min_rating},
        )
    except RedisError as e:
        logger.warning("prefs save failed: %s", e)


def _params_text(lang_code: str, radius: int, min_rating: float) -> str:
    _ = _t(lang_code)
    rating = _("rating_any_btn") if min_rating == 0 else f"{min_rating:.1f}+"
    return (
        f"{_('params_prompt')}\n\n"
        f"📏 {radius} {_('unit_m')}  ·  ⭐️ {rating}"
    )


async def _ask_location(message: Message, state: FSMContext, lang_code: str):
    await state.set_state(SearchSteps.waiting_for_location)
    _ = _t(lang_code)
    await message.answer(
        f"{_('request_location')}\n\n{_('location_privacy_info')}",
        reply_markup=kb.get_location_keyboard(_),
        parse_mode="HTML",
    )


async def _show_params(
    message: Message, state: FSMContext, lang_code: str, redis_conn, user_id: int
):
    await state.set_state(None)
    data = await state.get_data()
    if "radius" not in data or "min_rating" not in data:
        radius, min_rating = await _load_prefs(redis_conn, user_id)
        await state.update_data(radius=radius, min_rating=min_rating)
    else:
        radius, min_rating = int(data["radius"]), float(data["min_rating"])
    await message.answer(
        _params_text(lang_code, radius, min_rating),
        reply_markup=kb.get_params_keyboard(_t(lang_code), radius, min_rating),
    )


def _format_card(
    lang_code: str, i: int, user_lat: float, user_lon: float, place: dict
) -> str:
    _ = _t(lang_code)
    name = html.escape(str(place.get("name") or "—"))
    rating = place.get("rating")
    rating_txt = f"{rating:.1f}" if isinstance(rating, int | float) else "—"
    total = int(place.get("user_ratings_total") or 0)
    ptype = html.escape(str(place.get("primary_type") or ""))
    vicinity = html.escape(str(place.get("vicinity") or "—"))

    geo = "—"
    if place.get("lat") is not None and place.get("lon") is not None:
        plat, plon = float(place["lat"]), float(place["lon"])
        dist = calculate_distance(user_lat, user_lon, plat, plon)
        direction = bearing_to_direction(
            _, calculate_bearing(user_lat, user_lon, plat, plon)
        )
        geo = f"{dist} {_('unit_m')} · {direction}"

    stars = f"⭐️ {rating_txt}" + (f" ({total})" if total else "")
    if ptype:
        stars += f" · 🍽 {ptype}"
    return f"<b>{i}. {name}</b>\n{stars}\n🧭 {geo}\n🗺 {vicinity}"


def _format_results(
    lang_code: str,
    lat: float,
    lon: float,
    result: SearchResult,
    min_rating: float,
) -> str:
    _ = _t(lang_code)
    parts = []
    if result.relaxed:
        parts.append(f"ℹ️ {_('relaxed_note').format(rating=f'{min_rating:.1f}')}")
    elif not result.rating_available:
        parts.append(f"ℹ️ {_('no_rating_note')}")
    top = result.places[:TOP_N]
    parts += [_format_card(lang_code, i, lat, lon, p) for i, p in enumerate(top, 1)]
    return "\n\n".join(parts)


# --- Команды ---


@router.message(CommandStart())
async def handle_start(
    message: Message,
    state: FSMContext,
    lang_code: str,
    lang_known: bool,
    analytics=None,
):
    await state.clear()
    if analytics and message.from_user:
        await analytics.track_user(message.from_user.id)

    if not lang_known:
        await state.set_state(None)
        await message.answer(
            get_string("select_language", lang="ru"),
            reply_markup=kb.get_language_keyboard(),
        )
        return
    await _ask_location(message, state, lang_code)


@router.message(Command("language"))
async def handle_language(message: Message, lang_code: str):
    await message.answer(
        get_string("select_language", lang=lang_code),
        reply_markup=kb.get_language_keyboard(),
    )


@router.message(Command("stats"))
async def handle_stats(message: Message, analytics=None):
    if (
        not settings.ADMIN_ID
        or not message.from_user
        or message.from_user.id != settings.ADMIN_ID
        or analytics is None
    ):
        return
    s = await analytics.get_today_stats()
    await message.answer(
        f"DAU: {s['active_users']}\nsearches: {s['searches']}\n"
        f"empty: {s['empty_results']}\nfeedback: {s['feedback']}\n"
        f"radius: {s['radius_usage']}\nrating: {s['rating_usage']}"
    )


@router.message(Command("feedback"))
async def handle_feedback_cmd(message: Message, state: FSMContext, lang_code: str):
    if not settings.ADMIN_ID:
        return
    await state.set_state(FeedbackState.waiting_for_feedback)
    await message.answer(get_string("feedback_prompt", lang=lang_code))


@router.message(FeedbackState.waiting_for_feedback, F.text)
async def handle_feedback_text(
    message: Message, state: FSMContext, bot: Bot, lang_code: str, analytics=None
):
    user = message.from_user
    who = f"{user.id} (@{user.username})" if user else "unknown"
    try:
        await bot.send_message(
            settings.ADMIN_ID,
            f"📨 Feedback от {html.escape(who)}:\n\n{html.escape(message.text or '')}",
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        logger.error("feedback delivery failed: %s", e)
    if analytics:
        await analytics.track_feedback_request()
    await state.set_state(None)  # локация/параметры в FSM сохраняются
    await message.answer(get_string("feedback_thanks", lang=lang_code))


# --- Язык ---


@router.callback_query(F.data.startswith("lang_"))
async def set_language(
    callback: CallbackQuery, state: FSMContext, redis_conn, lang_code: str
):
    new_lang = (callback.data or "").split("_", 1)[1]
    msg = _message_of(callback)
    if new_lang not in SUPPORTED_LANGUAGES or msg is None:
        await callback.answer()
        return

    await redis_conn.set(f"user_lang:{callback.from_user.id}", new_lang)
    await callback.answer(get_string("language_selected", lang=new_lang))

    data = await state.get_data()
    if "latitude" in data:
        await _show_params(msg, state, new_lang, redis_conn, callback.from_user.id)
    else:
        await _ask_location(msg, state, new_lang)


# --- Геопозиция → экран параметров ---


@router.message(F.location)
async def got_location(
    message: Message, state: FSMContext, lang_code: str, redis_conn
):
    if message.from_user is None or message.location is None:
        return
    await state.update_data(
        latitude=message.location.latitude, longitude=message.location.longitude
    )
    # убираем reply-клавиатуру «отправить геопозицию» без лишнего сообщения
    await _show_params(message, state, lang_code, redis_conn, message.from_user.id)


@router.message(SearchSteps.waiting_for_location)
async def location_expected(message: Message, state: FSMContext, lang_code: str):
    await _ask_location(message, state, lang_code)


# --- Экран параметров ---


async def _refresh_params(callback: CallbackQuery, lang_code: str, radius, min_rating):
    msg = _message_of(callback)
    if msg is None:
        return
    try:
        await msg.edit_text(
            _params_text(lang_code, radius, min_rating),
            reply_markup=kb.get_params_keyboard(_t(lang_code), radius, min_rating),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data.startswith("radius_"))
async def set_radius(callback: CallbackQuery, state: FSMContext, lang_code: str):
    await callback.answer()
    try:
        radius = int((callback.data or "").split("_", 1)[1])
    except ValueError:
        return
    if radius not in kb.RADII:
        return
    await state.update_data(radius=radius)
    data = await state.get_data()
    min_rating = float(data.get("min_rating", settings.DEFAULT_MIN_RATING))
    await _refresh_params(callback, lang_code, radius, min_rating)


@router.callback_query(F.data.startswith("minrating_"))
async def set_min_rating(callback: CallbackQuery, state: FSMContext, lang_code: str):
    await callback.answer()
    try:
        min_rating = float((callback.data or "").split("_", 1)[1])
    except ValueError:
        return
    if min_rating not in kb.MIN_RATINGS:
        return
    await state.update_data(min_rating=min_rating)
    data = await state.get_data()
    radius = int(data.get("radius", settings.DEFAULT_RADIUS))
    await _refresh_params(callback, lang_code, radius, min_rating)


@router.callback_query(F.data == "change_params")
async def change_params(
    callback: CallbackQuery, state: FSMContext, lang_code: str, redis_conn
):
    await callback.answer()
    msg = _message_of(callback)
    if msg is None:
        return
    data = await state.get_data()
    if "latitude" not in data:
        await _ask_location(msg, state, lang_code)
        return
    await _show_params(msg, state, lang_code, redis_conn, callback.from_user.id)


@router.callback_query(F.data == "new_search")
async def new_search(callback: CallbackQuery, state: FSMContext, lang_code: str):
    await callback.answer()
    msg = _message_of(callback)
    if msg is not None:
        await _ask_location(msg, state, lang_code)


# --- Поиск ---


@router.callback_query(F.data == "search_go")
async def do_search(
    callback: CallbackQuery,
    state: FSMContext,
    lang_code: str,
    redis_conn,
    analytics=None,
):
    _ = _t(lang_code)
    msg = _message_of(callback)
    user_id = callback.from_user.id
    if msg is None:
        await callback.answer()
        return

    # Антиспам: каждый поиск стоит вызовов внешнего API.
    allowed = await redis_conn.set(
        f"throttle:search:{user_id}", 1, nx=True, ex=SEARCH_THROTTLE_SEC
    )
    if not allowed:
        await callback.answer(_("too_fast"))
        return
    await callback.answer()

    data = await state.get_data()
    if "latitude" not in data or "longitude" not in data:
        await _ask_location(msg, state, lang_code)  # FSM протух по TTL
        return

    lat, lon = float(data["latitude"]), float(data["longitude"])
    radius = int(data.get("radius", settings.DEFAULT_RADIUS))
    min_rating = float(data.get("min_rating", settings.DEFAULT_MIN_RATING))
    await _save_prefs(redis_conn, user_id, radius, min_rating)

    if analytics:
        await analytics.track_user(user_id)
        await analytics.track_feature_use("radius", radius)
        await analytics.track_feature_use("rating", min_rating)

    status = await msg.answer(_("searching"))

    try:
        result = await search_places(
            lat=lat,
            lon=lon,
            radius=radius,
            min_rating=min_rating,
            lang_code=lang_code,
            redis_conn=redis_conn,
        )
    except Exception:
        logger.exception("search failed")
        await status.edit_text(
            _("error_generic"), reply_markup=kb.get_retry_keyboard(_)
        )
        return

    if not result.places:
        if analytics:
            await analytics.track_empty_result()
        await status.edit_text(
            _("no_results_radius"), reply_markup=kb.get_retry_keyboard(_)
        )
        return

    if analytics:
        await analytics.track_search_request()

    top = result.places[:TOP_N]
    await status.edit_text(
        _format_results(lang_code, lat, lon, result, min_rating),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb.get_results_keyboard(_, top),
    )


# --- Запасные хендлеры (регистрируются последними) ---


@router.callback_query()
async def stale_callback(callback: CallbackQuery):
    """Кнопка от старой версии/сообщения: просто гасим «часики»."""
    await callback.answer()


@router.message()
async def fallback_message(message: Message, state: FSMContext, lang_code: str):
    await _ask_location(message, state, lang_code)
