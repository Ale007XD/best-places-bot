# bot/middlewares/redis.py

from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
import redis.asyncio as redis


class RedisMiddleware(BaseMiddleware):
    """
    Прокидывает redis_conn в data для всех хендлеров.
    """

    def __init__(self, redis_conn: redis.Redis):
        self.redis_conn = redis_conn

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:

        data["redis_conn"] = self.redis_conn

        return await handler(event, data)
