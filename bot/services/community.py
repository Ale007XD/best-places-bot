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
  place:closed:{pid}  — hash uid → вес жалобы «закрыто» (1; у админа = порог), TTL CLOSED_TTL_DAYS
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


class ClosedResult(NamedTuple):
    total: int  # сумма весов жалоб
    hidden: bool  # набран порог: место скрывается из выдачи
    first: bool  # первая жалоба этого пользователя на это место
    rewarded: bool


async def report_closed(r, uid: int, pid: str, weight: int = 1) -> ClosedResult:
    """Жалоба «место закрыто». Одна на пользователя и место; TTL отсчитывается от последней новой."""
    key = f"place:closed:{pid}"
    first = await r.hget(key, str(uid)) is None
    if first:
        async with r.pipeline(transaction=True) as pipe:
            pipe.hset(key, str(uid), max(int(weight), 1))
            pipe.expire(key, settings.CLOSED_TTL_DAYS * _DAY)
            await pipe.execute()
    total = sum(int(v) for v in (await r.hvals(key)))
    rewarded = await _maybe_reward(r, uid) if first else False
    return ClosedResult(total, total >= settings.CLOSED_REPORTS_THRESHOLD, first, rewarded)


async def closed_ids(r, pids: list[str | None]) -> set[str]:
    """place_id из списка, набравшие порог жалоб «закрыто»."""
    ids = [p for p in dict.fromkeys(pids) if p]
    if not ids:
        return set()
    async with r.pipeline(transaction=False) as pipe:
        for pid in ids:
            pipe.hvals(f"place:closed:{pid}")
        rows = await pipe.execute()
    need = settings.CLOSED_REPORTS_THRESHOLD
    return {pid for pid, vals in zip(ids, rows, strict=True) if sum(int(v) for v in vals) >= need}


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


# Данные мест сохраняем только из открытых источников (OSM/Geoapify). Foursquare (и аварийные
# Mapbox) запрещают хранить свои ответы — у них остаётся лишь id для отметок.
_STORABLE_PREFIXES = ("osm:", "gp:")


def is_storable_place_id(pid: str | None) -> bool:
    return bool(pid) and str(pid).startswith(_STORABLE_PREFIXES)


async def purge_foreign_meta(r, apply: bool = False) -> tuple[int, int]:
    """Находит (и при apply=True удаляет) place:meta:* мест не из OSM/Geoapify.

    Возвращает (всего записей, из них чужих)."""
    total = foreign = 0
    async for key in r.scan_iter("place:meta:*", count=500):
        total += 1
        pid = key.split("place:meta:", 1)[1] if isinstance(key, str) else key.decode()[11:]
        if is_storable_place_id(pid):
            continue
        foreign += 1
        if apply:
            await r.delete(key)
    return total, foreign


async def remember_places(r, places: list[dict[str, Any]]) -> None:
    """Сохраняет данные показанных мест (только OSM/Geoapify) — для топов района по голосам."""
    async with r.pipeline(transaction=False) as pipe:
        n = 0
        for p in places:
            pid = p.get("place_id")
            if not is_storable_place_id(pid):
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
