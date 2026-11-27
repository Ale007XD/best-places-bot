"""Удаление данных пользователя по его просьбе (/deleteme).

Два уровня:
  • profile — настройки, последняя геопозиция, язык, отметки 👍/👎 и жалобы «закрыто», присутствие в
    дневной статистике. Баланс поисков, Pro, серия, реферальная связь и платежи остаются.
  • all — то же и дополнительно баланс бонусных поисков, Pro, серия и реферальная связь
    (необратимо; купленное сгорает).
Не удаляется в обоих случаях: идемпотентные записи платежей (учёт и возвраты), дневные счётчики
лимитов (кончаются сами за сутки; иначе «удалить и начать заново» сбрасывало бы лимиты) и сообщения
/feedback — они пересылаются администратору в Telegram и лежат в его переписке, а не у нас.
"""

from datetime import timedelta

from bot.services import quota
from bot.utils.analytics import STATS_TTL

PROFILE_KEYS = ("user_prefs:{u}", "user_lastloc:{u}", "user_lang:{u}", "user_votes:{u}")
BALANCE_KEYS = ("user_credits:{u}", "pro_until:{u}", "streak:{u}", "referrer:{u}", "first_search:{u}", "ref_count:{u}")
_SCAN = 500


async def _drop_votes(r, uid: int) -> int:
    """Убирает голоса пользователя у всех мест и корректирует счётчики 👍/👎."""
    n = 0
    async for key in r.scan_iter("place:voters:*", count=_SCAN):
        vote = await r.hget(key, str(uid))
        if vote is None:
            continue
        pid = key.split("place:voters:", 1)[1] if isinstance(key, str) else key.decode()[len("place:voters:") :]
        await r.hdel(key, str(uid))
        field = "up" if vote in ("u", b"u") else "down"
        counters = f"place:votes:{pid}"
        if int(await r.hget(counters, field) or 0) > 0:
            await r.hincrby(counters, field, -1)
        n += 1
    return n


async def _drop_closed_reports(r, uid: int) -> int:
    n = 0
    async for key in r.scan_iter("place:closed:*", count=_SCAN):
        if await r.hdel(key, str(uid)):
            n += 1
            if not await r.exists(key):  # жалоб не осталось — убираем и из индекса скрытых
                pid = key.split("place:closed:", 1)[1] if isinstance(key, str) else key.decode()[len("place:closed:") :]
                await r.zrem("closed:hidden", pid)
    return n


async def delete_user_data(r, uid: int, include_balance: bool = False) -> dict[str, int]:
    """Удаляет данные пользователя; возвращает сколько записей затронуто (для ответа и тестов)."""
    stats = {"keys": 0, "votes": 0, "reports": 0, "stats_days": 0, "balance": 0}
    keys = [k.format(u=uid) for k in PROFILE_KEYS]
    async for key in r.scan_iter(f"vote_rewards:{uid}:*", count=_SCAN):
        keys.append(key)
    stats["keys"] = int(await r.delete(*keys))
    stats["votes"] = await _drop_votes(r, uid)
    stats["reports"] = await _drop_closed_reports(r, uid)
    today = quota.local_today()
    days = (STATS_TTL // 86400) + 1
    for i in range(-1, days + 1):  # статистика живёт 90 суток; ±1 день — на разницу UTC и местной даты
        stats["stats_days"] += int(await r.srem(f"stats:users:daily:{(today - timedelta(days=i)).isoformat()}", uid))
    if include_balance:
        stats["balance"] = int(await r.delete(*[k.format(u=uid) for k in BALANCE_KEYS]))
    return stats
