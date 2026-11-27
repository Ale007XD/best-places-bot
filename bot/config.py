from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта вычисляется относительно этого файла — не зависит от CWD.
_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    """
    Переменные окружения.

    Для запуска обязателен только BOT_TOKEN. FSQ_API_KEY нужен для рейтингов и запасного провайдера. Остальное — опционально:
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
    FSQ_API_KEY: str = ""  # Foursquare Service Key (Bearer); пусто — без рейтингов и без FSQ-провайдера

    # --- опциональные ---
    FSQ_API_VERSION: str = "2025-06-17"
    MAPBOX_TOKEN: str = ""  # аварийный фолбэк, если Foursquare недоступен
    # Geoapify (данные OSM): базовый слой без рейтингов. Ключ — на geoapify.com (бесплатно
    # 3 000 кредитов/день); без ключа провайдер пропускается.
    GEOAPIFY_API_KEY: str = ""
    GEOAPIFY_URL: str = "https://api.geoapify.com/v2/places"
    # Поиск блюда проверяет НАЗВАНИЯ, а не 3 ближайших: обычный лимит (100) в плотном центре
    # обрезает набор на ~500 м. Здесь лимит выше (макс. 500 у Geoapify); стоит 1 кредит + 1 за
    # каждые 20 мест, т.е. ~16 кредитов на промах кэша при 300. Ключ кэша отдельный.
    GEOAPIFY_DISH_LIMIT: int = 300
    # Порядок провайдеров БАЗОВОГО поиска; один запрос обслуживается ОДНИМ провайдером
    # (первый с >= 3 местами, иначе тот, у кого мест больше) — данные разных источников не
    # смешиваются (лицензии Foursquare и OSM/ODbL). fsq здесь — базовые поля, без кэша.
    BASE_PROVIDERS: str = "geoapify,fsq,overpass"
    # Кэш данных OSM/Geoapify (ODbL это допускает). Ответы Foursquare не хранятся вообще:
    # правила Foursquare запрещают серверный кэш.
    OSM_CACHE_TTL: int = 86400
    # OpenStreetMap (Overpass): бесплатный фолбэк без ключа, места без рейтинга
    OVERPASS_ENABLED: bool = True
    # Один или несколько адресов через запятую; пробуются по порядку. Список — из вики OSM:
    # основной сервер, VK Maps (глобальный), private.coffee (бывший kumi.systems), Япония
    # (тестовый, ближе к Азии; может отключаться). Свои — через OVERPASS_URL в .env.
    OVERPASS_URL: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter"
    )
    ADMIN_ID: int = 0  # Telegram user_id: /feedback и /stats (0 = выключено)
    REDIS_URL: str = "redis://redis:6379/0"

    # --- монетизация / геймификация ---
    # Стартовое значение; дальше админ переключает командой /money on|off (хранится в Redis).
    # По умолчанию ВЫКЛ: после обновления никто не упрётся в paywall, пока вы сами не включите.
    QUOTA_ENABLED: bool = False
    # Тизер: сколько поисков С РЕЙТИНГАМИ И ФОТО в день бесплатно (дальше — базовая выдача без
    # них; расширенные поля Foursquare платные). Pro / бонусные поиски открывают рейтинги.
    FREE_SEARCHES_PER_DAY: int = 1
    BASE_DAILY_LIMIT: int = 30  # потолок базовых (без рейтингов) поисков в день — защита инфраструктуры
    PHOTOS_ENABLED: bool = True
    # Вид выдачи: "cards" — заголовок + карточки-фото (по одному сообщению на место),
    # "compact" — одно сообщение со сворачиваемыми цитатами. Админ переключает: /layout.
    RESULTS_LAYOUT: str = "cards"
    PRO_DAILY_LIMIT: int = 100  # fair-use потолок поисков с рейтингами для Pro
    STREAK_BONUS_CAP: int = 2  # макс. прибавка к дневному лимиту поисков с рейтингами за серию дней
    QUOTA_UTC_OFFSET_HOURS: int = 7  # когда «наступает» новый день (7 = Вьетнам)
    REFERRAL_BONUS: int = 5  # бонусных поисков обоим за приглашённого
    REFERRAL_MAX_REWARDED: int = 20  # потолок наград за рефералы на одного пользователя
    VOTE_REWARD_EVERY: int = 3  # каждые N новых отметок «был здесь» = +1 бонусный поиск (0 = выкл.)
    VOTE_REWARD_DAILY_CAP: int = 3  # максимум таких наград в день
    CLOSED_REPORTS_THRESHOLD: int = 2  # жалоб «закрыто» от разных людей, после которых место скрывается (админ — сразу)
    CLOSED_DAILY_LIMIT: int = 20  # жалоб «закрыто» от одного пользователя в день (админ — без лимита)
    CLOSED_TTL_DAYS: int = 60  # сколько дней действует скрытие
    SITE_URL: str = ""  # сайт проекта (GitHub Pages), без «/» в конце; пусто — ссылки не добавляются
    CHANNEL_ID: str = ""  # канал для объявлений: @username или -100…; бот — админ канала с правом публикации
    SUPPORT_URL: str = ""  # куда направлять вопросы по оплате (аккаунт поддержки / чат), необязательно
    SPECIALTIES_SHOW_UNVERIFIED: bool = False  # показывать непроверенные блюда (только для проверки словаря)
    STARS_PRICE_PACK20: int = 50
    STARS_PRICE_PRO_WEEK: int = 100
    STARS_PRICE_PRO_MONTH: int = 300
    STARS_PRICE_FREEZE: int = 15

    # Поиск по адресу/названию (OpenStreetMap Nominatim, текст запроса уходит на их сервер)
    GEOCODER_ENABLED: bool = True
    GEOCODER_URL: str = "https://nominatim.openstreetmap.org/search"

    # Inline: сколько ближайших мест, радиус по умолчанию/потолок и срок жизни «последней
    # точки» (inline-запрос не всегда несёт геопозицию — тогда берём присланную боту).
    INLINE_RESULTS: int = 3
    INLINE_DEFAULT_RADIUS: int = 200
    INLINE_MAX_RADIUS: int = 1000
    LASTLOC_TTL_HOURS: int = 6

    # Дефолты экрана параметров (если у пользователя нет сохранённых)
    DEFAULT_RADIUS: int = 200
    DEFAULT_MIN_RATING: float = 4.0


# Единый экземпляр настроек для всего приложения.
settings = Settings()  # type: ignore[call-arg]  # обязательные поля приходят из окружения
