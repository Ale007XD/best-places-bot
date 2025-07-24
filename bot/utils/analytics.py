# Файл: bot/utils/analytics.py
import redis.asyncio as redis
from datetime import date

class Analytics:
    def __init__(self, host='redis', port=6379):
        # Подключаемся к Redis, используя имя сервиса из docker-compose
        self.r = redis.Redis(host=host, port=port, decode_responses=True)

    def _get_today_str(self) -> str:
        """Возвращает сегодняшнюю дату в формате ГГГГ-ММ-ДД."""
        return date.today().isoformat()

    async def track_user(self, user_id: int):
        """Отмечает уникального пользователя за сегодняшний день."""
        # Используем SADD для добавления в 'множество' - дубликаты игнорируются
        await self.r.sadd(f"stats:users:daily:{self._get_today_str()}", user_id)

    async def track_search_request(self):
        """Увеличивает счетчик успешных поисков за день."""
        await self.r.incr(f"stats:searches:daily:{self._get_today_str()}")

    async def track_empty_result(self):
        """Увеличивает счетчик 'пустых' результатов за день."""
        await self.r.incr(f"stats:empty_results:daily:{self._get_today_str()}")
        
    async def track_share_button_click(self):
        """Увеличивает счетчик нажатий на кнопку 'Поделиться'."""
        await self.r.incr(f"stats:shares:daily:{self._get_today_str()}")

    async def track_feedback_request(self):
        """Увеличивает счетчик запросов обратной связи."""
        await self.r.incr(f"stats:feedback:daily:{self._get_today_str()}")

    async def track_feature_use(self, feature: str, value: any):
        """Отслеживает использование конкретной фичи (например, радиуса)."""
        # HINCRBY увеличивает значение поля в хэше
        await self.r.hincrby(f"stats:features:{feature}:{self._get_today_str()}", str(value), 1)

    async def get_today_stats(self) -> dict:
        """Собирает всю статистику за сегодня."""
        today = self._get_today_str()
        
        # Используем pipeline для выполнения нескольких команд за один запрос
        pipe = self.r.pipeline()
        pipe.scard(f"stats:users:daily:{today}")         # Уникальные юзеры
        pipe.get(f"stats:searches:daily:{today}")        # Поисковые запросы
        pipe.get(f"stats:empty_results:daily:{today}")   # Пустые результаты
        # pipe.get(f"stats:shares:daily:{today}")          # Нажатия "Поделиться"
        pipe.get(f"stats:feedback:daily:{today}")        # Запросы фидбэка
        pipe.hgetall(f"stats:features:radius:{today}")   # Использование радиусов
        pipe.hgetall(f"stats:features:rating:{today}")   # Использование рейтинга
        
        results = await pipe.execute()
        
        # Приводим к числовому формату, обрабатывая None
        return {
            "active_users": results[0],
            "searches": int(results[1] or 0),
            "empty_results": int(results[2] or 0),
            "shares": int(results[3] or 0),
            "feedback": int(results[4] or 0),
            "radius_usage": results[5],
            "rating_usage": results[6],
        }
