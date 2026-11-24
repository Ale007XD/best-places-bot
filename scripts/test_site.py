"""Тесты сборки сайта (scripts/build_site.py): страницы, ссылки, гиды только из вычитанного."""

import copy
import re
import tempfile
from pathlib import Path

from bot.services import specialties
from scripts import build_site as bs


def _pages(out: Path) -> set[str]:
    return {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}


def main() -> None:
    cfg = bs.load_config()
    assert bs.check_content() == [], bs.check_content()
    assert bs._shape({"a": [1, {"b": 2}]}) == {"a": [{}.get("x"), {"b": None}]}  # структура без текстов

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "_site"

        # 1. базовая сборка: в словаре нет вычитанных блюд → гидов на сайте нет.
        # Ссылки сообщества очищены: тест не зависит от того, что владелец вписал в site/config.json
        plain = {**cfg, "channel_url": "", "chat_url": "", "support_url": ""}
        files = bs.build(out, plain)
        got = _pages(out)
        assert {"index.html", "404.html", "robots.txt", "sitemap.xml", ".nojekyll", "static/style.css",
                "ru/index.html", "en/index.html", "ru/terms.html", "en/terms.html",
                "ru/privacy.html", "en/privacy.html"} <= got, got
        assert not any("/guide/" in f for f in files), files
        assert bs.check(out) == [], bs.check(out)
        ru = (out / "ru/index.html").read_text(encoding="utf-8")
        en = (out / "en/index.html").read_text(encoding="utf-8")
        assert "guide" not in ru and "Что попробовать здесь" not in ru  # непроверенное не рекламируем
        assert f"https://t.me/{cfg['bot_username']}?start=site_ru" in ru
        assert f"https://t.me/{cfg['bot_username']}?start=site_en" in en
        assert "@@" not in ru and f"@{cfg['bot_username']}" in ru
        assert 'lang="ru"' in ru and 'hreflang="en"' in ru and 'rel="canonical"' in ru
        assert "Powered by Foursquare" in ru and "© OpenStreetMap contributors" in en
        assert "community" not in ru  # ссылок на канал/чат/поддержку нет → блока нет

        # лендинг построен вокруг болей пользователя и их закрытия, а не вокруг техники
        content = {lang: bs.load_content(lang) for lang in ("ru", "en")}
        for lang, page_html in (("ru", ru), ("en", en)):
            main_text = page_html[page_html.index("<main"):page_html.index("</main>")]
            for item in content[lang]["pains"]["items"]:
                if item.get("id") != "guides":
                    assert bs.esc(item["pain"]) in main_text and bs.esc(item["fix"].replace("{bot}", cfg["bot_username"])) in main_text
            jargon = ("Foursquare", "Geoapify", "OpenStreetMap", "Overpass", "Mapbox", "inline", "API", "Redis", "FSM")
            jargon += ("кэш", "провайдер", "радиус") if lang == "ru" else ("cache", "provider", "radius")
            assert not [w for w in jargon if w.lower() in main_text.lower()], (lang, [w for w in jargon if w.lower() in main_text.lower()])
            assert main_text.index('id="pains"') < main_text.index('id="how"') < main_text.index('id="faq"')
        assert not re.search(r'(?:href|src)="/', ru), "абсолютные пути сломают сайт под /<repo>/"

        # 2. условия берут текст «Покупки» из локали бота (один источник правды)
        from bot.services.translator import get_string

        for lang in ("ru", "en"):
            terms = (out / f"{lang}/terms.html").read_text(encoding="utf-8")
            assert get_string("terms_text", lang=lang).split(".")[0] in terms, lang
            priv = (out / f"{lang}/privacy.html").read_text(encoding="utf-8")
            assert cfg["policy_date"] in priv and "Mapbox" in priv and "/feedback" in priv

        # 3. sitemap: все страницы, кроме 404 и служебного редиректа в корне
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        locs = set(re.findall(r"<loc>[^<]*/([^<]*)</loc>", sitemap))
        html_pages = {p for p in got if p.endswith(".html") and p not in ("404.html", "index.html")}
        assert {p.rsplit("/", 1)[-1] for p in html_pages} == locs

        # 4. корневой редирект по языку и запасной вариант без JS
        root = (out / "index.html").read_text(encoding="utf-8")
        assert "navigator" in root and "<noscript>" in root and 'href="ru/index.html"' in root

        # 5. экранирование конфигурации и блок сообщества, когда ссылки заданы
        evil = {**cfg, "brand": "<b>x</b>", "channel_url": "https://t.me/c", "support_url": "https://t.me/s"}
        bs.build(out, evil)
        page = (out / "en/index.html").read_text(encoding="utf-8")
        assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
        assert 'id="community"' in page and "https://t.me/c" in page and "https://t.me/s" in page
        assert bs.check(out) == []

        # 6. гиды: на сайт попадают только вычитанные блюда (verified=true + источники)
        data = copy.deepcopy(specialties.load())
        city = data["cities"]["hanoi"]
        city["specialties"][0].update(verified=True, sources=["https://example.org/a", "https://example.org/b"])
        city["specialties"][0]["title_en"] = "Beef pho"
        hidden_title = city["specialties"][1]["title_local"]  # не вычитано — на сайте быть не должно
        real_load = specialties.load
        def fake_load():
            return data

        specialties.load = fake_load
        try:
            files = bs.build(out)
            assert "ru/guide/hanoi.html" in files and "en/guide/hanoi.html" in files and "ru/guide/index.html" in files
            assert not any(f.endswith("/guide/hoi_an.html") for f in files)
            guide = (out / "ru/guide/hanoi.html").read_text(encoding="utf-8")
            assert city["specialties"][0]["title_local"] in guide and hidden_title not in guide
            assert "https://example.org/a" in guide and 'rel="nofollow noopener"' in guide
            assert "site_guide_ru" in guide
            en_guide = (out / "en/guide/hanoi.html").read_text(encoding="utf-8")
            assert "Beef pho" in en_guide and "Ханой" not in en_guide and "Hanoi" in en_guide
            ru_index = (out / "ru/index.html").read_text(encoding="utf-8")
            assert "Что попробовать здесь" in ru_index and "guide/index.html" in ru_index  # теперь есть что рекламировать
            assert bs.check(out) == [], bs.check(out)
        finally:
            specialties.load = real_load

        # 7. проверка ловит битые ссылки и плейсхолдеры
        (out / "ru/index.html").write_text('<html lang="ru"><title>t</title><a href="nope.html">x</a>{bot}', encoding="utf-8")
        problems = bs.check(out)
        assert any("битая ссылка nope.html" in p for p in problems) and any("плейсхолдер" in p for p in problems)

    print("site ok")


if __name__ == "__main__":
    main()
