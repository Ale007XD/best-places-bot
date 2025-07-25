import json
from pathlib import Path
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
import redis.asyncio as redis

# --- СИСТЕМА ПЕРЕВОДОВ ---

# Загружаем все переводы в память при старте
def load_translations():
    translations = {}
    locales_dir = Path(__file__).parent.parent / "locales"
    for file in locales_dir.glob("*.json"):
        lang_code = file.stem
        with open(file, "r", encoding="utf-8") as f:
            translations[lang_code] = json.load(f)
    return translations

TRANSLATIONS = load_translations()
DEFAULT_LANG = "ru"

def get_string(key: str, lang: str = DEFAULT_LANG) -> str:
    """Получает строку перевода по ключу и языку."""
    return TRANSLATIONS.get(lang, {}).get(key, TRANSLATIONS[DEFAULT_LANG].get(key, f"_{key}_"))

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
        # Получаем пользователя из события, если он есть
        user = data.get("event_from_user")
        if user is None:
            # Если пользователя нет (например, обновление канала), используем язык по умолчанию
            lang = DEFAULT_LANG
        else:
            # Получаем язык пользователя из Redis
            lang = await self.redis.get(f"user_lang:{user.id}")
            if not lang:
                # Если в Redis ничего нет, используем язык по умолчанию
                lang = DEFAULT_LANG
        
        # Передаем в обработчик функцию-переводчик с уже "зашитым" языком
        # Теперь в хэндлерах можно будет вызывать _("key")
        data["_"] = lambda key, **kwargs: get_string(key, lang).format(**kwargs)

        return await handler(event, data)
