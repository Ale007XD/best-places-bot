# bot/handlers/user_handlers.py
"""
Диалог:
  /start → (язык, только если не удалось определить) → геопозиция →
  ОДИН экран параметров (радиус + мин. рейтинг + «Искать») → одно сообщение с топ-3.

Точка поиска может быть любой (FSM: latitude/longitude — где ищем; home_lat/home_lon — где
пользователь сам; point_label — подпись выбранной точки, None = «рядом со мной»):
  • геопозиция из Telegram (📎 → Геопозиция → любое место на карте);
  • venue (место, присланное из другого приложения);
  • текст: координаты, ссылка Google Maps (и короткая), адрес/название (Nominatim).
Выбор временной точки НЕ перезаписывает «домашнюю»; вернуться — кнопка «📍 Рядом со мной».

Параметры (радиус, категория, мин. рейтинг) запоминаются в Redis (user_prefs:{id}) и
подставляются при следующем поиске. Присланная «моя» геопозиция дублируется в
user_lastloc:{id} (TTL LASTLOC_TTL_HOURS) — это точка-фолбэк для inline без геопозиции.
"""

import html
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from redis.exceptions import RedisError

from bot.config import settings
from bot.handlers import account_handlers as acc
from bot.keyboards import inline_keyboards as kb
from bot.services import cards, community, quota, specialties
from bot.services.translator import SUPPORTED_LANGUAGES, get_string
from bot.utils import geocoder, links
from bot.utils.attribution import attribution
from bot.utils.places_service import TOP_N, SearchResult, search_places
from bot.utils.querytools import KINDS as _KINDS

logger = logging.getLogger(__name__)

router = Router()

SEARCH_THROTTLE_SEC = 4
GEOCODE_THROTTLE_SEC = 2


class SearchSteps(StatesGroup):
    waiting_for_location = State()  # ждём «мою» геопозицию (кнопка под полем ввода)
    waiting_for_point = State()  # ждём любую другую точку (после «🗺 Другая точка»)


class FeedbackState(StatesGroup):
    waiting_for_feedback = State()


# --- Вспомогательное ---


def _t(lang_code: str):
    return lambda key: get_string(key, lang=lang_code)


def _message_of(callback: CallbackQuery) -> Message | None:
    """callback.message может быть None / недоступным (старое сообщение)."""
    return callback.message if isinstance(callback.message, Message) else None


def _radius_of(data: dict) -> int:
    """Радиус из FSM; устаревшее значение (до смены набора радиусов) заменяется на дефолт."""
    try:
        radius = int(data.get("radius", settings.DEFAULT_RADIUS))
    except (TypeError, ValueError):
        return settings.DEFAULT_RADIUS
    return radius if radius in kb.RADII else settings.DEFAULT_RADIUS


async def _load_prefs(redis_conn, user_id: int) -> tuple[int, float, str]:
    radius, min_rating, kind = settings.DEFAULT_RADIUS, settings.DEFAULT_MIN_RATING, "all"
    try:
        prefs = await redis_conn.hgetall(f"user_prefs:{user_id}")
        if prefs.get("radius") in {str(r) for r in kb.RADII}:
            radius = int(prefs["radius"])
        if prefs.get("min_rating") in {str(m) for m in kb.MIN_RATINGS}:
            min_rating = float(prefs["min_rating"])
        if prefs.get("kind") in _KINDS:
            kind = prefs["kind"]
    except RedisError as e:  # prefs — удобство, не критичный путь
        logger.warning("prefs load failed: %s", e)
    return radius, min_rating, kind


async def _save_prefs(redis_conn, user_id: int, radius: int, min_rating: float, kind: str = "all"):
    try:
        await redis_conn.hset(
            f"user_prefs:{user_id}",
            mapping={"radius": radius, "min_rating": min_rating, "kind": kind},
        )
    except RedisError as e:
        logger.warning("prefs save failed: %s", e)


def _params_text(
    lang_code: str,
    radius: int,
    min_rating: float,
    label: str | None,
    locked: bool = False,
    kind: str = "all",
) -> str:
    _ = _t(lang_code)
    where = _("where_point").format(label=label) if label else _("where_me")
    summary = f"📏 {radius} {_('unit_m')}"
    if kind != "all":
        summary += f"  ·  {_(kb.KIND_KEYS[kind])}"
    if not locked:  # фильтр рейтинга недоступен — не показываем его в сводке
        rating = _("rating_any_btn") if min_rating == 0 else f"{min_rating:.1f}+"
        summary += f"  ·  ⭐️ {rating}"
    return f"{_('params_prompt')}\n\n{where}\n{summary}"


def _params_markup(lang_code: str, data: dict):
    radius = _radius_of(data)
    min_rating = float(data.get("min_rating", settings.DEFAULT_MIN_RATING))
    show_home = bool(data.get("point_label")) and "home_lat" in data
    return kb.get_params_keyboard(
        _t(lang_code),
        radius,
        min_rating,
        show_home,
        bool(data.get("rating_locked")),
        data.get("kind", "all"),
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
    if "radius" not in data or "min_rating" not in data or "kind" not in data:
        radius, min_rating, kind = await _load_prefs(redis_conn, user_id)
        await state.update_data(radius=radius, min_rating=min_rating, kind=kind)
    # Тизер: когда поиски с рейтингами на сегодня кончились, фильтр рейтинга заперт (🔒).
    try:
        st = await quota.get_status(redis_conn, user_id)
        await state.update_data(rating_locked=st.money_enabled and not st.can_rate)
    except RedisError:
        await state.update_data(rating_locked=False)
    data = await state.get_data()
    await message.answer(
        _params_text(
            lang_code,
            int(data["radius"]),
            float(data["min_rating"]),
            data.get("point_label"),
            bool(data.get("rating_locked")),
            data.get("kind", "all"),
        ),
        reply_markup=_params_markup(lang_code, data),
    )


async def _apply_point(
    message: Message,
    state: FSMContext,
    lang_code: str,
    redis_conn,
    user_id: int,
    lat: float,
    lon: float,
    label: str | None,
    as_home: bool = False,
    accuracy: float | None = None,
):
    """Задаёт точку поиска. Домашнюю точку перезаписывает только as_home.

    accuracy — horizontal_accuracy геопозиции Telegram, м (у точки, выбранной на карте, его нет)."""
    upd: dict = {
        "latitude": lat,
        "longitude": lon,
        "point_label": None if as_home else label,
        "accuracy": accuracy,
    }
    if as_home:
        upd.update(home_lat=lat, home_lon=lon, home_accuracy=accuracy)
        try:  # фолбэк-точка для inline-запросов без геопозиции
            await redis_conn.set(
                f"user_lastloc:{user_id}", f"{lat},{lon}", ex=settings.LASTLOC_TTL_HOURS * 3600
            )
        except RedisError as e:
            logger.warning("lastloc save failed: %s", e)
    await state.update_data(**upd)
    await _show_params(message, state, lang_code, redis_conn, user_id)


def _format_header(
    lang_code: str,
    result: SearchResult,
    min_rating: float,
    label: str | None,
    upsell: bool,
    dish: str | None = None,
) -> str:
    """Заголовок выдачи (как на макете): «✨ Топ-3 места рядом с вами» + подзаголовок и пометки.

    dish — название блюда: заголовок поиска блюда, без пометок про рейтинги."""
    _ = _t(lang_code)
    if dish:
        lines = [_("dish_title").format(dish=html.escape(dish, quote=False)), _("dish_subtitle")]
        if label:
            lines.insert(1, f"📍 {html.escape(label, quote=False)}")
    elif label:
        lines = [f"✨ <b>{_('results_title_point')}</b>", f"📍 {html.escape(label, quote=False)}"]
    else:
        lines = [f"✨ <b>{_('results_title')}</b>", _("results_subtitle")]
    notes = []
    if result.radius_used > result.radius_asked > 0:
        key = "accuracy_note" if result.by_accuracy else "radius_expanded_note"
        notes.append(f"🔎 {_(key).format(asked=result.radius_asked, used=result.radius_used)}")
    if dish:
        pass  # рейтинги в поиске блюда не используются
    elif upsell:
        notes.append(f"🔒 {_('upsell_note')}")
    elif result.relaxed:
        notes.append(f"ℹ️ {_('relaxed_note').format(rating=f'{min_rating:.1f}')}")
    elif not result.rating_available:
        notes.append(f"ℹ️ {_('no_rating_note')}")
    return "\n".join(lines + ([""] + notes if notes else []))


# --- Команды ---


@router.message(CommandStart())
async def handle_start(
    message: Message,
    state: FSMContext,
    bot: Bot,
    command: CommandObject,
    lang_code: str,
    lang_known: bool,
    redis_conn,
    analytics=None,
):
    await state.clear()
    if analytics and message.from_user:
        await analytics.track_user(message.from_user.id)

    # Реферальная ссылка: /start ref_<id пригласившего>
    args = (command.args or "").strip()
    if message.from_user and args.startswith("ref_") and args[4:].isdigit():
        await quota.register_referrer(redis_conn, message.from_user.id, int(args[4:]))

    if analytics and re.fullmatch(r"site_[a-z0-9_]{1,30}", args):  # переход с сайта: site_ru, site_guide_en…
        await analytics.track_feature_use("site", args)

    if not lang_known:
        await state.set_state(None)
        await message.answer(
            get_string("select_language", lang="ru"),
            reply_markup=kb.get_language_keyboard(),
        )
        return
    if args == "inline_help" and message.from_user:  # кнопка над inline-выдачей
        radius, _rating, _kind = await _load_prefs(redis_conn, message.from_user.id)
        radius = (
            radius if radius in kb.RADII else settings.INLINE_DEFAULT_RADIUS
        )  # то же, что видит inline
        try:
            username = await acc.bot_username(bot)
        except TelegramAPIError:
            username = ""
        await message.answer(
            get_string("inline_help_text", lang=lang_code).format(
                bot=f"@{username}" if username else "@bot", radius=radius
            )
        )
    await _ask_location(message, state, lang_code)


@router.message(Command("language"))
async def handle_language(message: Message, lang_code: str):
    await message.answer(
        get_string("select_language", lang=lang_code),
        reply_markup=kb.get_language_keyboard(),
    )


def _pct(part: str | None, total: str | None) -> str:
    try:
        denominator = int(total or 0)
        return f"{100 * int(part or 0) / denominator:.0f}%" if denominator else "—"
    except ValueError:
        return "—"


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
    q = await analytics.get_quality_stats()
    n = q.get("searches")
    avg = f"{int(q.get('latency_ms_sum', 0)) / int(n):.0f} мс" if n and int(n) else "—"
    await message.answer(
        f"DAU: {s['active_users']}\nsearches: {s['searches']}\n"
        f"empty: {s['empty_results']}\nfeedback: {s['feedback']}\n"
        f"radius: {s['radius_usage']}\nrating: {s['rating_usage']}\n"
        f"purchases: {s['purchases']} · Stars: {s['stars']}\npaywall: {s['paywall']}\n\n"
        f"📊 Качество выдачи (сегодня, поисков: {n or 0})\n"
        f"3 места найдено: {_pct(q.get('results3'), n)}\n"
        f"без рейтинга: {_pct(q.get('no_rating'), n)} · добор: {_pct(q.get('relaxed'), n)}\n"
        f"из кэша: {_pct(q.get('cache_hits'), n)} · с обращением к API: {q.get('api_searches', 0)}\n"
        f"сбои Foursquare: {q.get('fsq_failed', 0)} · OSM-фолбэк: {q.get('osm_used', 0)} · "
        f"недоступно: {q.get('unavailable', 0)}\n"
        f"среднее время: {avg}"
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


# --- Точка поиска: геопозиция, venue, текст ---


@router.message(F.location)
async def got_location(
    message: Message, state: FSMContext, lang_code: str, redis_conn
):
    if message.from_user is None or message.location is None:
        return
    data = await state.get_data()
    current = await state.get_state()
    # «Моя» геопозиция — то, что пришло в ответ на нашу кнопку (или если дома ещё нет).
    # Любая другая присланная точка (в т.ч. выбранная на карте) — временная.
    as_home = current == SearchSteps.waiting_for_location.state or "home_lat" not in data
    await _apply_point(
        message,
        state,
        lang_code,
        redis_conn,
        message.from_user.id,
        message.location.latitude,
        message.location.longitude,
        label=get_string("pin_label", lang=lang_code),
        as_home=as_home,
        accuracy=message.location.horizontal_accuracy,
    )


@router.message(F.venue)
async def got_venue(message: Message, state: FSMContext, lang_code: str, redis_conn):
    if message.from_user is None or message.venue is None:
        return
    loc = message.venue.location
    await _apply_point(
        message,
        state,
        lang_code,
        redis_conn,
        message.from_user.id,
        loc.latitude,
        loc.longitude,
        label=message.venue.title,
    )


@router.message(F.text, ~F.text.startswith("/"))
async def got_text_point(
    message: Message, state: FSMContext, bot: Bot, lang_code: str, redis_conn, analytics=None
):
    """Текст как точка: координаты → ссылка на карту → адрес/название (геокодер)."""
    if message.from_user is None or not message.text:
        return
    _ = _t(lang_code)
    uid = message.from_user.id
    text = message.text.strip()

    if not await redis_conn.set(f"throttle:geo:{uid}", 1, nx=True, ex=GEOCODE_THROTTLE_SEC):
        await message.answer(_("too_fast"))
        return

    pt = await geocoder.parse_point(text)
    if pt:
        await _apply_point(
            message, state, lang_code, redis_conn, uid, pt[0], pt[1], label=_("coords_label")
        )
        return

    data = await state.get_data()
    # Название блюда из словаря города, где пользователь сейчас («pho», «Bún chả»), — это не
    # адрес, а поиск заведений с этим блюдом вокруг текущей точки.
    if "latitude" in data and "longitude" in data:
        hit = specialties.resolve_dish(text, float(data["latitude"]), float(data["longitude"]))
        if hit:
            city, dish = hit
            if not await redis_conn.set(f"throttle:search:{uid}", 1, nx=True, ex=SEARCH_THROTTLE_SEC):
                await message.answer(_("too_fast"))
                return
            ref = specialties.dish_ref(city["id"], dish["id"])
            await _run_search(message, uid, state, bot, lang_code, redis_conn, analytics, dish_ref=ref)
            return

    if "http://" in text or "https://" in text or len(text) < 3 or len(text) > 150:
        await message.answer(_("point_not_found"))
        return
    if not settings.GEOCODER_ENABLED:
        await message.answer(_("geo_unavailable"))
        return

    near = None
    if "home_lat" in data:
        near = (float(data["home_lat"]), float(data["home_lon"]))
    elif "latitude" in data:
        near = (float(data["latitude"]), float(data["longitude"]))

    candidates = await geocoder.geocode(text, lang_code, redis_conn, near=near)
    if not candidates:
        await message.answer(_("point_not_found"))
        return
    if len(candidates) == 1:
        c = candidates[0]
        await _apply_point(message, state, lang_code, redis_conn, uid, c.lat, c.lon, c.label)
        return

    await state.update_data(geo_candidates=[list(c) for c in candidates])
    await message.answer(
        _("geo_choose"),
        reply_markup=kb.get_geo_choices_keyboard([c.label for c in candidates]),
    )


@router.callback_query(F.data.startswith("geo_"))
async def choose_geo_candidate(
    callback: CallbackQuery, state: FSMContext, lang_code: str, redis_conn
):
    await callback.answer()
    msg = _message_of(callback)
    data = await state.get_data()
    try:
        idx = int((callback.data or "").split("_", 1)[1])
        lat, lon, label = data["geo_candidates"][idx]
    except (ValueError, KeyError, IndexError):
        if msg is not None:
            await _ask_location(msg, state, lang_code)
        return
    if msg is not None:
        await _apply_point(
            msg, state, lang_code, redis_conn, callback.from_user.id, float(lat), float(lon), label
        )


@router.callback_query(F.data == "pick_point")
async def pick_point(callback: CallbackQuery, state: FSMContext, lang_code: str):
    await callback.answer()
    msg = _message_of(callback)
    if msg is None:
        return
    await state.set_state(SearchSteps.waiting_for_point)
    await msg.answer(get_string("pick_point_prompt", lang=lang_code))


@router.callback_query(F.data == "use_home")
async def use_home(
    callback: CallbackQuery, state: FSMContext, lang_code: str, redis_conn
):
    await callback.answer()
    msg = _message_of(callback)
    data = await state.get_data()
    if msg is None:
        return
    if "home_lat" not in data:
        await _ask_location(msg, state, lang_code)
        return
    await state.update_data(
        latitude=data["home_lat"],
        longitude=data["home_lon"],
        point_label=None,
        accuracy=data.get("home_accuracy"),
    )
    await _show_params(msg, state, lang_code, redis_conn, callback.from_user.id)


@router.message(SearchSteps.waiting_for_location)
@router.message(SearchSteps.waiting_for_point)
async def non_point_message(message: Message, state: FSMContext, lang_code: str):
    """Стикер/фото и т.п. вместо точки."""
    await message.answer(get_string("pick_point_prompt", lang=lang_code))


# --- Экран параметров ---


async def _refresh_params(callback: CallbackQuery, lang_code: str, data: dict):
    msg = _message_of(callback)
    if msg is None:
        return
    try:
        await msg.edit_text(
            _params_text(
                lang_code,
                _radius_of(data),
                float(data.get("min_rating", settings.DEFAULT_MIN_RATING)),
                data.get("point_label"),
                bool(data.get("rating_locked")),
                data.get("kind", "all"),
            ),
            reply_markup=_params_markup(lang_code, data),
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
    await _refresh_params(callback, lang_code, await state.get_data())


@router.callback_query(F.data.startswith("kind_"))
async def set_kind(callback: CallbackQuery, state: FSMContext, lang_code: str):
    await callback.answer()
    kind = (callback.data or "").split("_", 1)[1]
    if kind not in kb.KINDS:
        return
    await state.update_data(kind=kind)
    await _refresh_params(callback, lang_code, await state.get_data())


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
    await _refresh_params(callback, lang_code, await state.get_data())


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


@router.callback_query(F.data == "try_here")
async def try_here(callback: CallbackQuery, state: FSMContext, lang_code: str):
    """Бесплатная карточка «Что попробовать здесь» по городу точки поиска (без внешних запросов)."""
    msg = _message_of(callback)
    data = await state.get_data()
    if msg is None or "latitude" not in data or "longitude" not in data:
        await callback.answer(get_string("try_here_none", lang=lang_code), show_alert=True)
        return
    card = specialties.card_for_point(lang_code, float(data["latitude"]), float(data["longitude"]))
    if card is None:
        await callback.answer(get_string("try_here_none", lang=lang_code), show_alert=True)
        return
    text, city, dishes = card
    await callback.answer()
    await msg.answer(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb.get_dish_keyboard(city["id"], dishes),
    )


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
    bot: Bot,
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
    await _run_search(msg, user_id, state, bot, lang_code, redis_conn, analytics)


@router.callback_query(F.data.startswith(specialties.DISH_PREFIX))
async def dish_search(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    lang_code: str,
    redis_conn,
    analytics=None,
):
    """Кнопка блюда в карточке «Что попробовать здесь» → поиск таких заведений рядом."""
    _ = _t(lang_code)
    msg = _message_of(callback)
    user_id = callback.from_user.id
    ref = (callback.data or "")[len(specialties.DISH_PREFIX) :]
    if msg is None or specialties.get_dish(ref) is None:
        await callback.answer(_("try_here_none"), show_alert=True)
        return
    allowed = await redis_conn.set(
        f"throttle:search:{user_id}", 1, nx=True, ex=SEARCH_THROTTLE_SEC
    )
    if not allowed:
        await callback.answer(_("too_fast"))
        return
    await callback.answer()
    await _run_search(msg, user_id, state, bot, lang_code, redis_conn, analytics, dish_ref=ref)


async def _run_search(
    msg: Message,
    user_id: int,
    state: FSMContext,
    bot: Bot,
    lang_code: str,
    redis_conn,
    analytics=None,
    dish_ref: str | None = None,
):
    """Поиск вокруг точки из FSM и выдача. dish_ref («город:блюдо») — поиск заведений с блюдом:
    базовый слой (без платных рейтингов), радиус пользователя, расширение при нехватке мест."""
    _ = _t(lang_code)
    found = specialties.get_dish(dish_ref) if dish_ref else None
    dish_city, dish = found if found else (None, None)
    needles = specialties.venue_needles(dish) if dish else ()
    spellings = specialties.venue_spellings(dish) if dish else ()
    dish_label = specialties.dish_title(dish, lang_code) if dish else None

    data = await state.get_data()
    if "latitude" not in data or "longitude" not in data:
        await _ask_location(msg, state, lang_code)  # FSM протух по TTL
        return

    lat, lon = float(data["latitude"]), float(data["longitude"])
    radius = _radius_of(data)
    min_rating = float(data.get("min_rating", settings.DEFAULT_MIN_RATING))
    kind = data.get("kind", "all")
    if kind not in kb.KINDS or dish:
        kind = "all"  # блюдо ищем среди всех заведений
    label = data.get("point_label")

    # Тизер: «богатый» поиск (рейтинги + фото, платные поля Foursquare) — из бесплатного
    # дневного лимита / бонусных поисков / Pro; иначе базовая выдача без них (почти безлимитно).
    quota_status = await quota.get_status(redis_conn, user_id)
    entitled = quota_status.can_rate and dish is None  # поиск блюда — базовый слой
    if not entitled and not quota_status.can_search_base:
        if analytics:
            await analytics.track_feature_use("paywall", "shown")
        await acc.show_paywall(msg, bot, lang_code, user_id, "paywall_text")
        return

    if not dish:  # настройки не перезаписываем поиском блюда
        await _save_prefs(redis_conn, user_id, radius, min_rating, kind)

    if analytics:
        await analytics.track_user(user_id)
        await analytics.track_feature_use("radius", radius)
        if dish_city and dish:
            await analytics.track_feature_use("dish", specialties.dish_ref(dish_city["id"], dish["id"]))
        if kind != "all":
            await analytics.track_feature_use("kind", kind)
        if entitled:
            await analytics.track_feature_use("rating", min_rating)

    status = await msg.answer(_("searching"))

    started = time.monotonic()
    try:
        result = await search_places(
            lat=lat,
            lon=lon,
            radius=radius,
            min_rating=min_rating if entitled else 0.0,
            lang_code=lang_code,
            redis_conn=redis_conn,
            with_ratings=entitled,
            kind=kind,
            accuracy_m=data.get("accuracy"),
            dish_tokens=needles,
            dish_spellings=spellings,
        )
    except Exception:
        logger.exception("search failed")
        if analytics:
            await analytics.track_search_failure()
        await status.edit_text(
            _("error_generic"), reply_markup=kb.get_retry_keyboard(_)
        )
        return
    latency_ms = int((time.monotonic() - started) * 1000)

    if not result.places:
        if analytics:
            await analytics.track_empty_result()
        no_results = (
            _("dish_none").format(dish=dish_label, radius=result.radius_used or radius)
            if dish
            else _("no_results_radius")
        )
        await status.edit_text(no_results, reply_markup=kb.get_retry_keyboard(_))
        return

    top = result.places[:TOP_N]
    if analytics:
        await analytics.track_search_request()
        await analytics.track_search_quality(
            n_results=len(top),
            rating_available=result.rating_available,
            relaxed=result.relaxed,
            from_cache=result.from_cache,
            fsq_ok=result.fsq_ok,
            used_osm=result.used_osm,
            latency_ms=latency_ms,
            rated=result.rated,
        )

    # Апселл — только когда монетизация включена и платные данные скрыты из-за лимита.
    upsell = (
        quota_status.money_enabled and not quota_status.unlimited and not entitled and dish is None
    )
    head = _format_header(lang_code, result, min_rating, label, upsell, dish=dish_label)
    show_try = specialties.has_dishes(lat, lon)  # есть подборка «что попробовать» для этого города
    tail: list[str] = []  # блоки под карточками: серия/лимит, подсказка, атрибуция
    st_after: quota.Status | None = None

    # Геймификация / монетизация (ошибки Redis здесь не должны ломать выдачу)
    referrer_id = None
    try:
        await community.remember_places(redis_conn, top)
        if not result.from_cache:
            if entitled and result.rated:
                await quota.consume(redis_conn, user_id, quota_status)  # платные поля
            else:
                await quota.consume_base(redis_conn, user_id)
        streak = await quota.touch_streak(redis_conn, user_id)
        referrer_id = await quota.reward_referral_if_any(redis_conn, user_id)

        notes = []
        if streak.freeze_used:
            notes.append(_("streak_freeze_used"))
        if streak.milestone_bonus:
            notes.append(
                _("streak_milestone").format(
                    days=streak.days, bonus=streak.milestone_bonus
                )
            )
        if referrer_id is not None:
            notes.append(
                _("referral_reward_new").format(bonus=settings.REFERRAL_BONUS)
            )
        st_after = await quota.get_status(redis_conn, user_id)
        footer = acc.format_footer(lang_code, st_after, streak.days)
        extra = [*notes, footer] if footer else notes
        if extra:
            tail.append("\n".join(extra))
    except RedisError:
        logger.exception("quota bookkeeping failed")
        referrer_id = None

    # Подсказка к кнопкам; про бонус за отметки — только если лимиты вообще действуют.
    bonus_on = bool(
        settings.VOTE_REWARD_EVERY > 0 and st_after is not None and not st_after.unlimited
    )
    hint = (
        _("vote_hint_bonus").format(n=settings.VOTE_REWARD_EVERY) if bonus_on else _("vote_hint")
    )
    tail.append(hint)
    # Источник данных (OSM/ODbL, Geoapify, Foursquare) требует указания.
    if credit := attribution(result.provider, top):
        tail.append(credit)

    try:
        ref = await acc.ref_link(bot, user_id)
    except TelegramAPIError:  # getMe не ответил — выдачу не ломаем, просто без ссылки на бота
        logger.warning("ref link unavailable")
        ref = ""

    if await cards.get_layout(redis_conn) == "compact":
        # Один пост: заголовок, карточки-цитаты, хвост; кнопки — рядами под сообщением.
        blocks = [
            cards.build_caption(lang_code, i, p, lat, lon, compact=True)
            for i, p in enumerate(top, start=1)
        ]
        await status.edit_text(
            "\n\n".join([head, *blocks, *tail]),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb.get_compact_keyboard(
                _,
                top,
                [links.map_url(p) for p in top],
                [links.share_url(p, ref, lang_code) for p in top],
                show_try=show_try,
            ),
        )
        if referrer_id is not None:
            await acc.notify_referrer(bot, redis_conn, referrer_id)
        return

    # Заголовок выдачи (бывшее «Ищу…»), под ним — карточки: фото + название + рейтинг + кнопки.
    await status.edit_text(
        "\n\n".join([head, *tail]),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb.get_results_actions_keyboard(_, show_unlock=upsell, show_try=show_try),
    )
    for i, place in enumerate(top, start=1):
        await cards.send_place_card(
            bot, msg.chat.id, redis_conn, lang_code, i, place, lat, lon, ref
        )
    if referrer_id is not None:
        await acc.notify_referrer(bot, redis_conn, referrer_id)


# --- Запасные хендлеры (регистрируются последними) ---


@router.callback_query()
async def stale_callback(callback: CallbackQuery):
    """Кнопка от старой версии/сообщения: просто гасим «часики»."""
    await callback.answer()


@router.message()
async def fallback_message(message: Message, state: FSMContext, lang_code: str):
    await _ask_location(message, state, lang_code)
