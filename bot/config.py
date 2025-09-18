from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    Класс для хранения и валидации переменных окружения.
    Использует pydantic для надежной работы с конфигурацией.
    """
    # Конфигурация указывает, что нужно искать файл .env на один уровень выше
    # текущей директории (т.е. в корне проекта)
    model_config = SettingsConfigDict(env_file='../.env', env_file_encoding='utf-8')

    # Обязательные переменные, которые должны быть в .env
    BOT_TOKEN: str
    GOOGLE_MAPS_API_KEY: str
    ADMIN_ID: int

    # Настройки Redis с значениями по умолчанию
    REDIS_HOST: str = 'localhost'
    REDIS_PORT: int = 6379

# Создаем единый экземпляр настроек для всего приложения.
# Импортируем его в другие модули, чтобы получить доступ к токенам.
settings = Settings()
