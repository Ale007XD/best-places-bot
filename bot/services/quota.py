# bot/services/quota.py
"""
Квоты, серии (streak), бонусные поиски, Pro и рефералы. Всё хранится в Redis.

Две «валюты» (тизер):
  • поиск С РЕЙТИНГАМИ И ФОТО (Premium-поля Foursquare — стоят денег): дневной лимит
    (free: FREE_SEARCHES_PER_DAY + бонус серии; Pro: PRO_DAILY_LIMIT), затем бонусные поиски
    (user_credits: рефералы, награды серии, паки за Stars);
  • БАЗОВЫЙ поиск (без рейтингов и фото) — доступен всегда, потолок BASE_DAILY_LIMIT в день.
Списывается успешный поиск, сделавший обращение к API (не из кэша).

Ответы из кэша ничего не стоят и не списываются. Пустой результат и ошибки — тоже.

Ключи Redis:
  quota:used:{uid}:{YYYY-MM-DD}  — использовано за день (TTL 2 дня)
  user_credits:{uid}             — баланс бонусных поисков
  pro_until:{uid}                — unix-время окончания Pro
  streak:{uid}                   — hash: days, last, freezes
  referrer:{uid}                 — кто пригласил (ставится один раз)
  first_search:{uid}             — флаг «первый успешный поиск уже был»
  ref_count:{uid}                — сколько наград за рефералов уже выдано
"""

import time
from datetime import UTC, date, datetime, timedelta
from typing import Literal, NamedTuple

from bot.config import settings

# День серии → бонусные поиски в награду
STREAK_MILESTONES = {3: 3, 7: 5, 14: 10, 30: 20}
_DAY = 86400


# Рантайм-настройки (меняются командой /money админа, переживают перезапуск).
CFG_KEY = "cfg:money"


class Config(NamedTuple):
    enabled: bool  # монетизация/лимиты включены
    free_per_day: int  # бесплатных поисков в день (до бонуса серии)
    admin_exempt: bool  # админ без лимитов (выключите, чтобы протестировать paywall)


def _parse_config(vals) -> Config:
    enabled, free, exempt = vals
    return Config(
        enabled=settings.QUOTA_ENABLED if enabled is None else enabled == "1",
        free_per_day=int(free) if free and free.isdigit() else settings.FREE_SEARCHES_PER_DAY,
        admin_exempt=True if exempt is None else exempt == "1",
    )


async def get_config(r) -> Config:
    return _parse_config(await r.hmget(CFG_KEY, "enabled", "free", "exempt"))


async def set_config(
    r, *, enabled: bool | None = None, free: int | None = None, exempt: bool | None = None
) -> Config:
    mapping: dict[str, str] = {}
    if enabled is not None:
        mapping["enabled"] = "1" if enabled else "0"
    if free is not None:
        mapping["free"] = str(max(int(free), 0))
    if exempt is not None:
        mapping["exempt"] = "1" if exempt else "0"
    if mapping:
        await r.hset(CFG_KEY, mapping=mapping)
    return await get_config(r)


def local_today() -> date:
    offset = timedelta(hours=settings.QUOTA_UTC_OFFSET_HOURS)
    return (datetime.now(UTC) + offset).date()


def _used_key(uid: int, d: date) -> str:
    return f"quota:used:{uid}:{d.isoformat()}"


def _base_key(uid: int, d: date) -> str:
    return f"quota:base:{uid}:{d.isoformat()}"


def _streak_view(h: dict, today: date) -> tuple[int, int, int]:
    """(показываемая серия, серия после сегодняшнего поиска, заморозки)."""
    days = int(h.get("days") or 0)
    freezes = int(h.get("freezes") or 0)
    last = h.get("last")
    if last == today.isoformat():
        return days, days, freezes
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    if days and (last == d1 or (last == d2 and freezes > 0)):
        return days, days + 1, freezes
    return 0, 1, freezes


class Status(NamedTuple):
    unlimited: bool  # квоты выключены или это админ
    pro_until: int  # unix, 0 если Pro нет
    limit: int  # дневной лимит
    used: int
    credits: int
    streak_days: int
    streak_bonus: int
    freezes: int
    money_enabled: bool = True
    base_used: int = 0
    base_limit: int = 30

    @property
    def is_pro(self) -> bool:
        return self.pro_until > 0

    @property
    def free_left(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def left_total(self) -> int:
        return self.free_left + self.credits

    @property
    def can_rate(self) -> bool:
        """Можно ли сейчас получить поиск С РЕЙТИНГАМИ и фото (бесплатный тизер/кредиты/Pro)."""
        return self.unlimited or self.left_total > 0

    @property
    def can_search_base(self) -> bool:
        """Базовый поиск (без рейтингов) — почти без ограничений, только потолок на день."""
        return self.unlimited or self.base_used < self.base_limit


async def get_status(r, uid: int) -> Status:
    today = local_today()
    async with r.pipeline(transaction=False) as pipe:
        pipe.get(_used_key(uid, today))
        pipe.get(f"user_credits:{uid}")
        pipe.get(f"pro_until:{uid}")
        pipe.hgetall(f"streak:{uid}")
        pipe.hmget(CFG_KEY, "enabled", "free", "exempt")
        pipe.get(_base_key(uid, today))
        used, credits, pro_until, streak, cfg_vals, base_used = await pipe.execute()

    cfg = _parse_config(cfg_vals)

    pro_ts = int(pro_until or 0)
    is_pro = pro_ts > int(time.time())
    shown, effective, freezes = _streak_view(streak or {}, today)
    bonus = min(max(effective - 1, 0), settings.STREAK_BONUS_CAP)
    limit = settings.PRO_DAILY_LIMIT if is_pro else cfg.free_per_day + bonus
    is_admin = bool(settings.ADMIN_ID) and uid == settings.ADMIN_ID
    unlimited = (not cfg.enabled) or (is_admin and cfg.admin_exempt)
    return Status(
        unlimited=unlimited,
        pro_until=pro_ts if is_pro else 0,
        limit=limit,
        used=int(used or 0),
        credits=max(int(credits or 0), 0),
        streak_days=shown,
        streak_bonus=bonus,
        freezes=freezes,
        money_enabled=cfg.enabled,
        base_used=int(base_used or 0),
        base_limit=settings.BASE_DAILY_LIMIT,
    )


async def consume(r, uid: int, status: Status) -> Literal["free", "credit", "unlimited"] | None:
    """Списывает один поиск. None — не осталось (гонка с параллельным запросом)."""
    if status.unlimited:
        return "unlimited"

    key = _used_key(uid, local_today())
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, 2 * _DAY)
    if n <= status.limit:
        return "free"
    await r.decr(key)

    if await r.decr(f"user_credits:{uid}") >= 0:
        return "credit"
    await r.incr(f"user_credits:{uid}")
    return None


async def consume_base(r, uid: int) -> int:
    """Учитывает базовый поиск (без рейтингов, обращение к API). Возвращает счётчик за день."""
    key = _base_key(uid, local_today())
    n = int(await r.incr(key))
    if n == 1:
        await r.expire(key, 2 * _DAY)
    return n


# --- Серия ---


class StreakUpdate(NamedTuple):
    days: int
    milestone_bonus: int
    freeze_used: bool
    is_new_day: bool


async def touch_streak(r, uid: int) -> StreakUpdate:
    """Засчитывает сегодняшний день в серию. Идемпотентно в пределах дня."""
    today = local_today()
    key = f"streak:{uid}"
    h = await r.hgetall(key)
    shown, effective, freezes = _streak_view(h, today)

    if h.get("last") == today.isoformat():
        return StreakUpdate(effective, 0, False, False)

    two_days_ago = (today - timedelta(days=2)).isoformat()
    freeze_used = bool(shown) and h.get("last") == two_days_ago
    if freeze_used:
        freezes -= 1

    await r.hset(
        key,
        mapping={"days": effective, "last": today.isoformat(), "freezes": max(freezes, 0)},
    )
    bonus = STREAK_MILESTONES.get(effective, 0)
    if bonus:
        await add_credits(r, uid, bonus)
    return StreakUpdate(effective, bonus, freeze_used, True)


# --- Начисления ---


async def add_credits(r, uid: int, n: int) -> int:
    return int(await r.incrby(f"user_credits:{uid}", n))


async def add_freezes(r, uid: int, n: int = 1) -> int:
    return int(await r.hincrby(f"streak:{uid}", "freezes", n))


async def grant_pro(r, uid: int, days: int) -> int:
    """Продлевает Pro (от текущего конца, если он ещё не истёк). Возвращает unix-время."""
    key = f"pro_until:{uid}"
    now = int(time.time())
    current = int(await r.get(key) or 0)
    until = max(current, now) + days * _DAY
    await r.set(key, until)
    return until


# --- Рефералы ---


async def register_referrer(r, uid: int, referrer_id: int) -> bool:
    """Запоминает, кто пригласил. Только для тех, кто ещё ни разу не искал."""
    if referrer_id == uid or await r.exists(f"user_prefs:{uid}"):
        return False
    return bool(await r.set(f"referrer:{uid}", referrer_id, nx=True))


async def reward_referral_if_any(r, uid: int) -> int | None:
    """
    Вызывается после успешного поиска. При ПЕРВОМ успешном поиске приглашённого
    начисляет бонус обоим и возвращает id пригласившего (иначе None).
    Награда — за реальное действие, а не за /start: так сложнее накручивать.
    """
    if not await r.set(f"first_search:{uid}", 1, nx=True):
        return None
    ref = await r.get(f"referrer:{uid}")
    if not ref:
        return None
    ref_id = int(ref)

    if await r.incr(f"ref_count:{ref_id}") > settings.REFERRAL_MAX_REWARDED:
        await r.decr(f"ref_count:{ref_id}")
        return None

    await add_credits(r, uid, settings.REFERRAL_BONUS)
    await add_credits(r, ref_id, settings.REFERRAL_BONUS)
    return ref_id


async def referral_count(r, uid: int) -> int:
    return int(await r.get(f"ref_count:{uid}") or 0)
