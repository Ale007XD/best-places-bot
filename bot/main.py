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

    # Создаем соединение с Redis для FSM и i18n.
    # decode_responses=False потому что I18nMiddleware теперь сам декодирует.
    redis_conn = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT)

    bot = Bot(token=settings.BOT_TOKEN)
    storage = MemoryStorage()

    # Передаем соединение в Dispatcher, чтобы он управлял его жизненным циклом
    dp = Dispatcher(storage=storage, redis_conn=redis_conn)

    # Регистрируем Middleware: теперь он не требует аргументов в конструкторе
    dp.update.middleware(I18nMiddleware())

    dp.include_router(user_handlers.router)

    await bot.delete_webhook(drop_pending_updates=True)

    logging.info("Запуск бота...")
    try:
        await dp.start_polling(bot)
    finally:
        await redis_conn.close()
        logging.info("Соединение с Redis закрыто.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
