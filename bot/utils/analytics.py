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

    async def track_purchase(self, product: str, stars: int):
        """Покупка за Stars: счётчик по товару + сумма Stars за день."""
        today = self._get_today_str()
        key = f"stats:features:purchase:{today}"
        await self.r.hincrby(key, product, 1)
        await self.r.expire(key, STATS_TTL)
        rkey = f"stats:stars:daily:{today}"
        await self.r.incrby(rkey, stars)
        await self.r.expire(rkey, STATS_TTL)

    async def track_search_quality(
        self,
        *,
        n_results: int,
        rating_available: bool,
        relaxed: bool,
        from_cache: bool,
        fsq_ok: bool,
        used_osm: bool,
        latency_ms: int,
        rated: bool = False,
    ):
        """Метрики качества: полнота выдачи, доля мест без рейтинга, причины фолбэка, задержка."""
        key = f"stats:quality:{self._get_today_str()}"
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.hincrby(key, "searches", 1)
            pipe.hincrby(key, "latency_ms_sum", latency_ms)
            if n_results >= 3:
                pipe.hincrby(key, "results3", 1)
            if not rating_available:
                pipe.hincrby(key, "no_rating", 1)
            if relaxed:
                pipe.hincrby(key, "relaxed", 1)
            if rated:
                pipe.hincrby(key, "rated", 1)
            if from_cache:
                pipe.hincrby(key, "cache_hits", 1)
            else:
                pipe.hincrby(key, "api_searches", 1)  # прокси стоимости: поиски с обращением к API
            if not fsq_ok:
                pipe.hincrby(key, "fsq_failed", 1)
            if used_osm:
                pipe.hincrby(key, "osm_used", 1)
            pipe.expire(key, STATS_TTL)
            await pipe.execute()

    async def track_search_failure(self):
        key = f"stats:quality:{self._get_today_str()}"
        await self.r.hincrby(key, "unavailable", 1)
        await self.r.expire(key, STATS_TTL)

    async def get_quality_stats(self) -> dict[str, str]:
        return await self.r.hgetall(f"stats:quality:{self._get_today_str()}")

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
        pipe.hgetall(f"stats:features:purchase:{today}")
        pipe.get(f"stats:stars:daily:{today}")
        pipe.hgetall(f"stats:features:paywall:{today}")

        results = await pipe.execute()

        return {
            "active_users": results[0],
            "searches": int(results[1] or 0),
            "empty_results": int(results[2] or 0),
            "feedback": int(results[3] or 0),
            "radius_usage": results[4],
            "rating_usage": results[5],
            "purchases": results[6],
            "stars": int(results[7] or 0),
            "paywall": results[8],
        }
