from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
import redis.asyncio as redis
from aiogram.fsm.context import FSMContext

# Импортируем из нового, чистого модуля
from bot.services.translator import get_string, DEFAULT_LANG

class I18nMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        state: FSMContext = data.get("state")
        redis_conn: redis.Redis = data.get("redis_conn")
        lang_code = None

        # 1. Сначала пробуем взять язык из FSMContext (если был выбран и сохранён)
        if state is not None:
            fsm_data = await state.get_data()
            lang_code = fsm_data.get("lang_code")

        # 2. Если нет — ищем в Redis
        if not lang_code and redis_conn:
            if user is None:
                lang_code = DEFAULT_LANG
            else:
                lang_code_bytes = await redis_conn.get(f"user_lang:{user.id}")
                lang_code = lang_code_bytes.decode('utf-8') if lang_code_bytes else DEFAULT_LANG

        if not lang_code:
            lang_code = DEFAULT_LANG

        # Передаём функцию-переводчик и язык во все роутеры/handlers
        data["_"] = lambda key, **kwargs: get_string(key, lang_code).format(**kwargs)
        data["lang_code"] = lang_code

        return await handler(event, data)
