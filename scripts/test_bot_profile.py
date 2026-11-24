"""Тесты профиля бота (bot/data/bot_profile.json → Bot API) и ссылок на сайт в /terms, /paysupport."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramAPIError

from bot.config import settings
from bot.handlers import account_handlers as acc
from bot.services import bot_profile as bp
from bot.utils import links


class FakeBot:
    """Записывает вызовы Bot API; fail — какие методы должны падать."""

    def __init__(self, fail: tuple[str, ...] = ()):
        self.calls: list[tuple[str, object, str | None]] = []
        self.fail = fail

    async def _call(self, name: str, payload, language_code):
        self.calls.append((name, payload, language_code))
        if name in self.fail:
            raise TelegramAPIError(method=None, message="boom")  # type: ignore[arg-type]
        return True

    async def set_my_commands(self, commands, language_code=None):
        return await self._call("commands", commands, language_code)

    async def set_my_description(self, description, language_code=None):
        return await self._call("description", description, language_code)

    async def set_my_short_description(self, short_description, language_code=None):
        return await self._call("short_description", short_description, language_code)


async def main() -> None:
    data = bp.load()
    assert bp.validate(data) == [] and data["default_language"] == "en"
    assert {"start", "terms", "paysupport"} <= {c["command"] for c in data["languages"]["ru"]["commands"]}
    # Telegram Stars требует /paysupport, меню должно его содержать во всех языках
    assert all("paysupport" in [c["command"] for c in p["commands"]] for p in data["languages"].values())

    # 1. валидатор ловит нарушения лимитов Bot API
    def errs(mutate) -> list[str]:
        bad = copy.deepcopy(data)
        mutate(bad)
        return bp.validate(bad)

    assert any("512" in e for e in errs(lambda d: d["languages"]["en"].update(description="x" * 513)))
    assert any("120" in e for e in errs(lambda d: d["languages"]["en"].update(short_description="x" * 121)))
    assert any("имя команды" in e for e in errs(lambda d: d["languages"]["en"]["commands"][0].update(command="Start")))
    assert any("повторяются" in e for e in errs(lambda d: d["languages"]["en"]["commands"].append(dict(d["languages"]["en"]["commands"][0]))))
    assert any("3..256" in e for e in errs(lambda d: d["languages"]["en"]["commands"][0].update(description="a")))
    assert any("default_language" in e for e in errs(lambda d: d.update(default_language="de")))
    assert any("две строчные" in e for e in errs(lambda d: d["languages"].update({"EN-us": d["languages"]["en"]})))

    # 2. apply: все языки и методы; язык по умолчанию — без language_code
    bot = FakeBot()
    assert await bp.apply(bot) == []
    assert len(bot.calls) == 9
    by_code = {(name, code) for name, _p, code in bot.calls}
    assert {("commands", None), ("description", None), ("commands", "ru"), ("short_description", "zh")} <= by_code
    ru_cmds = next(p for n, p, c in bot.calls if n == "commands" and c == "ru")
    assert ru_cmds[0].command == "start" and ru_cmds[0].description == "Найти места рядом"

    # 3. сбой одного вызова не останавливает остальные и попадает в список ошибок
    bot = FakeBot(fail=("description",))
    result = await bp.apply(bot)
    assert len(bot.calls) == 9 and len(result) == 3 and all("description" in r for r in result), result

    # 4. /terms и /paysupport: ссылки на сайт на языке пользователя (zh → en); без SITE_URL — как раньше
    settings.SITE_URL, settings.SUPPORT_URL = "https://u.github.io/repo/", "https://t.me/support"
    try:
        for lang, folder in (("ru", "ru"), ("en", "en"), ("zh", "en")):
            msg = SimpleNamespace(answer=AsyncMock())
            await acc.cmd_terms(msg, lang)
            text = msg.answer.await_args.args[0]
            assert f"https://u.github.io/repo/{folder}/terms.html" in text, text
            assert f"https://u.github.io/repo/{folder}/privacy.html" in text
            assert msg.answer.await_args.kwargs["disable_web_page_preview"] is True
        msg = SimpleNamespace(answer=AsyncMock())
        await acc.cmd_paysupport(msg, "ru")
        text = msg.answer.await_args.args[0]
        assert "https://t.me/support" in text and "https://u.github.io/repo/ru/terms.html" in text
        assert links.site_url("ru", "privacy") == "https://u.github.io/repo/ru/privacy.html"
        settings.SITE_URL = settings.SUPPORT_URL = ""
        msg = SimpleNamespace(answer=AsyncMock())
        await acc.cmd_terms(msg, "en")
        assert "http" not in msg.answer.await_args.args[0] and links.site_url("en", "terms") == ""
        msg = SimpleNamespace(answer=AsyncMock())
        await acc.cmd_paysupport(msg, "en")
        assert "http" not in msg.answer.await_args.args[0]
    finally:
        settings.SITE_URL = settings.SUPPORT_URL = ""

    print("bot_profile ok")


if __name__ == "__main__":
    asyncio.run(main())
