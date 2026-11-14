# bot/utils/analytics.py
from datetime import UTC, datetime

import redis.asyncio as redis

STATS_TTL = 90 * 24 * 3600  # ключи статистики живут 90 дней


class Analytics:
    def __init__(self, redis_conn: redis.Redis):
        self.r = redis_conn

    async def _incr(self, key: str):
        await self.r.incr(key)
        await self.r.expire(key, STATS_TTL)

    def _get_today_str(self) -> str:
        """Returns today's date (UTC) in format YYYY-MM-DD."""
        return datetime.now(tz=UTC).date().isoformat()

    async def track_user(self, user_id: int):
        """Tracks user_id in a daily sorted set."""
        key = f"stats:users:daily:{self._get_today_str()}"
        await self.r.sadd(key, user_id)
        await self.r.expire(key, STATS_TTL)

    async def track_search_request(self):
        """Tracks number of searches in a daily counter."""
        await self._incr(f"stats:searches:daily:{self._get_today_str()}")

    async def track_empty_result(self):
        """Tracks number of empty results in a daily counter."""
        await self._incr(f"stats:empty_results:daily:{self._get_today_str()}")

    async def track_share_button_click(self):
        """Tracks share button clicks in a daily counter."""
        await self._incr(f"stats:shares:daily:{self._get_today_str()}")

    async def track_feedback_request(self):
        """Tracks feedback requests in a daily counter."""
        await self._incr(f"stats:feedback:daily:{self._get_today_str()}")

    async def track_feature_use(self, feature: str, value):
        """Tracks feature usage in a daily hash."""
        key = f"stats:features:{feature}:{self._get_today_str()}"
        await self.r.hincrby(key, str(value), 1)
        await self.r.expire(key, STATS_TTL)

    async def get_today_stats(self) -> dict:
        """Gets today's statistics."""
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
