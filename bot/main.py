import asyncio
import logging
import redis.asyncio as redis
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.handlers import user_handlers
from bot.middlewares.i18n import I18nMiddleware

async def main():
    """Основная функция для настройки и запуска бота."""
    logging.basicConfig(
        level=logging.INFO, 
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    )

    # --- Инициализация Redis ---
    redis_conn = redis.Redis(host='redis', port=6379, decode_responses=True)
    
    bot = Bot(token=settings.BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # --- РЕГИСТРАЦИЯ MIDDLEWARE ---
    dp.update.middleware(I18nMiddleware(redis_conn))

    dp.include_router(user_handlers.router)

    await bot.delete_webhook(drop_pending_updates=True)
    
    logging.info("Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
