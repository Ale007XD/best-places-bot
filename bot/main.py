import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.handlers import user_handlers

async def main():
    """Основная функция для запуска бота."""
    # Настройка логирования
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    # Инициализация бота и диспетчера
    bot = Bot(token=settings.BOT_TOKEN)
    # Используем MemoryStorage для хранения состояний FSM (для простых случаев)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Регистрация роутеров
    dp.include_router(user_handlers.router)

    # Удаление вебхука перед запуском (если он был установлен)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запуск поллинга
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
