from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта вычисляется относительно этого файла — не зависит от CWD.
_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    """
    Класс для хранения и валидации переменных окружения.
    Использует pydantic для надёжной работы с конфигурацией.
    """
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding='utf-8')

    # Обязательные переменные, которые должны быть в .env
    BOT_TOKEN: str
    FSQ_API_KEY: str       # Foursquare Places API key (было GOOGLE_MAPS_API_KEY)
    MAPBOX_TOKEN: str # Добавляем ключ для Mapbox
    VIETMAP_API_KEY: str # VietMap
    ADMIN_ID: int          # Telegram user_id для получения фидбэка


# Единый экземпляр настроек для всего приложения.
settings = Settings()
