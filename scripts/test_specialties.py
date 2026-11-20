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


def main() -> None:
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        errs = sp.validate(data)
        print("\n".join(errs) or "словарь корректен")
        sys.exit(1 if errs else 0)

    data = sp.load()
    assert sp.validate(data) == [], sp.validate(data)
    ids = {c["id"] for c in data["cities"]}
    assert {"hcmc", "hanoi", "phan-rang"} <= ids, ids

    # 1. определение города по точке
    assert sp.detect_city(11.5778, 108.9945)["id"] == "phan-rang"  # Фанранг (центр)
    assert sp.detect_city(10.78, 106.70)["id"] == "hcmc"
    assert sp.detect_city(15.88, 108.33)["id"] == "hoi-an"  # ближе к центру Хойана, чем Дананга
    assert sp.detect_city(14.707, 107.692) is None  # Кон Тум: города нет в таблице

    # 2. непроверенные блюда скрыты по умолчанию (в словаре все verified=false)
    assert sp.visible_dishes(sp.detect_city(10.78, 106.70), show_unverified=False) == []
    assert sp.render("ru", sp.detect_city(10.78, 106.70), show_unverified=False) is None
    assert not sp.has_dishes(10.78, 106.70)

    # 3. карточка: язык, экранирование, лимит сообщения
    for lang in ("ru", "en", "zh"):
        for c in data["cities"]:
            text = sp.render(lang, c, show_unverified=True)
            assert text and len(text) <= 4096, (lang, c["id"], len(text or ""))
    ru = sp.render("ru", sp.detect_city(10.78, 106.70), show_unverified=True)
    assert "<b>Cơm tấm</b> — Ком там" in ru and "Хошимин" in ru, ru
    en = sp.render("en", sp.detect_city(10.78, 106.70), show_unverified=True)
    assert "Broken rice" in en and "Ho Chi Minh City" in en
    # название на местном языке совпало с переводом → не дублируется
    th = sp.render("en", sp.detect_city(13.75, 100.50), show_unverified=True)
    assert "<b>ผัดไทย</b> — Pad Thai" in th, th

    # 4. экранирование HTML из словаря
    evil = copy.deepcopy(data["cities"][0])
    evil["dishes"][0]["name"]["local"] = "A <script> & B"
    out = sp.render("ru", evil, show_unverified=True)
    assert "<script>" not in out and "&lt;script&gt;" in out

    # 5. verified=true — только с источником; валидатор ловит типичные ошибки
    bad = copy.deepcopy(data)
    bad["cities"][0]["dishes"][0]["verified"] = True
    assert any("sources" in e for e in sp.validate(bad)), sp.validate(bad)
    bad = copy.deepcopy(data)
    bad["cities"][0]["dishes"][0]["lol"] = 1
    assert any("лишние ключи" in e for e in sp.validate(bad))
    bad = copy.deepcopy(data)
    bad["cities"][1]["id"] = bad["cities"][0]["id"]
    assert any("дубль" in e for e in sp.validate(bad))
    bad = copy.deepcopy(data)
    bad["cities"][0]["dishes"][0]["kind"] = "pizza"
    assert any("kind" in e for e in sp.validate(bad))

    # 6. verified=true с источником показывается без флага
    ok = copy.deepcopy(data)
    ok["cities"][0]["dishes"][0]["verified"] = True
    ok["cities"][0]["dishes"][0]["sources"] = ["https://example.org/x"]
    assert sp.validate(ok) == []
    city = sp.detect_city(10.78, 106.70, ok)
    assert [d["id"] for d in sp.visible_dishes(city, show_unverified=False)] == ["com-tam"]

    # 7. кнопка «Что попробовать здесь» только когда есть подборка
    def tr(key):
        return key

    def cbs(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    assert "try_here" not in cbs(kb.get_results_actions_keyboard(tr))
    assert "try_here" in cbs(kb.get_results_actions_keyboard(tr, show_try=True))
    place = {"place_id": "osm:node/1", "name": "X"}
    assert "try_here" not in cbs(kb.get_compact_keyboard(tr, [place], ["u"], ["s"]))
    assert "try_here" in cbs(kb.get_compact_keyboard(tr, [place], ["u"], ["s"], show_try=True))
    print("specialties ok")


if __name__ == "__main__":
    main()
