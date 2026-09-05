# bot/utils/analytics.py
from datetime import datetime, timezone

import redis.asyncio as redis


class Analytics:
    def __init__(self, redis_conn: redis.Redis):
        self.r = redis_conn

    def _get_today_str(self) -> str:
        """Returns today's date (UTC) in format YYYY-MM-DD."""
        return datetime.now(tz=timezone.utc).date().isoformat()

    async def track_user(self, user_id: int):
        """Tracks user_id in a daily sorted set."""
        await self.r.sadd(f"stats:users:daily:{self._get_today_str()}", user_id)

    async def track_search_request(self):
        """Tracks number of searches in a daily counter."""
        await self.r.incr(f"stats:searches:daily:{self._get_today_str()}")

    async def track_empty_result(self):
        """Tracks number of empty results in a daily counter."""
        await self.r.incr(f"stats:empty_results:daily:{self._get_today_str()}")

    async def track_share_button_click(self):
        """Tracks share button clicks in a daily counter."""
        await self.r.incr(f"stats:shares:daily:{self._get_today_str()}")

    async def track_feedback_request(self):
        """Tracks feedback requests in a daily counter."""
        await self.r.incr(f"stats:feedback:daily:{self._get_today_str()}")

    async def track_feature_use(self, feature: str, value):
        """Tracks feature usage in a daily hash."""
        await self.r.hincrby(
            f"stats:features:{feature}:{self._get_today_str()}", str(value), 1
        )

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
