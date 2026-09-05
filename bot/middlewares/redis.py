# bot/middlewares/redis.py

from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as redis
from aiogram import BaseMiddleware


class RedisMiddleware(BaseMiddleware):
    """
    Прокидывает redis_conn в data для всех хендлеров.
    """

    def __init__(self, redis_conn: redis.Redis):
        self.redis_conn = redis_conn

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:

        data["redis_conn"] = self.redis_conn

        return await handler(event, data)
