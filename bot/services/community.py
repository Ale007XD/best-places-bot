# bot/services/community.py
"""
Отметки «был здесь» 👍/👎 — собственная база рейтингов.

Нужна, пока у мест нет рейтинга провайдера (нет Premium-кредитов Foursquare, данные OSM):
для места без рейтинга считается «народный» рейтинг со сглаживанием Лапласа,
(up + 1) / (up + down + 2) · 5 — одна отметка почти ничего не решает, нужны несколько.

Ключи Redis:
  place:votes:{pid}   — hash up/down (счётчики)
  place:voters:{pid}  — hash uid → "u"/"d" (один голос на пользователя, его можно изменить)
  place:meta:{pid}    — название/координаты/адрес (для будущих топов района)
  user_votes:{uid}    — сколько мест пользователь отметил впервые
  vote_rewards:{uid}:{день} — выдано наград за отметки сегодня
"""

from typing import Any, NamedTuple

from bot.config import settings
from bot.services import quota

_DAY = 86400


class VoteResult(NamedTuple):
    up: int
    down: int
    changed: bool  # счётчики изменились (первый голос или смена мнения)
    first_vote: bool
    rewarded: bool  # за эту отметку начислен бонусный поиск


def community_rating(up: int, down: int) -> float:
    """Рейтинг 0–5 со сглаживанием Лапласа."""
    return round(5 * (up + 1) / (up + down + 2), 2)


async def attach_votes(r, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Возвращает копии мест с полем votes={"up","down"}. Если у места нет рейтинга
    провайдера, но есть отметки — подставляет «народный» рейтинг (rating_src="community").
    """
    out = [dict(p) for p in places]
    pids = [p.get("place_id") for p in out]
    if not any(pids):
        return out

    async with r.pipeline(transaction=False) as pipe:
        for pid in pids:
            if pid:
                pipe.hgetall(f"place:votes:{pid}")
        rows = iter(await pipe.execute())

    for p, pid in zip(out, pids, strict=True):
        if not pid:
            continue
        h = next(rows) or {}
        up, down = max(int(h.get("up", 0)), 0), max(int(h.get("down", 0)), 0)
        if not (up or down):
            continue
        p["votes"] = {"up": up, "down": down}
        if p.get("rating") is None:
            p["rating"] = community_rating(up, down)
            p["rating_src"] = "community"
            p["user_ratings_total"] = up + down
    return out


async def record_vote(r, uid: int, pid: str, direction: str) -> VoteResult:
    """direction: "up" | "down". Повторный такой же голос ничего не меняет."""
    votes_key, voters_key = f"place:votes:{pid}", f"place:voters:{pid}"
    new = "u" if direction == "up" else "d"
    prev = await r.hget(voters_key, str(uid))

    changed = prev != new
    if changed:
        async with r.pipeline(transaction=True) as pipe:
            pipe.hset(voters_key, str(uid), new)
            if prev:
                pipe.hincrby(votes_key, "up" if prev == "u" else "down", -1)
            pipe.hincrby(votes_key, "up" if new == "u" else "down", 1)
            await pipe.execute()

    h = await r.hgetall(votes_key)
    up, down = max(int(h.get("up", 0)), 0), max(int(h.get("down", 0)), 0)

    first = prev is None
    rewarded = await _maybe_reward(r, uid) if first else False
    return VoteResult(up, down, changed, first, rewarded)


async def _maybe_reward(r, uid: int) -> bool:
    """Каждые VOTE_REWARD_EVERY новых отметок — +1 бонусный поиск (с дневным потолком)."""
    every = settings.VOTE_REWARD_EVERY
    n = await r.incr(f"user_votes:{uid}")
    if every <= 0 or n % every:
        return False
    key = f"vote_rewards:{uid}:{quota.local_today().isoformat()}"
    got = await r.incr(key)
    await r.expire(key, 2 * _DAY)
    if got > settings.VOTE_REWARD_DAILY_CAP:
        return False
    await quota.add_credits(r, uid, 1)
    return True


async def remember_places(r, places: list[dict[str, Any]]) -> None:
    """Сохраняет данные показанных мест — чтобы по голосам потом строить топы района."""
    async with r.pipeline(transaction=False) as pipe:
        n = 0
        for p in places:
            pid = p.get("place_id")
            if not pid:
                continue
            meta = {
                k: str(p[k])
                for k in ("name", "lat", "lon", "vicinity", "primary_type")
                if p.get(k) is not None
            }
            if meta:
                pipe.hset(f"place:meta:{pid}", mapping=meta)
                n += 1
        if n:
            await pipe.execute()
