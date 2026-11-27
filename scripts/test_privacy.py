"""Тесты /deleteme (удаление данных), лимита и админ-инструментов жалоб «закрыто», необязательного FSQ_API_KEY."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis as fakeredis

from bot.config import settings
from bot.handlers import account_handlers as acc
from bot.handlers import community_handlers as ch
from bot.services import community, privacy, quota
from bot.utils import places_service as ps

UID, OTHER = 111, 222


async def seed(r) -> None:
    """Данные двух пользователей: проверяем, что чужое не трогается."""
    today = quota.local_today()
    for u in (UID, OTHER):
        await r.hset(f"user_prefs:{u}", mapping={"radius": 200})
        await r.set(f"user_lastloc:{u}", "10.7,106.7")
        await r.set(f"user_lang:{u}", "ru")
        await r.set(f"user_votes:{u}", 3)
        await r.set(f"vote_rewards:{u}:{today.isoformat()}", 1)
        await r.set(f"user_credits:{u}", 5)
        await r.set(f"pro_until:{u}", "2030-01-01")
        await r.hset(f"streak:{u}", mapping={"count": 4})
        await r.set(f"referrer:{u}", 999)
        await r.set(f"first_search:{u}", 1)
        await r.set(f"ref_count:{u}", 2)
        await r.set(f"quota:used:{u}:{today.isoformat()}", 1)
        await r.sadd(f"stats:users:daily:{today.isoformat()}", u)
        await r.sadd(f"stats:users:daily:{(today.replace(day=1)).isoformat()}", u)
    # голоса: у места a оба, у места b только UID
    for pid, voters in (("osm:node/1", {UID: "u", OTHER: "d"}), ("osm:node/2", {UID: "d"})):
        await r.hset(f"place:voters:{pid}", mapping={str(k): v for k, v in voters.items()})
        await r.hset(f"place:votes:{pid}", mapping={"up": sum(v == "u" for v in voters.values()), "down": sum(v == "d" for v in voters.values())})
    # жалобы: место x скрыто двумя (UID и OTHER), место y — только UID
    await community.report_closed(r, UID, "osm:node/10")
    await community.report_closed(r, OTHER, "osm:node/10")
    await community.report_closed(r, UID, "osm:node/11")


async def test_delete() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    await seed(r)
    stats = await privacy.delete_user_data(r, UID)  # уровень «профиль»
    assert stats["votes"] == 2 and stats["reports"] == 2 and stats["stats_days"] >= 1, stats
    for key in ("user_prefs", "user_lastloc", "user_lang", "user_votes"):
        assert await r.exists(f"{key}:{UID}") == 0, key
    assert [k async for k in r.scan_iter(f"vote_rewards:{UID}:*")] == []
    # баланс, Pro, серия, реферальная связь и дневные лимиты остались
    for key in ("user_credits", "pro_until", "streak", "referrer", "first_search", "ref_count"):
        assert await r.exists(f"{key}:{UID}") == 1, key
    assert len([k async for k in r.scan_iter(f"quota:used:{UID}:*")]) == 1
    # голоса удалены, счётчики 👍/👎 скорректированы, чужие голоса целы
    assert await r.hget("place:voters:osm:node/1", str(UID)) is None and await r.hget("place:voters:osm:node/1", str(OTHER)) == "d"
    assert await r.hgetall("place:votes:osm:node/1") == {"up": "0", "down": "1"}
    assert await r.hgetall("place:votes:osm:node/2") == {"up": "0", "down": "0"}
    # жалобы: у места 10 осталась жалоба OTHER (скрытие снято), у места 11 жалоб нет совсем
    assert await community.closed_ids(r, ["osm:node/10", "osm:node/11"]) == set()
    assert await r.exists("place:closed:osm:node/11") == 0 and await r.hget("place:closed:osm:node/10", str(OTHER)) == "1"
    assert [pid for pid, _t, _ts in await community.list_hidden(r)] == []  # индекс скрытых очищен
    # данные другого пользователя нетронуты
    for key in ("user_prefs", "user_lastloc", "user_lang", "user_votes", "user_credits", "pro_until", "streak", "referrer"):
        assert await r.exists(f"{key}:{OTHER}") == 1, key
    assert await r.sismember(f"stats:users:daily:{quota.local_today().isoformat()}", OTHER)
    assert not await r.sismember(f"stats:users:daily:{quota.local_today().isoformat()}", UID)

    # уровень «всё»: дополнительно баланс, Pro, серия, реферальная связь
    stats = await privacy.delete_user_data(r, UID, include_balance=True)
    assert stats["balance"] == 6, stats
    for key in ("user_credits", "pro_until", "streak", "referrer", "first_search", "ref_count"):
        assert await r.exists(f"{key}:{UID}") == 0, key
    assert await r.exists(f"user_credits:{OTHER}") == 1
    assert len([k async for k in r.scan_iter(f"quota:used:{UID}:*")]) == 1  # лимиты не сбрасываются удалением


async def test_deleteme_handlers() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    await seed(r)

    class FakeMessage:
        def __init__(self):
            self.edit_text = AsyncMock()

    real = acc.Message
    acc.Message = FakeMessage  # type: ignore[misc,assignment]
    try:
        # команда: подтверждение с тремя кнопками, ничего не удалено
        msg = SimpleNamespace(answer=AsyncMock())
        await acc.cmd_deleteme(msg, "ru")
        kb = msg.answer.await_args.kwargs["reply_markup"]
        assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["del:profile", "del:all", "del:cancel"]
        assert "платежах" in msg.answer.await_args.args[0] and await r.exists(f"user_prefs:{UID}") == 1

        def callback(data: str):
            return SimpleNamespace(data=data, from_user=SimpleNamespace(id=UID), message=FakeMessage(), answer=AsyncMock())

        state = SimpleNamespace(clear=AsyncMock())
        c = callback("del:cancel")
        await acc.deleteme_confirm(c, state, "ru", r)
        assert state.clear.await_count == 0 and await r.exists(f"user_prefs:{UID}") == 1
        c = callback("del:profile")
        await acc.deleteme_confirm(c, state, "en", r)
        assert state.clear.await_count == 1 and await r.exists(f"user_prefs:{UID}") == 0
        assert "Done" in c.message.edit_text.await_args.args[0] and await r.exists(f"user_credits:{UID}") == 1
        # двойное нажатие не выполняет удаление повторно
        again = callback("del:all")
        await acc.deleteme_confirm(again, state, "en", r)
        assert state.clear.await_count == 1 and await r.exists(f"user_credits:{UID}") == 1
        await r.delete(f"del:lock:{UID}")
        full = callback("del:all")
        await acc.deleteme_confirm(full, state, "ru", r)
        assert await r.exists(f"user_credits:{UID}") == 0 and "баланс" in full.message.edit_text.await_args.args[0]
    finally:
        acc.Message = real  # type: ignore[misc]


async def test_closed_limits() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    settings.CLOSED_DAILY_LIMIT = 3
    settings.ADMIN_ID = 1
    try:
        # лимит: четвёртая новая жалоба за день не записывается; повтор той же жалобы лимит не тратит
        for i in range(3):
            assert not (await community.report_closed(r, UID, f"p{i}")).limited
        res = await community.report_closed(r, UID, "p3")
        assert res.limited and res.total == 0 and await r.exists("place:closed:p3") == 0
        assert not (await community.report_closed(r, UID, "p0")).limited  # уже жаловался — не новая жалоба
        assert not (await community.report_closed(r, OTHER, "p3")).limited  # у другого пользователя свой лимит
        # админ без лимита и скрывает сразу
        for i in range(5):
            res = await community.report_closed(r, 1, f"adm{i}", weight=settings.CLOSED_REPORTS_THRESHOLD)
            assert res.hidden and not res.limited
        # индекс скрытых: только набравшие порог; /closed и /unhide доступны только админу
        hidden = await community.list_hidden(r, 10)
        assert {pid for pid, _t, _ts in hidden} == {f"adm{i}" for i in range(5)}

        def command(uid: int, args: str | None = None):
            return SimpleNamespace(from_user=SimpleNamespace(id=uid), answer=AsyncMock()), SimpleNamespace(args=args)

        m, _c = command(UID)
        await acc.cmd_closed(m, r)
        assert m.answer.await_count == 0  # не админ — молчим
        m, _c = command(1)
        await acc.cmd_closed(m, r)
        assert "adm4" in m.answer.await_args.args[0] and "/unhide" in m.answer.await_args.args[0]
        m, c = command(UID, "adm4")
        await acc.cmd_unhide(m, c, r)
        assert m.answer.await_count == 0 and await community.closed_ids(r, ["adm4"]) == {"adm4"}
        m, c = command(1, "adm4")
        await acc.cmd_unhide(m, c, r)
        assert m.answer.await_args.args[0] == "Скрытие снято." and await community.closed_ids(r, ["adm4"]) == set()
        m, c = command(1, "adm4")
        await acc.cmd_unhide(m, c, r)
        assert "нет" in m.answer.await_args.args[0]
        assert "adm4" not in {pid for pid, _t, _ts in await community.list_hidden(r, 10)}

        # обработчик кнопки: при исчерпанном лимите — предупреждение, жалоба не записывается
        cb = SimpleNamespace(data="cl:zz", from_user=SimpleNamespace(id=UID), message=None, answer=AsyncMock())
        await community.report_closed(r, UID, "z0")  # лимит UID уже исчерпан (p0..p2)
        await ch.closed_report(cb, "ru", r, None)
        assert cb.answer.await_args.kwargs["show_alert"] is True and await r.exists("place:closed:zz") == 0
    finally:
        settings.CLOSED_DAILY_LIMIT, settings.ADMIN_ID = 20, 0


def test_optional_fsq() -> None:
    assert "osm.jp" not in settings.OVERPASS_URL  # зеркало не резолвится с VPS — убрано из значений по умолчанию
    saved = (settings.FSQ_API_KEY, settings.GEOAPIFY_API_KEY, settings.OVERPASS_ENABLED, settings.BASE_PROVIDERS)
    try:
        settings.GEOAPIFY_API_KEY, settings.OVERPASS_ENABLED, settings.BASE_PROVIDERS = "g", True, "geoapify,fsq,overpass"
        settings.FSQ_API_KEY = ""
        assert ps._base_providers() == ["geoapify", "overpass"]  # без ключа Foursquare пропускается
        settings.FSQ_API_KEY = "k"
        assert ps._base_providers() == ["geoapify", "fsq", "overpass"]
    finally:
        settings.FSQ_API_KEY, settings.GEOAPIFY_API_KEY, settings.OVERPASS_ENABLED, settings.BASE_PROVIDERS = saved


def main() -> None:
    asyncio.run(test_delete())
    asyncio.run(test_deleteme_handlers())
    asyncio.run(test_closed_limits())
    test_optional_fsq()
    print("privacy ok")


if __name__ == "__main__":
    main()
