"""Конвейер словаря специалитетов: модель предлагает блюда → источники проверяются автоматически →
изменения готовы для PR (verified всегда false: вычитывает человек).

    python -m scripts.ai.dictionary city <id|next> [--write] [--report FILE]
    python -m scripts.ai.dictionary tokens [--write] [--limit N] [--report FILE]

city   — новый город из bot/data/city_backlog.json (или первый ещё не добавленный): блюда, английские
         тексты, ссылки. Каждая ссылка скачивается; остаётся та, что открылась и где встречается
         название блюда; блюдо без двух таких ссылок на разных сайтах отбрасывается.
tokens — venue_tokens (слова из названий заведений) для блюд, где поля ещё нет; их использует
         «Найти блюдо». Общие слова (cafe, quán, ร้าน…) отбрасываются.
Без --write ничего не пишется (сухой прогон). В промпты идёт только содержимое репозитория.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from scripts.ai.llm import Client, LLMError, QuotaExhausted

ROOT = Path(__file__).resolve().parent.parent.parent
BACKLOG = ROOT / "bot" / "data" / "city_backlog.json"

GENERIC = frozenset({
    "cafe", "coffee", "quan", "nha hang", "restaurant", "bar", "food", "shop", "house", "kitchen",
    "bistro", "quan an", "an uong", "ca phe", "ร้าน", "อาหาร", "กาแฟ",
})  # fmt: skip
DISH_KEYS = {
    "id", "title_ru", "title_local", "title_ascii", "short_desc", "how_to_eat", "search_tokens",
    "cuisine_tags", "title_en", "short_desc_en", "how_to_eat_en", "venue_tokens", "sources",
}  # fmt: skip
MIN_SOURCES = 2
UA = "Mozilla/5.0 (compatible; NearbyNinja-dictionary-bot; +https://github.com/Ale007XD/best-places-bot)"


def _specialties():
    """bot.services.specialties требует переменные окружения bot.config — задаём заглушки."""
    import os

    os.environ.setdefault("BOT_TOKEN", "0:ai-pipeline")
    os.environ.setdefault("FSQ_API_KEY", "ai-pipeline")
    from bot.services import specialties
    from bot.utils.textnorm import fold

    return specialties, fold


def next_city(dictionary: dict[str, Any], backlog: dict[str, Any]) -> dict[str, Any] | None:
    have = set(dictionary["cities"])
    return next((c for c in backlog["cities"] if c["id"] not in have), None)


def city_messages(city: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "Ты помогаешь составить справочник «что попробовать» для Telegram-бота по поиску кафе. Нужны только "
        "блюда и напитки, которые реально едят местные жители и которые есть в обычных кафе и на уличных "
        "точках. Ничего не выдумывай: если не уверен — не включай блюдо. Тексты — своими словами."
    )
    user = f"""Город: id {city['id']}, {city['name_ru']} / {city['name_en']} ({city['country_code']}).
{city.get('hint', '')}
Дай 5–7 блюд (если надёжных меньше — меньше). Для КАЖДОГО блюда: минимум 2 источника на разных сайтах
(только реальные URL, где блюдо упомянуто в связи с этим городом; выдумывать ссылки нельзя).
Поля блюда: id (латиница, цифры, подчёркивания), title_ru («Транскрипция (пояснение)»), title_local (как в
меню, местным письмом), title_ascii, short_desc и how_to_eat (по 1–2 предложения на русском, 10–300 знаков;
how_to_eat только подтверждённое), title_en, short_desc_en, how_to_eat_en, search_tokens (1–8 написаний
блюда), cuisine_tags (до 6), venue_tokens (1–6 отличительных слов из НАЗВАНИЙ заведений этого блюда;
латиница без диакритики + местное письмо; без общих слов вроде cafe, quán, ร้าน), sources.
Верни ТОЛЬКО JSON: {{"specialties": [ ... ], "notes": {{"<id блюда>": "тип и обоснование"}}}}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def sanitize_dish(dish: dict[str, Any]) -> dict[str, Any]:
    """Только известные поля; verified принудительно false — подтверждает человек."""
    clean = {k: v for k, v in dish.items() if k in DISH_KEYS}
    clean["verified"] = False
    if "venue_tokens" in clean:
        clean["venue_tokens"] = [t for t in clean["venue_tokens"] if isinstance(t, str)]
    return clean


def build_city(city: dict[str, Any], dishes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name_ru": city["name_ru"], "name_en": city["name_en"], "country_code": city["country_code"],
        "center": city["center"], "radius_km": city["radius_km"],
        "specialties": [sanitize_dish(d) for d in dishes if isinstance(d, dict)],
    }  # fmt: skip


def check_payload(city: dict[str, Any]):
    """Проверка ответа модели той же схемой, что и для файла словаря."""
    specialties, _fold = _specialties()

    def check(data: Any) -> list[str]:
        if not isinstance(data, dict) or not isinstance(data.get("specialties"), list) or not data["specialties"]:
            return ['нужен объект {"specialties": [...], "notes": {...}} с блюдами']
        probe = {"schema_version": specialties.SCHEMA_VERSION, "cities": {city["id"]: build_city(city, data["specialties"])}}
        return specialties.validate(probe)

    return check


def verify_sources(http: httpx.Client, dish: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(рабочие ссылки, строки отчёта). Рабочая: открылась (< 400), HTML и содержит название блюда."""
    specialties, fold = _specialties()
    needles = [n for n in (*specialties.dish_needles(dish), fold(dish.get("title_en", ""))) if len(n) >= 3]
    kept, report = [], []
    for url in dish.get("sources", []):
        try:
            r = http.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=15)
        except httpx.HTTPError as e:
            report.append(f"✗ {url} — не открылась ({type(e).__name__})")
            continue
        if r.status_code >= 400:
            report.append(f"✗ {url} — HTTP {r.status_code}")
        elif "html" not in r.headers.get("content-type", "html").lower():
            report.append(f"✗ {url} — не HTML")
        elif not any(n in fold(r.text[:400_000]) for n in needles):
            report.append(f"✗ {url} — название блюда на странице не найдено")
        else:
            kept.append(url)
            report.append(f"✓ {url}")
    return kept, report


def enough_sources(urls: list[str]) -> bool:
    return len(urls) >= MIN_SOURCES and len({urlparse(u).netloc.removeprefix("www.") for u in urls}) >= MIN_SOURCES


def run_city(dictionary: dict[str, Any], city: dict[str, Any], llm: Client, http: httpx.Client) -> tuple[dict[str, Any], str]:
    """Возвращает (новый словарь, отчёт в Markdown). Исходный словарь не изменяется."""
    specialties, _fold = _specialties()
    payload = llm.json(city_messages(city), check_payload(city))
    notes = payload.get("notes") if isinstance(payload.get("notes"), dict) else {}
    kept_dishes, lines = [], [f"## {city['name_ru']} ({city['id']}) — предложено блюд: {len(payload['specialties'])}", ""]
    for dish in payload["specialties"]:
        good, report = verify_sources(http, dish)
        status = "принято (verified=false)" if enough_sources(good) else "ОТБРОШЕНО: нет двух рабочих источников"
        lines += [f"### {dish.get('title_local')} — {status}", f"Тип/обоснование модели: {notes.get(dish.get('id'), '—')}", *[f"- {r}" for r in report], ""]
        if enough_sources(good):
            kept_dishes.append({**dish, "sources": good})
    if not kept_dishes:
        raise LLMError("ни одно блюдо не прошло проверку источников")
    merged = json.loads(json.dumps(dictionary))
    merged["cities"][city["id"]] = build_city(city, kept_dishes)
    errors = specialties.validate(merged)
    if errors:
        raise LLMError("словарь после добавления некорректен: " + "; ".join(errors[:5]))
    lines.append(f"Добавлено блюд: {len(kept_dishes)}. Поставить verified=true можно только после ручной проверки ссылок.")
    return merged, "\n".join(lines)


def tokens_messages(batch: list[tuple[str, dict[str, Any], str]]) -> list[dict[str, str]]:
    items = [
        {"ref": ref, "город": city_name, "title_local": d["title_local"], "title_ascii": d["title_ascii"], "search_tokens": d["search_tokens"]}
        for ref, d, city_name in batch
    ]
    system = "Ты помогаешь настроить поиск заведений по названию. Ничего не выдумывай."
    user = (
        "Для каждого блюда дай venue_tokens: 1–6 отличительных слов, которые встречаются в НАЗВАНИЯХ заведений, "
        "специализирующихся на блюде (например, для «Phở bò» — \"pho\", а не \"pho bo\"). Латиница строчными без "
        "диакритики плюс местное письмо. Запрещены общие слова: cafe, coffee, quán, nhà hàng, restaurant, bar, food, "
        "ร้าน, อาหาร, กาแฟ. Если по названию заведения блюдо не опознать — верни пустой список.\n"
        f"Блюда: {json.dumps(items, ensure_ascii=False)}\n"
        'Верни ТОЛЬКО JSON: {"venue_tokens": {"<ref>": ["..."]}}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def check_tokens(refs: set[str]):
    _s, fold = _specialties()

    def check(data: Any) -> list[str]:
        vt = data.get("venue_tokens") if isinstance(data, dict) else None
        if not isinstance(vt, dict) or set(vt) - refs:
            return ['нужен {"venue_tokens": {ref: [...]}} только для переданных ref']
        errs = []
        for ref, tokens in vt.items():
            if not isinstance(tokens, list) or len(tokens) > 6 or not all(isinstance(t, str) and 2 <= len(t.strip()) <= 40 for t in tokens):
                errs.append(f"{ref}: до 6 строк по 2..40 символов")
            elif [t for t in tokens if fold(t) in GENERIC]:
                errs.append(f"{ref}: общие слова запрещены: {[t for t in tokens if fold(t) in GENERIC]}")
        return errs

    return check


def run_tokens(dictionary: dict[str, Any], llm: Client, limit: int = 8, batch_size: int = 8) -> tuple[dict[str, Any], str]:
    merged = json.loads(json.dumps(dictionary))
    todo = [
        (f"{cid}:{d['id']}", d, c["name_ru"])
        for cid, c in merged["cities"].items()
        for d in c["specialties"]
        if "venue_tokens" not in d
    ]
    lines = [f"## venue_tokens: блюд без поля — {len(todo)}", ""]
    by_ref = {ref: d for ref, d, _ in todo}
    added = 0
    for i in range(0, min(len(todo), limit * batch_size), batch_size):
        batch = todo[i : i + batch_size]
        data = llm.json(tokens_messages(batch), check_tokens({ref for ref, _d, _c in batch}))
        for ref, tokens in data["venue_tokens"].items():
            if tokens:
                by_ref[ref]["venue_tokens"] = [t.strip() for t in tokens]
                lines.append(f"- {ref}: {', '.join(tokens)}")
                added += 1
    lines.append(f"\nДобавлено полей: {added}. Проверьте, что слова действительно встречаются в названиях заведений (`python -m bot.diag dish <город:блюдо>`).")
    specialties, _fold = _specialties()
    errors = specialties.validate(merged)
    if errors:
        raise LLMError("словарь после добавления некорректен: " + "; ".join(errors[:5]))
    return merged, "\n".join(lines)


def main(argv: list[str] | None = None, client: Client | None = None, http: httpx.Client | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("city")
    c.add_argument("city_id")
    t = sub.add_parser("tokens")
    t.add_argument("--limit", type=int, default=8, help="максимум запросов к модели")
    for p in (c, t):
        p.add_argument("--write", action="store_true")
        p.add_argument("--report", default="")
    args = ap.parse_args(argv)
    specialties, _fold = _specialties()
    dictionary = json.loads(specialties.DATA_PATH.read_text(encoding="utf-8"))
    try:
        llm = client or Client.from_env()
        if args.cmd == "city":
            backlog = json.loads(BACKLOG.read_text(encoding="utf-8"))
            city = next_city(dictionary, backlog) if args.city_id == "next" else next((x for x in backlog["cities"] if x["id"] == args.city_id), None)
            if city is None:
                print("в бэклоге нет подходящего города — всё уже добавлено")
                return 0
            merged, report = run_city(dictionary, city, llm, http or httpx.Client())
        else:
            merged, report = run_tokens(dictionary, llm, limit=args.limit)
    except QuotaExhausted as e:
        print(f"::warning::{e}")
        return 0
    except LLMError as e:
        print(f"::error::{e}")
        return 1
    print(report)
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
    if args.write:
        specialties.DATA_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"записано: {specialties.DATA_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
