import json
from pathlib import Path
from typing import Callable, Dict, Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
import redis.asyncio as redis

# --- СИСТЕМА ПЕРЕВОДОВ ---

def load_translations():
    """Загружает все файлы переводов из папки locales в память."""
    translations = {}
    locales_dir = Path(__file__).parent.parent / "locales"
    for file in locales_dir.glob("*.json"):
        lang_code = file.stem
        with open(file, "r", encoding="utf-8") as f:
            translations[lang_code] = json.load(f)
    return translations

TRANSLATIONS = load_translations()
DEFAULT_LANG = "ru" # Язык по умолчанию, если у пользователя не выбран другой

def get_string(key: str, lang: str = DEFAULT_LANG) -> str:
    """
    Получает строку перевода по ключу и языку.
    Если ключ не найден в выбранном языке, пытается найти его в языке по умолчанию.
    """
    return TRANSLATIONS.get(lang, {}).get(key, TRANSLATIONS.get(DEFAULT_LANG, {}).get(key, f"_{key}_"))

# --- MIDDLEWARE ДЛЯ AIOGRAM ---

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
        
        if user is None:
            lang_code = DEFAULT_LANG
        else:
            lang_code = await self.redis.get(f"user_lang:{user.id}")
            if not lang_code:
                lang_code = DEFAULT_LANG
        
        # Передаем в обработчики и другие middleware функцию-переводчик `_`
        data["_"] = lambda key, **kwargs: get_string(key, lang_code).format(**kwargs)
        # Также передаем сам код языка, он может понадобиться
        data["lang_code"] = lang_code
        # И соединение с Redis, чтобы не импортировать его в хэндлерах
        data["redis_conn"] = self.redis

        return await handler(event, data)
