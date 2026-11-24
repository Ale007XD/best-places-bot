"""Профиль бота в Telegram: меню команд, описание и короткое описание — по языкам.

Источник правды — bot/data/bot_profile.json. Применяется при каждом старте бота, поэтому менять
меню и описания можно правкой файла и деплоем — без ручной работы в @BotFather. (Подсказка поля
ввода inline и режим inline по-прежнему задаются только в @BotFather: у Bot API для них нет метода.)
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "bot_profile.json"
_COMMAND = re.compile(r"^[a-z0-9_]{1,32}$")
_LANG = re.compile(r"^[a-z]{2}$")


def validate(data: Any) -> list[str]:
    """Ограничения Bot API: команда [a-z0-9_] ≤ 32, её описание 3..256, описание ≤ 512, короткое ≤ 120."""
    if not isinstance(data, dict) or data.get("version") != 1:
        return ["корень: нужен объект с version=1"]
    langs = data.get("languages")
    default = data.get("default_language")
    if not isinstance(langs, dict) or default not in (langs or {}):
        return ["languages: нужен объект, где есть default_language"]
    errs: list[str] = []
    for lang, p in langs.items():
        tag = f"язык {lang!r}"
        if not _LANG.match(lang):
            errs.append(f"{tag}: код — две строчные буквы")
        cmds = p.get("commands")
        if not isinstance(cmds, list) or not 1 <= len(cmds) <= 100:
            errs.append(f"{tag}: commands — от 1 до 100 команд")
            cmds = []
        names = [c.get("command") for c in cmds if isinstance(c, dict)]
        if len(set(names)) != len(names):
            errs.append(f"{tag}: команды повторяются")
        for c in cmds:
            if not (isinstance(c, dict) and _COMMAND.match(str(c.get("command", "")))):
                errs.append(f"{tag}: имя команды {c!r} — [a-z0-9_], до 32 символов")
            elif not 3 <= len(str(c.get("description", ""))) <= 256:
                errs.append(f"{tag}: /{c['command']} — описание 3..256 символов")
        if not 1 <= len(str(p.get("description", ""))) <= 512:
            errs.append(f"{tag}: description — 1..512 символов")
        if not 1 <= len(str(p.get("short_description", ""))) <= 120:
            errs.append(f"{tag}: short_description — 1..120 символов")
    return errs


def load() -> dict[str, Any]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    errs = validate(data)
    if errs:
        raise ValueError("bot_profile.json: " + "; ".join(errs))
    return data


async def apply(bot: Bot) -> list[str]:
    """Отправляет профиль в Telegram. Ошибка одного вызова не останавливает остальные.

    Возвращает список ошибок (пустой — всё применено). Язык по умолчанию задаётся без language_code."""
    try:
        data = load()
    except (OSError, ValueError) as e:
        logger.error("bot profile: cannot load: %s", e)
        return [str(e)]
    errors: list[str] = []
    for lang, p in data["languages"].items():
        code = None if lang == data["default_language"] else lang
        calls = (
            ("commands", bot.set_my_commands([BotCommand(**c) for c in p["commands"]], language_code=code)),
            ("description", bot.set_my_description(p["description"], language_code=code)),
            ("short_description", bot.set_my_short_description(p["short_description"], language_code=code)),
        )
        for what, call in calls:
            try:
                await call
            except TelegramAPIError as e:
                errors.append(f"{lang}/{what}: {e}")
                logger.warning("bot profile %s/%s failed: %s", lang, what, e)
    return errors
