"""python -m scripts.test_dictionary — слияние ревью, locality_type, evidence вне образа, вычитанные блюда в выдаче.

Проверяет без сети: (1) слияние additions для города, который уже есть, и вынос evidence;
(2) locality_type: валидатор, порядок карточки, срез MAX_DISHES; (3) боевой словарь: в образе нет
evidence, venue_tokens согласованы с гейтом «Найти блюдо», а блюдо, ставшее verified, реально
попадает в карточку, кнопки, разбор текста и поиск; (4) docs/specialties-evidence покрывает словарь.
Запуск: BOT_TOKEN=x FSQ_API_KEY=x python -m scripts.test_dictionary
"""

import copy
import json
from pathlib import Path

from bot.keyboards import inline_keyboards as kb
from bot.services import specialties as sp
from bot.utils.textnorm import fold, matches
from scripts.merge_specialties import evidence, merge

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "specialties-evidence" / "v1_2a.json"
IMAGE_DICT_LIMIT = 150_000  # байт: словарь едет в образ Docker, доказательства (сотни КБ) — нет
# Пары «токен блюда A попадает в название блюда B того же города» — осознанные, не ошибка словаря.
KNOWN_OVERLAP = {("nha_trang", "bun_ca_nha_trang", "bun_ca_sua_nha_trang")}


def _dish(i: int, loc: str | None, verified: bool = True) -> dict:
    d = {
        "id": f"dish_{i}", "title_ru": f"Блюдо {i}", "title_local": f"Món {i}", "title_ascii": f"mon {i}",
        "short_desc": "Описание блюда для теста.", "how_to_eat": "Как есть это блюдо — подсказка.",
        "search_tokens": [f"mon {i}"], "cuisine_tags": ["test"], "verified": verified,
        "sources": ["https://a.org/x"],
    }  # fmt: skip
    if loc:
        d["locality_type"] = loc
    return d


def main() -> None:
    base = json.loads(sp.DATA_PATH.read_text(encoding="utf-8"))
    assert sp.validate(base) == [] and base["schema_version"] == "1.0"

    # 1. слияние: additions для существующего города = обновление по id, лишние поля не попадают
    hoi = base["cities"]["hoi_an"]["specialties"][0]
    review = {
        "additions": {
            "hoi_an": {
                "specialties": [
                    {
                        **{k: v for k, v in hoi.items() if k != "verified"},
                        "how_to_eat": "Новая подсказка о том, как это есть.", "title_ru": "Другое имя",
                        "sources": ["https://new.org/a"], "locality_type": "city_origin", "verified": True,
                        "verification": {"decision": "confirmed"}, "youtube_evidence": [{"url": "https://y.t/1"}],
                    },
                    {**_dish(1, "generic"), "id": "banh_new_hoi_an", "verification": {}, "youtube_evidence": []},
                ]
            },
            "new_city": {
                "name_ru": "Н", "name_en": "N", "country_code": "VN", "center": {"lat": 1.0, "lon": 2.0},
                "radius_km": 5, "specialties": [{**_dish(2, "regional"), "verification": {}, "youtube_evidence": []}],
            },
        },
        "updates": [],
    }
    merged, report = merge(base, review)
    assert sp.validate(merged) == [], sp.validate(merged)
    got = next(d for d in merged["cities"]["hoi_an"]["specialties"] if d["id"] == hoi["id"])
    assert got["how_to_eat"].startswith("Новая") and got["sources"] == ["https://new.org/a"]
    assert got["locality_type"] == "city_origin" and got["verified"] is False  # verified ставит только человек
    assert got["title_ru"] == hoi["title_ru"]  # остальные поля — решает человек
    assert any("title_ru" in r and hoi["id"] in r for r in report), report
    assert any(r.startswith("sources:") and hoi["id"] in r for r in report)
    new = next(d for d in merged["cities"]["hoi_an"]["specialties"] if d["id"] == "banh_new_hoi_an")
    assert new["verified"] is False and "verification" not in new and "youtube_evidence" not in new
    assert merged["cities"]["new_city"]["specialties"][0]["verified"] is False
    assert not any("verification" in d or "youtube_evidence" in d for c in merged["cities"].values() for d in c["specialties"])
    assert "new_city" not in base["cities"] and base["cities"]["hoi_an"]["specialties"][0]["how_to_eat"] == hoi["how_to_eat"]
    ev = evidence(base, review, "r.json")
    assert ev["dishes"][f"hoi_an/{hoi['id']}"]["verification"] == {"decision": "confirmed"}
    assert ev["dishes"][f"hoi_an/{hoi['id']}"]["sources_removed"] == sorted(hoi["sources"])  # убранные ссылки не теряются
    assert ev["dishes"]["hoi_an/banh_new_hoi_an"]["youtube_evidence"] == []

    # 2. locality_type: валидатор и порядок карточки
    for bad in ("local", "", None, 3):
        broken = copy.deepcopy(base)
        broken["cities"]["hanoi"]["specialties"][0]["locality_type"] = bad
        assert any("locality_type" in e for e in sp.validate(broken)), bad
    mixed = {"specialties": [_dish(1, "generic"), _dish(2, "regional"), _dish(3, None), _dish(4, "city_origin"),
                             _dish(5, "local_favorite"), _dish(6, "city_specialty")]}  # fmt: skip
    order = [d["id"] for d in sp.visible_dishes(mixed, show_unverified=False)]
    assert order == ["dish_4", "dish_3", "dish_6", "dish_5", "dish_2", "dish_1"], order  # без поля = city_specialty, устойчиво
    many = {"specialties": [_dish(i, "generic") for i in range(1, 8)] + [_dish(8, "city_origin"), _dish(9, "city_origin")]}
    shown = [d["id"] for d in sp.visible_dishes(many, show_unverified=False)]
    assert len(shown) == sp.MAX_DISHES and shown[:2] == ["dish_8", "dish_9"] and "dish_7" not in shown  # общие отбрасываются первыми
    assert [d["id"] for d in sp.visible_dishes({"specialties": [_dish(1, None, verified=False)]}, show_unverified=False)] == []

    # 3. боевой словарь: в образе только словарь, доказательства — в docs/
    assert sp.DATA_PATH.stat().st_size < IMAGE_DICT_LIMIT, sp.DATA_PATH.stat().st_size
    text = sp.DATA_PATH.read_text(encoding="utf-8")
    assert "youtube_evidence" not in text and '"verification"' not in text
    assert not any(ROOT.joinpath("bot").rglob("*evidence*"))
    dishes = [(cid, d) for cid, c in base["cities"].items() for d in c["specialties"]]
    assert len(dishes) >= 56 and all(d["verified"] is False or d.get("sources") for _cid, d in dishes)
    for cid, c in base["cities"].items():  # по умолчанию (SPECIALTIES_SHOW_UNVERIFIED=false) видны только вычитанные
        assert {d["id"] for d in sp.visible_dishes(c, show_unverified=False)} == {
            d["id"] for d in c["specialties"] if d["verified"]
        }, cid

    # 4. venue_tokens согласованы с гейтом: название «Quán <токен>» находит своё блюдо, чужие слова — нет
    for cid, d in dishes:
        needles, spell = sp.venue_needles(d), sp.venue_spellings(d)
        assert needles, (cid, d["id"])
        for token in d.get("venue_tokens", []):
            assert fold(token) in needles, (cid, d["id"], token)
            # токен — слова из НАЗВАНИЙ заведений («Lẩu Cừu»), не обязательно из названия блюда
            assert matches(f"Quán {token} Số 1", needles, spell), (cid, d["id"], token)
        assert not matches("Cửa Hàng Tiện Lợi 24h", needles, spell), (cid, d["id"])
        for other in base["cities"][cid]["specialties"]:
            if other is d:
                continue
            hit = any(matches(t, d.get("venue_tokens", [])) for t in (other["title_local"], other["title_ascii"]))
            assert not hit or (cid, d["id"], other["id"]) in KNOWN_OVERLAP, (cid, d["id"], other["id"])

    # 5. блюдо, ставшее verified, попадает в карточку, кнопки, разбор текста и поиск
    verified = copy.deepcopy(base)
    for c in verified["cities"].values():
        for d in c["specialties"]:
            d["verified"] = True
    assert sp.validate(verified) == []  # verified=true допустим: у каждого блюда есть sources
    real_load = sp.load
    sp.load = lambda: verified  # type: ignore[assignment]
    try:
        for cid, c in verified["cities"].items():
            lat, lon = c["center"]["lat"], c["center"]["lon"]
            city = sp.detect_city(lat, lon)
            assert city and city["id"] == cid, cid
            shown = sp.visible_dishes(city, show_unverified=False)
            assert len(shown) == min(len(c["specialties"]), sp.MAX_DISHES), cid
            card = sp.render("ru", city, show_unverified=False)
            assert card and all(sp.clean(d["title_local"]) in card for d in shown), cid
            cbs = [b.callback_data for row in kb.get_dish_keyboard(cid, shown).inline_keyboard for b in row]
            assert cbs == [f"dish:{cid}:{d['id']}" for d in shown] and all(len(x.encode()) <= 64 for x in cbs)
            for d in shown:
                assert sp.get_dish(sp.dish_ref(cid, d["id"]))[1]["id"] == d["id"]
                hit = sp.resolve_dish(sp.dish_needles(d)[0], lat, lon, show_unverified=False)
                assert hit and hit[0]["id"] == cid and sp.dish_needles(hit[1])[0] == sp.dish_needles(d)[0], (cid, d["id"])
    finally:
        sp.load = real_load
    assert sp.resolve_dish("pho", 21.03, 105.85, show_unverified=False) is None  # боевой словарь: не вычитано — скрыто

    # 6. доказательства лежат в docs/ и покрывают словарь
    ev_file = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert ev_file["schema"] == "specialties-evidence/1" and ev_file["dishes"]
    keys = {f"{cid}/{d['id']}" for cid, d in dishes}
    assert set(ev_file["dishes"]) <= keys, sorted(set(ev_file["dishes"]) - keys)
    assert len(ev_file["dishes"]) >= 55
    assert all(e.get("verification") is not None for e in ev_file["dishes"].values())
    assert all(d["verified"] is False for _cid, d in dishes), "verified ставит человек — слияние его не трогает"

    print("dictionary ok")


if __name__ == "__main__":
    main()
