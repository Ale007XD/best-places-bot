"""Вливает ответ «второго прохода» (additions / updates / removals) в bot/data/specialties.json.

Запуск: BOT_TOKEN=x FSQ_API_KEY=x python -m scripts.merge_specialties review.json [--write]
Без --write — только отчёт. verified никогда не меняется (проверяет человек); status и note
в словарь не попадают и выводятся в отчёте; removals только перечисляются, ничего не удаляют.
Файл не записывается, если после слияния валидатор находит ошибки.
"""

import copy
import json
import sys
from pathlib import Path
from typing import Any

from bot.services import specialties as sp

UPDATABLE = ("title_local", "search_tokens", "how_to_eat", "title_en", "short_desc_en", "how_to_eat_en", "sources")


def merge(base: dict[str, Any], review: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    data = copy.deepcopy(base)
    report: list[str] = []

    for cid, city in (review.get("additions") or {}).items():
        if cid in data["cities"]:
            report.append(f"additions: город {cid} уже есть — пропущен")
            continue
        city = copy.deepcopy(city)
        for dish in city.get("specialties", []):
            dish["verified"] = False  # проверяет человек
        data["cities"][cid] = city
        report.append(f"additions: добавлен город {cid} ({len(city.get('specialties', []))} блюд)")

    index = {(cid, d["id"]): d for cid, c in data["cities"].items() for d in c["specialties"]}
    seen: set[tuple[str, str]] = set()
    for u in review.get("updates") or []:
        key = (u.get("city_id"), u.get("id"))
        dish = index.get(key)
        if dish is None:
            report.append(f"updates: нет блюда {key} — пропущено")
            continue
        seen.add(key)
        for field in UPDATABLE:
            if field in u:
                dish[field] = u[field]
        status = u.get("status")
        if status != "confirmed":
            report.append(f"updates: {key[0]}/{key[1]} — {status}: {u.get('note', '')}")
    report.append(f"updates: применено {len(seen)}")

    unreviewed = sorted(k for k in index if k not in seen and not any(k[0] == a for a in (review.get('additions') or {})))
    if unreviewed:
        report.append(f"без проверки (нет в updates): {unreviewed}")
    for r in review.get("removals") or []:
        report.append(f"removals (НЕ удалено, решает человек): {r.get('city_id')}/{r.get('id')} — {r.get('reason')}")
    return data, report


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    review = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    base = json.loads(sp.DATA_PATH.read_text(encoding="utf-8"))
    merged, report = merge(base, review)
    print("\n".join(report))
    errs = sp.validate(merged)
    if errs:
        print("\nОШИБКИ после слияния (файл не записан):\n" + "\n".join(errs))
        sys.exit(1)
    if "--write" in sys.argv:
        sp.DATA_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nзаписано: {sp.DATA_PATH}")
    else:
        print("\nвалидатор: ок (без --write файл не изменён)")


if __name__ == "__main__":
    main()
