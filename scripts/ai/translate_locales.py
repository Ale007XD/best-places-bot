"""Перевод недостающих строк локалей бота (bot/locales/*.json) моделью, с жёсткой проверкой.

    python -m scripts.ai.translate_locales --langs vi,th [--write] [--report FILE]
    python -m scripts.ai.translate_locales --existing [--write]     # только уже существующие языки

Исходник — en.json (с контекстом из ru.json). Берутся ключи, которых нет в целевом файле; готовые
переводы не трогаются. Каждый перевод проверяется: те же плейсхолдеры {name}, те же HTML-теги,
не пусто, не длиннее 2.5× оригинала (+20). Новый язык сам по себе в бота не подключается — это
отдельное продуктовое решение (кнопка выбора языка, bot_profile.json).
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.ai.llm import Client, LLMError, QuotaExhausted

LOCALES = Path(__file__).resolve().parent.parent.parent / "bot" / "locales"
SOURCE_LANGS = ("ru", "en")
PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
TAG = re.compile(r"</?[a-z]+(?:\s[^>]*)?>")
BATCH = 25
LANG_NAMES = {"vi": "Vietnamese", "th": "Thai", "zh": "Simplified Chinese", "es": "Spanish", "de": "German", "fr": "French"}


def load(lang: str) -> dict[str, str]:
    path = LOCALES / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def missing_keys(source: dict[str, str], target: dict[str, str]) -> list[str]:
    return [k for k in source if k not in target]


def check_translation(src: str, out: Any) -> list[str]:
    if not isinstance(out, str) or not out.strip():
        return ["пустой перевод"]
    errs = []
    if Counter(PLACEHOLDER.findall(src)) != Counter(PLACEHOLDER.findall(out)):
        errs.append(f"плейсхолдеры: было {sorted(PLACEHOLDER.findall(src))}, стало {sorted(PLACEHOLDER.findall(out))}")
    if Counter(TAG.findall(src)) != Counter(TAG.findall(out)):
        errs.append("HTML-теги не совпадают с оригиналом")
    if len(out) > 2.5 * len(src) + 20:
        errs.append("перевод слишком длинный")
    return errs


def messages(lang: str, batch: dict[str, str], context: dict[str, str]) -> list[dict[str, str]]:
    system = (
        f"You translate UI strings of a Telegram bot that helps people find places to eat nearby into {LANG_NAMES.get(lang, lang)}. "
        "Keep the friendly, short tone. Keep placeholders like {name} exactly as is, keep HTML tags (<b>, <i>, <a>, <blockquote>) "
        "and emoji, keep /commands untranslated. Do not add or remove sentences."
    )
    user = (
        f"Translate the values into {LANG_NAMES.get(lang, lang)}. English source (key → text):\n"
        f"{json.dumps(batch, ensure_ascii=False)}\n"
        f"Russian version for context only:\n{json.dumps(context, ensure_ascii=False)}\n"
        "Return ONLY a JSON object with the same keys and translated values."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def translate(llm: Client, lang: str, source: dict[str, str], ru: dict[str, str], keys: list[str], limit: int) -> dict[str, str]:
    done: dict[str, str] = {}
    for i in range(0, min(len(keys), limit * BATCH), BATCH):
        part = keys[i : i + BATCH]
        batch = {k: source[k] for k in part}

        def check(data: Any, batch: dict[str, str] = batch) -> list[str]:
            if not isinstance(data, dict) or set(data) != set(batch):
                return ["нужен JSON-объект с теми же ключами"]
            return [f"{k}: {e}" for k, v in data.items() for e in check_translation(batch[k], v)]

        done.update(llm.json(messages(lang, batch, {k: ru.get(k, "") for k in part}), check, max_tokens=4000))
    return done


def merged_file(source: dict[str, str], target: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """Порядок ключей — как в исходнике; существующие переводы сохраняются как есть."""
    return {k: target.get(k, new.get(k)) for k in source if k in target or k in new} | {k: v for k, v in target.items() if k not in source}


def main(argv: list[str] | None = None, client: Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--langs", default="", help="коды языков через запятую (vi,th)")
    ap.add_argument("--existing", action="store_true", help="все файлы локалей, кроме ru/en")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=6, help="максимум запросов к модели на язык")
    ap.add_argument("--report", default="")
    args = ap.parse_args(argv)
    langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    if args.existing:
        langs += [p.stem for p in sorted(LOCALES.glob("*.json")) if p.stem not in SOURCE_LANGS and p.stem not in langs]
    if not langs:
        print("нет языков для перевода")
        return 0
    source, ru = load("en"), load("ru")
    lines: list[str] = []
    try:
        todo = {lang: missing_keys(source, load(lang)) for lang in langs}
        if not any(todo.values()):
            print("все строки уже переведены")
            return 0
        llm = client or Client.from_env()
        for lang, keys in todo.items():
            if not keys:
                continue
            new = translate(llm, lang, source, ru, keys, args.limit)
            lines.append(f"- {lang}: переведено {len(new)} из {len(keys)} недостающих строк")
            if args.write and new:
                path = LOCALES / f"{lang}.json"
                path.write_text(json.dumps(merged_file(source, load(lang), new), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except QuotaExhausted as e:
        print(f"::warning::{e}")
        return 0
    except LLMError as e:
        print(f"::error::{e}")
        return 1
    report = "## Перевод локалей\n\n" + "\n".join(lines) + "\n\nПроверьте тон и термины; новый язык подключается в боте отдельно."
    print(report)
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
