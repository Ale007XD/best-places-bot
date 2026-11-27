import asyncio
import logging

import redis.asyncio as redis
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage

from bot.config import settings
from bot.handlers import (
    account_handlers,
    community_handlers,
    inline_handlers,
    user_handlers,
)
from bot.middlewares.i18n import I18nMiddleware
from bot.services import bot_profile
from bot.utils.analytics import Analytics
from bot.utils.http import close_client

logger = logging.getLogger(__name__)

# Геопозиция и параметры лежат в FSM — не держим их дольше этого срока.
FSM_TTL_SEC = 6 * 3600


async def main():
    """Основная функция для настройки и запуска бота."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    # httpx на INFO пишет полные URL запросов: там токены API и координаты пользователей.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    redis_conn = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    await redis_conn.ping()  # падаем сразу и понятно, если Redis недоступен

    storage = RedisStorage(
        redis=redis_conn, state_ttl=FSM_TTL_SEC, data_ttl=FSM_TTL_SEC
    )

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=storage)

    # Доступно в хендлерах как аргумент `analytics`
    dp["analytics"] = Analytics(redis_conn=redis_conn)

    dp.update.middleware(I18nMiddleware(redis_conn))
    # account_handlers — первым: у user_handlers в конце есть catch-all хендлеры
    dp.include_router(account_handlers.router)
    dp.include_router(community_handlers.router)
    dp.include_router(inline_handlers.router)
    dp.include_router(user_handlers.router)

    if not settings.FSQ_API_KEY:
        logger.warning("FSQ_API_KEY пуст — рейтинги и Foursquare как провайдер отключены (базовый поиск работает)")

    await bot_profile.apply(bot)  # меню команд и описания — из bot/data/bot_profile.json

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Запуск бота...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await close_client()
        await redis_conn.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
