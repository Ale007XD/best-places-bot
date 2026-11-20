# bot/handlers/community_handlers.py
"""Отметки «был здесь» 👍/👎 под карточками (callback vu:<place_id> / vd:<place_id>)."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from redis.exceptions import RedisError

from bot.config import settings
from bot.services import community
from bot.services.cards import vote_label
from bot.services.translator import get_string

logger = logging.getLogger(__name__)

router = Router()


def _with_counts(
    markup: InlineKeyboardMarkup, pid: str, up: int, down: int, mine: str
) -> InlineKeyboardMarkup:
    """Обновляет счётчики на кнопках 👍/👎 этой карточки; свой голос помечается ✅."""
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if b.callback_data == f"vu:{pid}":
                b = InlineKeyboardButton(
                    text=vote_label("👍", up, mine == "up"), callback_data=b.callback_data
                )
            elif b.callback_data == f"vd:{pid}":
                b = InlineKeyboardButton(
                    text=vote_label("👎", down, mine == "down"), callback_data=b.callback_data
                )
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(("vu:", "vd:")))
async def vote(callback: CallbackQuery, lang_code: str, redis_conn, analytics=None):
    data = callback.data or ""
    pid = data[3:]
    direction = "up" if data.startswith("vu:") else "down"
    uid = callback.from_user.id

    # Не чаще раза в ~0.6 с на пользователя — защита от накрутки кликами.
    if not await redis_conn.set(f"throttle:vote:{uid}", 1, nx=True, px=600):
        await callback.answer()
        return

    try:
        res = await community.record_vote(redis_conn, uid, pid, direction)
    except RedisError:
        logger.exception("vote failed")
        await callback.answer()
        return

    text = get_string("vote_thanks", lang=lang_code).format(up=res.up, down=res.down)
    if res.rewarded:
        text += "\n" + get_string("vote_reward", lang=lang_code)
    await callback.answer(text)

    # Счётчики прямо на кнопках карточки (в личном чате карточка принадлежит одному человеку)
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is not None and msg.reply_markup is not None:
        try:
            await msg.edit_reply_markup(
                reply_markup=_with_counts(msg.reply_markup, pid, res.up, res.down, direction)
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                logger.warning("vote markup update failed: %s", e)

    if analytics and res.changed:
        await analytics.track_feature_use("vote", direction)


def _mark_closed(markup: InlineKeyboardMarkup, pid: str) -> InlineKeyboardMarkup:
    """Кнопка «🚫 Закрыто» этой карточки получает ✅ (жалоба принята)."""
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if b.callback_data == f"cl:{pid}" and not (b.text or "").endswith("✅"):
                b = InlineKeyboardButton(text=f"{b.text} ✅", callback_data=b.callback_data)
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("cl:"))
async def closed_report(callback: CallbackQuery, lang_code: str, redis_conn, analytics=None):
    """«🚫 Закрыто»: жалоба на место. Порог CLOSED_REPORTS_THRESHOLD разных людей (админ — сразу)."""
    pid = (callback.data or "")[3:]
    uid = callback.from_user.id
    if not pid or not await redis_conn.set(f"throttle:closed:{uid}", 1, nx=True, px=600):
        await callback.answer()
        return

    is_admin = bool(settings.ADMIN_ID) and uid == settings.ADMIN_ID
    weight = settings.CLOSED_REPORTS_THRESHOLD if is_admin else 1
    try:
        res = await community.report_closed(redis_conn, uid, pid, weight)
    except RedisError:
        logger.exception("closed report failed")
        await callback.answer()
        return

    if res.hidden:
        text = get_string("closed_hidden", lang=lang_code)
    elif res.first:
        text = get_string("closed_thanks", lang=lang_code).format(
            n=res.total, need=settings.CLOSED_REPORTS_THRESHOLD
        )
    else:
        text = get_string("closed_already", lang=lang_code)
    if res.rewarded:
        text += "\n" + get_string("vote_reward", lang=lang_code)
    await callback.answer(text)

    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is not None and msg.reply_markup is not None:
        try:
            await msg.edit_reply_markup(reply_markup=_mark_closed(msg.reply_markup, pid))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                logger.warning("closed markup update failed: %s", e)

    if analytics and res.first:
        await analytics.track_feature_use("closed_report", "hidden" if res.hidden else "pending")
