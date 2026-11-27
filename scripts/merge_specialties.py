"""Вливает ответ «второго прохода» (additions / updates / removals) в bot/data/specialties.json.

Запуск: BOT_TOKEN=x FSQ_API_KEY=x python -m scripts.merge_specialties review.json [--write]
        [--evidence-out docs/specialties-evidence/v1_2a.json]
Без --write — только отчёт. verified никогда не меняется (проверяет человек); status и note
в словарь не попадают и выводятся в отчёте; removals только перечисляются, ничего не удаляют.
Файл не записывается, если после слияния валидатор находит ошибки.

Что в словарь не попадает вовсе: verification и youtube_evidence (доказательная база — это
сотни КБ, в образ Docker она не нужна). --evidence-out кладёт их отдельным файлом (docs/):
вычитывающему человеку нужны ссылки и кадры, боту — нет.

additions для города, который в словаре уже есть, — не «новый город», а свежая редакция блюд:
блюда находятся по id, поля из UPDATABLE применяются, расхождения в остальных полях только
выводятся в отчёте; блюда, которых нет, дописываются с verified=false.
"""

import copy
import json
import sys
from pathlib import Path
from typing import Any

from bot.services import specialties as sp

UPDATABLE = (
    "title_local", "search_tokens", "how_to_eat", "title_en", "short_desc_en", "how_to_eat_en", "sources", "venue_tokens",
    "locality_type",
)  # fmt: skip
EVIDENCE_FIELDS = ("status", "note", "locality_type", "verification", "youtube_evidence")
REVIEW_ONLY_FIELDS = ("title_ru", "title_ascii", "short_desc", "cuisine_tags")  # решает человек, не слияние
MAX_DISHES_PER_CITY = 12  # как в validate()


def _clean(dish: dict[str, Any]) -> dict[str, Any]:
    """Только поля словаря: verification, youtube_evidence, status, note остаются в evidence."""
    return {k: v for k, v in dish.items() if k in sp.DISH_FIELDS}


def _apply(dish: dict[str, Any], src: dict[str, Any], key: str, report: list[str]) -> None:
    old_sources = set(dish.get("sources", []))
    for field in UPDATABLE:
        if field in src:
            dish[field] = src[field]
    if "sources" in src:
        gone, new = old_sources - set(src["sources"]), set(src["sources"]) - old_sources
        if gone or new:
            report.append(f"sources: {key} — убрано {len(gone)}, добавлено {len(new)}")


def merge(base: dict[str, Any], review: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    data = copy.deepcopy(base)
    report: list[str] = []
    index = {(cid, d["id"]): d for cid, c in data["cities"].items() for d in c["specialties"]}
    existing_cities = set(data["cities"])
    seen: set[tuple[str, str]] = set()

    for cid, city in (review.get("additions") or {}).items():
        if cid not in existing_cities:
            city = copy.deepcopy(city)
            city["specialties"] = [{**_clean(d), "verified": False} for d in city.get("specialties", [])]  # проверяет человек
            data["cities"][cid] = city
            report.append(f"additions: добавлен город {cid} ({len(city['specialties'])} блюд)")
            continue
        added = refreshed = 0
        for src in city.get("specialties", []):
            key = (cid, src["id"])
            dish = index.get(key)
            if dish is None:
                if len(data["cities"][cid]["specialties"]) >= MAX_DISHES_PER_CITY:
                    report.append(f"additions: {cid}/{src['id']} — в городе уже {MAX_DISHES_PER_CITY} блюд, не добавлено")
                    continue
                new = {**_clean(src), "verified": False}
                data["cities"][cid]["specialties"].append(new)
                index[key] = new
                added += 1
                continue
            seen.add(key)
            _apply(dish, src, f"{cid}/{src['id']}", report)
            refreshed += 1
            for field in REVIEW_ONLY_FIELDS:
                if field in src and src[field] != dish.get(field):
                    report.append(f"additions: {cid}/{src['id']} — {field} в ревью другой (в словаре оставлен прежний)")
        report.append(f"additions: город {cid} уже есть — обновлено {refreshed}, добавлено {added}")

    for u in review.get("updates") or []:
        key = (u.get("city_id"), u.get("id"))
        dish = index.get(key)
        if dish is None:
            report.append(f"updates: нет блюда {key} — пропущено")
            continue
        seen.add(key)
        _apply(dish, u, f"{key[0]}/{key[1]}", report)
        status = u.get("status")
        if status != "confirmed":
            report.append(f"updates: {key[0]}/{key[1]} — {status}: {u.get('note', '')}")
    report.append(f"updates: применено {len([k for k in seen if k in {(x.get('city_id'), x.get('id')) for x in review.get('updates') or []}])}")

    unreviewed = sorted(k for k in index if k not in seen and k[0] in existing_cities)
    if unreviewed:
        report.append(f"без проверки (нет в updates/additions): {unreviewed}")
    for r in review.get("removals") or []:
        report.append(f"removals (НЕ удалено, решает человек): {r.get('city_id')}/{r.get('id')} — {r.get('reason')}")
    return data, report


def evidence(base: dict[str, Any], review: dict[str, Any], source_name: str = "") -> dict[str, Any]:
    """Доказательная база ревью: по ключу «город/блюдо» — вердикт, примечание, видео, убранные ссылки."""
    old = {(cid, d["id"]): d for cid, c in base["cities"].items() for d in c["specialties"]}
    dishes: dict[str, Any] = {}

    def put(cid: str, src: dict[str, Any]) -> None:
        entry = {k: src[k] for k in EVIDENCE_FIELDS if k in src}
        prev = old.get((cid, src["id"]))
        if prev is not None and "sources" in src:
            gone = sorted(set(prev.get("sources", [])) - set(src["sources"]))
            if gone:
                entry["sources_removed"] = gone  # ссылки, которых в новой редакции нет: ничего не теряем молча
        dishes[f"{cid}/{src['id']}"] = entry

    for cid, city in (review.get("additions") or {}).items():
        for src in city.get("specialties", []):
            put(cid, src)
    for u in review.get("updates") or []:
        put(u["city_id"], u)
    return {"schema": "specialties-evidence/1", "source_review": source_name, "dishes": dishes}


def main() -> None:
    argv = sys.argv[1:]
    out = None
    if "--evidence-out" in argv:
        i = argv.index("--evidence-out")
        out = Path(argv[i + 1])
        del argv[i : i + 2]
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    review_path = Path(args[0])
    review = json.loads(review_path.read_text(encoding="utf-8"))
    base = json.loads(sp.DATA_PATH.read_text(encoding="utf-8"))
    merged, report = merge(base, review)
    print("\n".join(report))
    errs = sp.validate(merged)
    if errs:
        print("\nОШИБКИ после слияния (файл не записан):\n" + "\n".join(errs))
        sys.exit(1)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(evidence(base, review, review_path.name), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"evidence: {out}")
    if "--write" in sys.argv:
        sp.DATA_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nзаписано: {sp.DATA_PATH}")
    else:
        print("\nвалидатор: ок (без --write файл не изменён)")


if __name__ == "__main__":
    main()
