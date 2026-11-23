from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from bot.services.translator import (
    DEFAULT_LANG,
    SUPPORTED_LANGUAGES,
    detect_language,
    get_string,
)


class I18nMiddleware(BaseMiddleware):
    """
    Кладёт в data: `_` (переводчик), `lang_code`, `lang_known`, `redis_conn`.

    Язык: сохранённый в Redis → иначе определённый по языку Telegram (и сразу
    сохраняется) → иначе DEFAULT_LANG с lang_known=False (тогда бот спросит язык).
    """

    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        lang_code = DEFAULT_LANG
        lang_known = False

        if user is not None:
            key = f"user_lang:{user.id}"
            stored = await self.redis.get(key)
            if stored in SUPPORTED_LANGUAGES:
                lang_code, lang_known = str(stored), True
            else:
                detected = detect_language(user.language_code)
                if detected:
                    lang_code, lang_known = detected, True
                    await self.redis.set(key, detected)

        data["_"] = lambda key, **kwargs: get_string(key, lang_code).format(**kwargs)
        data["lang_code"] = lang_code
        data["lang_known"] = lang_known
        data["redis_conn"] = self.redis

        return await handler(event, data)
