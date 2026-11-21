"""Тесты словаря специалитетов и карточки «Что попробовать здесь».

Запуск: BOT_TOKEN=x FSQ_API_KEY=x python -m scripts.test_specialties [путь/к/словарю.json]
С путём — только проверка структуры файла (удобно для вычитки словаря).
"""

import copy
import json
import sys
from pathlib import Path

from bot.keyboards import inline_keyboards as kb
from bot.services import specialties as sp
from scripts.merge_specialties import merge

HCMC = (10.78, 106.70)
PHAN_RANG = (11.5778, 108.9945)


def main() -> None:
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        errs = sp.validate(data)
        print("\n".join(errs) or "словарь корректен")
        sys.exit(1 if errs else 0)

    data = sp.load()
    assert sp.validate(data) == [], sp.validate(data)
    assert {"ho_chi_minh", "hanoi", "phan_rang", "bangkok"} <= set(data["cities"])

    # 1. определение города по точке (возвращается копия с id)
    assert sp.detect_city(*PHAN_RANG)["id"] == "phan_rang"
    assert sp.detect_city(*HCMC)["id"] == "ho_chi_minh"
    assert sp.detect_city(13.75, 100.50)["id"] == "bangkok"
    assert sp.detect_city(15.88, 108.33)["id"] == "hoi_an"  # Хойан теперь в таблице
    assert sp.detect_city(11.94, 108.46)["id"] == "da_lat"
    assert sp.detect_city(14.707, 107.692) is None  # Кон Тум: города нет в таблице
    assert "id" not in data["cities"]["phan_rang"]  # исходный словарь не изменяется

    # 2. непроверенные блюда скрыты по умолчанию (в словаре все verified=false)
    hcmc = sp.detect_city(*HCMC)
    assert sp.visible_dishes(hcmc, show_unverified=False) == []
    assert sp.render("ru", hcmc, show_unverified=False) is None
    assert not sp.has_dishes(*HCMC)

    # 3. карточка: языки, лимит сообщения, формат блюда
    for lang in ("ru", "en", "zh"):
        for cid in data["cities"]:
            text = sp.render(lang, {**data["cities"][cid], "id": cid}, show_unverified=True)
            assert text and len(text) <= 4096, (lang, cid, len(text or ""))
    ru = sp.render("ru", hcmc, show_unverified=True)
    assert "<b>Cơm tấm</b> — Ком Там" in ru and "Хошимин" in ru and "💡" in ru, ru
    en = sp.render("en", hcmc, show_unverified=True)
    assert "Ho Chi Minh City" in en  # английских описаний в словаре пока нет → русские как запас
    th = sp.render("en", sp.detect_city(13.75, 100.50), show_unverified=True)
    assert "<b>ผัดกะเพรา</b>" in th, th

    # 4. экранирование HTML из словаря и английские поля, когда они есть
    evil = copy.deepcopy(hcmc)
    evil["specialties"][0]["title_local"] = "A <script> & B"
    out = sp.render("ru", evil, show_unverified=True)
    assert "<script>" not in out and "&lt;script&gt;" in out
    tr = copy.deepcopy(hcmc)
    tr["specialties"][0].update(title_en="Broken rice", short_desc_en="Broken rice with pork chop.")
    assert "Broken rice with pork chop." in sp.render("en", tr, show_unverified=True)
    assert sp.validate({**data, "cities": {"x": {**data["cities"]["hanoi"]}}}) == []

    # 5. валидатор ловит типичные ошибки
    def errs_of(mutate) -> list[str]:
        bad = copy.deepcopy(data)
        mutate(bad)
        return sp.validate(bad)

    def first(d):
        return d["cities"]["hanoi"]["specialties"][0]

    def unsourced(d):  # блюдо, которое второй проход не проверил: ссылок нет
        return next(x for x in d["cities"]["chiang_mai"]["specialties"] if "sources" not in x)

    assert "sources" not in unsourced(data)
    assert any("sources" in e for e in errs_of(lambda d: unsourced(d).update(verified=True)))
    assert not any("sources" in e for e in errs_of(lambda d: first(d).update(verified=True)))  # ссылки есть
    assert any("лишние ключи" in e for e in errs_of(lambda d: first(d).update(lol=1)))
    assert any("нет полей" in e for e in errs_of(lambda d: first(d).pop("how_to_eat")))
    assert any("дубль" in e for e in errs_of(lambda d: d["cities"]["hanoi"]["specialties"].append(dict(first(d)))))
    assert any("search_tokens" in e for e in errs_of(lambda d: first(d).update(search_tokens=[])))
    assert any("center" in e for e in errs_of(lambda d: d["cities"]["hanoi"].update(center={"lat": 99, "lon": 0})))
    assert any("schema_version" in e for e in errs_of(lambda d: d.update(schema_version="2.0")))
    assert any("cities" in e for e in sp.validate({"schema_version": "1.0", "cities": []}))

    # 6. verified=true с источником показывается без флага
    ok = copy.deepcopy(data)
    dish = ok["cities"]["ho_chi_minh"]["specialties"][0]
    dish.update(verified=True, sources=["https://example.org/x"])
    assert sp.validate(ok) == []
    assert [d["id"] for d in sp.visible_dishes(sp.detect_city(*HCMC, ok), show_unverified=False)] == [dish["id"]]

    # 7. кнопка «Что попробовать здесь» только когда есть подборка
    def tr_key(key):
        return key

    def cbs(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "try_here" not in cbs(kb.get_results_actions_keyboard(tr_key))
    assert "try_here" in cbs(kb.get_results_actions_keyboard(tr_key, show_try=True))
    place = {"place_id": "osm:node/1", "name": "X"}
    assert "try_here" not in cbs(kb.get_compact_keyboard(tr_key, [place], ["u"], ["s"]))
    assert "try_here" in cbs(kb.get_compact_keyboard(tr_key, [place], ["u"], ["s"], show_try=True))

    # 8. слияние «второго прохода»: verified не трогаем, status/note в словарь не попадают
    review = {
        "additions": {"test_city": {**copy.deepcopy(data["cities"]["hanoi"]), "name_ru": "Тест", "name_en": "Test"}},
        "updates": [
            {"city_id": "hanoi", "id": first(data)["id"], "status": "corrected", "note": "n",
             "title_en": "Pho", "short_desc_en": "Beef noodle soup of Hanoi.", "sources": ["https://a.org/x", "https://b.org/y"]},
            {"city_id": "hanoi", "id": "нет-такого", "status": "confirmed", "note": "n"},
        ],
        "removals": [{"city_id": "hanoi", "id": first(data)["id"], "reason": "r"}],
    }
    review["additions"]["test_city"]["specialties"][0]["verified"] = True  # модель не вправе подтверждать
    review["additions"]["test_city"]["specialties"][0]["sources"] = ["https://a.org/x"]
    merged, report = merge(data, review)
    h = merged["cities"]["hanoi"]["specialties"][0]
    assert h["title_en"] == "Pho" and h["sources"] == ["https://a.org/x", "https://b.org/y"] and h["verified"] is False
    assert "status" not in h and "note" not in h
    assert merged["cities"]["test_city"]["specialties"][0]["verified"] is False
    assert any("нет блюда" in r for r in report) and any("removals" in r for r in report)
    assert len(merged["cities"]["hanoi"]["specialties"]) == len(data["cities"]["hanoi"]["specialties"])  # removals не удаляют
    assert sp.validate(merged) == [], sp.validate(merged)
    assert "test_city" not in data["cities"]  # исходный словарь не изменился
    print("specialties ok")


if __name__ == "__main__":
    main()
