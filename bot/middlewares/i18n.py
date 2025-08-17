from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
import redis.asyncio as redis
from aiogram.fsm.context import FSMContext

# Импортируем из нового, чистого модуля
from bot.services.translator import get_string, DEFAULT_LANG

class I18nMiddleware(BaseMiddleware):
    def __init__(self, redis_conn: redis.Redis):
        self.redis = redis_conn
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        state: FSMContext = data.get("state")
        lang_code = None

        # 1. Сначала пробуем взять язык из FSMContext (если был выбран и сохранён)
        if state is not None:
            fsm_data = await state.get_data()
            lang_code = fsm_data.get("lang_code")

        # 2. Если нет — ищем в Redis
        if not lang_code:
            if user is None:
                lang_code = DEFAULT_LANG
            else:
                lang_code = await self.redis.get(f"user_lang:{user.id}")
                if not lang_code:
                    lang_code = DEFAULT_LANG

        # Передаём функцию-переводчик и язык во все роутеры/handlers
        data["_"] = lambda key, **kwargs: get_string(key, lang_code).format(**kwargs)
        data["lang_code"] = lang_code
        data["redis_conn"] = self.redis

        return await handler(event, data)
