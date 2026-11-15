# bot/handlers/community_handlers.py
"""Отметки «был здесь» 👍/👎 под карточками (callback vu:<place_id> / vd:<place_id>)."""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery
from redis.exceptions import RedisError

from bot.services import community
from bot.services.translator import get_string

logger = logging.getLogger(__name__)

router = Router()


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
    if analytics and res.changed:
        await analytics.track_feature_use("vote", direction)
