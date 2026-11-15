from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта вычисляется относительно этого файла — не зависит от CWD.
_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    """
    Переменные окружения.

    Для запуска обязательны только BOT_TOKEN и FSQ_API_KEY. Остальное — опционально:
    пустое значение в .env (`MAPBOX_TOKEN=`) трактуется как «не задано».
    """

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- обязательные ---
    BOT_TOKEN: str
    FSQ_API_KEY: str  # Foursquare Service Key (Bearer)

    # --- опциональные ---
    FSQ_API_VERSION: str = "2025-06-17"
    MAPBOX_TOKEN: str = ""  # аварийный фолбэк, если Foursquare недоступен
    VIETMAP_API_KEY: str = ""  # аварийный фолбэк для Вьетнама
    # OpenStreetMap (Overpass): бесплатный фолбэк без ключа, места без рейтинга
    OVERPASS_ENABLED: bool = True
    OVERPASS_URL: str = "https://overpass-api.de/api/interpreter"
    ADMIN_ID: int = 0  # Telegram user_id: /feedback и /stats (0 = выключено)
    REDIS_URL: str = "redis://redis:6379/0"

    # Дефолты экрана параметров (если у пользователя нет сохранённых)
    DEFAULT_RADIUS: int = 500
    DEFAULT_MIN_RATING: float = 4.0


# Единый экземпляр настроек для всего приложения.
settings = Settings()
