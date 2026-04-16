# bot/utils/analytics.py
import redis.asyncio as redis
from datetime import date


class Analytics:
    def __init__(self, redis_conn: redis.Redis):
        # Переиспользуем соединение из main.py — нет дублирующего connection pool
        self.r = redis_conn

    def _get_today_str(self) -> str:
        """Возвращает сегодняшнюю дату в формате ГГГГ-ММ-ДД."""
        return date.today().isoformat()

    async def track_user(self, user_id: int):
        """Отмечает уникального пользователя за сегодняшний день."""
        await self.r.sadd(f"stats:users:daily:{self._get_today_str()}", user_id)

    async def track_search_request(self):
        """Увеличивает счётчик успешных поисков за день."""
        await self.r.incr(f"stats:searches:daily:{self._get_today_str()}")

    async def track_empty_result(self):
        """Увеличивает счётчик «пустых» результатов за день."""
        await self.r.incr(f"stats:empty_results:daily:{self._get_today_str()}")

    async def track_share_button_click(self):
        """Увеличивает счётчик нажатий на кнопку «Поделиться»."""
        await self.r.incr(f"stats:shares:daily:{self._get_today_str()}")

    async def track_feedback_request(self):
        """Увеличивает счётчик запросов обратной связи."""
        await self.r.incr(f"stats:feedback:daily:{self._get_today_str()}")

    async def track_feature_use(self, feature: str, value):
        """Отслеживает использование конкретной фичи (например, радиуса)."""
        await self.r.hincrby(f"stats:features:{feature}:{self._get_today_str()}", str(value), 1)

    async def get_today_stats(self) -> dict:
        """Собирает всю статистику за сегодня одним pipeline."""
        today = self._get_today_str()

        pipe = self.r.pipeline()
        pipe.scard(f"stats:users:daily:{today}")
        pipe.get(f"stats:searches:daily:{today}")
        pipe.get(f"stats:empty_results:daily:{today}")
        pipe.get(f"stats:feedback:daily:{today}")
        pipe.hgetall(f"stats:features:radius:{today}")
        pipe.hgetall(f"stats:features:rating:{today}")

        results = await pipe.execute()

        return {
            "active_users": results[0],
            "searches": int(results[1] or 0),
            "empty_results": int(results[2] or 0),
            "feedback": int(results[3] or 0),
            "radius_usage": results[4],
            "rating_usage": results[5],
        }

