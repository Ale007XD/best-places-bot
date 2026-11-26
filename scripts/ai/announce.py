"""Объявления о релизах: черновик RU+EN от модели → подтверждение админом в Telegram (по умолчанию)
или сразу публикация в канал (режим auto).

    python -m scripts.ai.announce --tag v1.2.0 [--mode approval|auto] [--dry-run]

Окружение: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_ID, CHANNEL_ID (для auto), TAG, RELEASE_NOTES.
Режим approval: черновик приходит админу в личку бота с кнопками «✅ Опубликовать» / «❌ Отклонить»
(обработчик — bot/handlers/account_handlers.py, публикует ровно показанный текст). Режим auto
публикует в канал и присылает админу копию. В промпт попадают только заметки релиза и темы
коммитов — данных пользователей нет.
"""

import argparse
import html
import os
import re
import subprocess
import sys
from typing import Any

import httpx

from scripts.ai.llm import Client, LLMError, QuotaExhausted

MAX_PART = 900  # знаков на язык; в сумме с разделителем укладывается в лимит Telegram (4096)
ALLOWED_HOSTS = ("t.me", "github.com", "github.io")
_TAGS = re.compile(r"</?([a-zA-Z0-9]+)(?:\s[^>]*)?>")
_SECRET = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{20,}|api[_-]?key", re.IGNORECASE)
_HREF = re.compile(r'<a href="([^"]+)">')
_IGNORED_SUBJECTS = re.compile(r"^(merge|fix(\s|:)|docs|chore|ci|test|refactor|revert|build_|git push)", re.IGNORECASE)

SYSTEM = (
    "Ты редактор Telegram-канала бота, который подсказывает, где поесть рядом. Пишешь короткие объявления "
    "о новом для обычных пользователей: простым языком, через пользу и решённую боль (например, «больше "
    "не придёте к закрытому месту»), без технических терминов, названий библиотек, провайдеров данных, "
    "номеров коммитов и версий. Ничего не выдумывай: только то, что есть во входных данных."
)


def commit_subjects(tag: str, prev: str | None = None) -> list[str]:
    """Темы коммитов между предыдущим тегом и tag (пусто, если git недоступен)."""
    try:
        base = prev or subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", f"{tag}^"], capture_output=True, text=True, check=True
        ).stdout.strip()
        out = subprocess.run(
            ["git", "log", "--pretty=%s", f"{base}..{tag}"], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [s for s in out.splitlines() if s.strip() and not _IGNORED_SUBJECTS.match(s.strip())]


def build_messages(notes: str, subjects: list[str], tag: str) -> list[dict[str, str]]:
    material = f"Заметки релиза:\n{notes.strip() or '(пусто)'}\n\nТемы изменений:\n" + "\n".join(f"- {s}" for s in subjects[:40])
    user = (
        f"Релиз {tag}. Материал:\n{material}\n\nСоставь объявление на русском и на английском для подписчиков "
        f"канала. Требования: каждое до {MAX_PART} знаков; 2–5 коротких пунктов или абзацев; допустимо только "
        "форматирование <b>, <i> и <a href=\"https://...\">; эмодзи умеренно. Если для обычных пользователей "
        'ничего нового нет, верни {"ru": "", "en": ""}.\n'
        'Верни ТОЛЬКО JSON: {"ru": "...", "en": "..."}'
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def validate_part(text: str, lang: str) -> list[str]:
    errs: list[str] = []
    if len(text) > MAX_PART:
        errs.append(f"{lang}: длиннее {MAX_PART} знаков")
    for name in _TAGS.findall(text):
        if name.lower() not in ("b", "i", "a"):
            errs.append(f"{lang}: недопустимый тег <{name}>")
    for href in _HREF.findall(text):
        host = re.sub(r"^https://([^/:?#]+).*$", r"\1", href)
        if not href.startswith("https://") or not host.endswith(ALLOWED_HOSTS):
            errs.append(f"{lang}: ссылка на неразрешённый адрес {href[:60]}")
    if len(_HREF.findall(text)) != len(re.findall(r"<a\b", text)):
        errs.append(f"{lang}: у <a> нет корректного href")
    if _SECRET.search(text):
        errs.append(f"{lang}: похоже на секрет или ключ")
    # нет «висящих» < и & (иначе Telegram отклонит разметку)
    stripped = _TAGS.sub("", text)
    if re.search(r"<|&(?!amp;|lt;|gt;|quot;)", stripped):
        errs.append(f"{lang}: неэкранированные < или &")
    return errs


def check(data: Any) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("ru"), str) or not isinstance(data.get("en"), str):
        return ['нужен объект {"ru": "...", "en": "..."}']
    errs = validate_part(data["ru"], "ru") + validate_part(data["en"], "en")
    if bool(data["ru"].strip()) != bool(data["en"].strip()):
        errs.append("заполнен только один язык")
    if data["ru"].strip() and not re.search(r"[а-яё]", data["ru"], re.IGNORECASE):
        errs.append("ru: нет русского текста")
    if data["en"].strip() and re.search(r"[а-яё]", data["en"], re.IGNORECASE):
        errs.append("en: есть русский текст")
    return errs


def compose(data: dict[str, str]) -> str:
    return f"🇷🇺 {data['ru'].strip()}\n\n———\n\n🇬🇧 {data['en'].strip()}"


def telegram(http: httpx.Client, token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = http.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
    body = r.json()
    if r.status_code != 200 or not body.get("ok"):
        raise LLMError(f"Telegram {method}: {body.get('description', r.status_code)}")  # токен в сообщение не попадает
    return body["result"]


def deliver(http: httpx.Client, token: str, text: str, mode: str, admin_id: str, channel_id: str) -> str:
    """approval → черновик админу с кнопками; auto → в канал + копия админу. Возвращает описание действия."""
    if mode == "auto":
        if not channel_id:
            raise LLMError("режим auto требует CHANNEL_ID")
        telegram(http, token, "sendMessage", {"chat_id": channel_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        if admin_id:
            telegram(http, token, "sendMessage", {"chat_id": admin_id, "text": "✅ Объявление опубликовано в канале автоматически:\n\n" + text, "parse_mode": "HTML", "disable_web_page_preview": True})
        return "опубликовано в канал"
    if not admin_id:
        raise LLMError("режим approval требует ADMIN_ID")
    keyboard = {"inline_keyboard": [[{"text": "✅ Опубликовать в канал", "callback_data": "ann:ok"}, {"text": "❌ Отклонить", "callback_data": "ann:no"}]]}
    telegram(http, token, "sendMessage", {"chat_id": admin_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": keyboard})
    return "черновик отправлен админу на подтверждение"


def main(argv: list[str] | None = None, http: httpx.Client | None = None, client: Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", default=os.environ.get("TAG", ""))
    ap.add_argument("--prev", default=None, help="предыдущий тег (по умолчанию — ближайший)")
    ap.add_argument("--mode", choices=["approval", "auto"], default=os.environ.get("ANNOUNCE_MODE", "approval") or "approval")
    ap.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN") == "true", help="только показать текст, ничего не отправлять (или DRY_RUN=true)")
    args = ap.parse_args(argv)
    if not args.tag:
        print("не указан --tag / TAG", file=sys.stderr)
        return 2
    notes = os.environ.get("RELEASE_NOTES", "")
    subjects = commit_subjects(args.tag, args.prev)
    if not notes.strip() and not subjects:
        print("нет заметок релиза и значимых изменений — объявлять нечего")
        return 0
    try:
        llm = client or Client.from_env()
        data = llm.json(build_messages(notes, subjects, args.tag), check)
    except QuotaExhausted as e:
        print(f"::warning::{e}; повторите завтра или задайте режим вручную")
        return 0
    except LLMError as e:
        print(f"::error::{e}")
        return 1
    if not data["ru"].strip():
        print("модель решила, что для пользователей нового нет — объявление не создаётся")
        return 0
    text = compose(data)
    print(text)
    if args.dry_run:
        return 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("::error::нет TELEGRAM_BOT_TOKEN")
        return 1
    try:
        done = deliver(http or httpx.Client(timeout=30), token, text, args.mode, os.environ.get("ADMIN_ID", ""), os.environ.get("CHANNEL_ID", ""))
    except LLMError as e:
        print(f"::error::{html.escape(str(e))}")
        return 1
    print(f"::notice::{done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
