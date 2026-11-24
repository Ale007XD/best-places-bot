"""Собирает статический сайт в каталог _site/ (его выкладывает GitHub Pages через Actions).

    python -m scripts.build_site [--out _site] [--check]

Страницы: лендинг RU/EN, условия, конфиденциальность, гиды «что попробовать» по городам, sitemap,
robots, 404 и корневой редирект по языку браузера. Всё берётся из репозитория: тексты —
site/content/*.json, настройки — site/config.json, текст «Покупки» — из локалей бота,
гиды — из bot/data/specialties.json (только блюда с verified=true и ссылками на источники:
непроверенное на публичный сайт не попадает).
"""

import argparse
import html
import json
import os
import posixpath
import re
import shutil
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("BOT_TOKEN", "0:site-build")  # bot.config требует поля окружения
os.environ.setdefault("FSQ_API_KEY", "site-build")

from bot.services import specialties
from bot.services.translator import get_string

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "site"
LANGS = ("ru", "en")


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def load_config() -> dict[str, Any]:
    return json.loads((SRC / "config.json").read_text(encoding="utf-8"))


def load_content(lang: str) -> dict[str, Any]:
    return json.loads((SRC / "content" / f"{lang}.json").read_text(encoding="utf-8"))


def _shape(value: Any) -> Any:
    """Структура без текстов: ключи словарей и длины списков — для проверки паритета языков."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return None


def check_content() -> list[str]:
    """RU и EN должны совпадать по структуре (ключи, число пунктов)."""
    ru, en = load_content("ru"), load_content("en")
    return [] if _shape(ru) == _shape(en) else ["site/content: структура ru и en различается"]


def guide_cities() -> list[dict[str, Any]]:
    """Города с вычитанными блюдами (verified=true, с источниками) — только они идут на сайт."""
    out = []
    for cid, city in specialties.load()["cities"].items():
        dishes = specialties.visible_dishes(city, show_unverified=False)
        if dishes:
            out.append({**city, "id": cid, "_dishes": dishes})
    return out


class Site:
    def __init__(self, config: dict[str, Any]):
        self.cfg = config
        self.base = config["site_url"].rstrip("/")
        self.pages: dict[str, str] = {}
        self.cities = guide_cities()

    # --- ссылки ---
    def bot_url(self, tag: str) -> str:
        return f"https://t.me/{self.cfg['bot_username']}?start={tag}"

    @staticmethod
    def rel(src: str, dst: str) -> str:
        """Относительная ссылка между страницами (сайт лежит под /<repo>/, абсолютные пути нельзя)."""
        start = posixpath.dirname(src) or "."
        return posixpath.relpath(dst, start)

    # --- каркас страницы ---
    def page(self, lang: str, path: str, title: str, description: str, body: str, *, root: str | None = None) -> None:
        c = load_content(lang)
        other = "en" if lang == "ru" else "ru"
        canonical = f"{self.base}/{path}"
        alt_path = path.replace(f"{lang}/", f"{other}/", 1)
        alts = "".join(
            f'<link rel="alternate" hreflang="{code}" href="{self.base}/{p}">'
            for code, p in ((lang, path), (other, alt_path))
        )
        nav = "".join(
            f'<a href="{self.rel(path, f"{lang}/index.html")}#{anchor}">{esc(c["nav"][key])}</a>'
            for key, anchor in (("pains", "pains"), ("how", "how"), ("faq", "faq"))
        )
        if self.cfg.get("channel_url") or self.cfg.get("chat_url") or self.cfg.get("support_url"):
            nav += f'<a href="{self.rel(path, f"{lang}/index.html")}#community">{esc(c["nav"]["community"])}</a>'
        switch = (
            f'<a href="{self.rel(path, alt_path)}" hreflang="{other}" lang="{other}">{esc(load_content(other)["lang_name"])}</a>'
        )
        css = self.rel(path, "static/style.css")
        icon = self.rel(path, "static/favicon.svg")
        self.pages[path] = f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical}">{alts}
<meta property="og:type" content="website"><meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}"><meta property="og:url" content="{canonical}">
<link rel="icon" href="{icon}" type="image/svg+xml">
<link rel="stylesheet" href="{css}">
</head>
<body>
<header class="top"><div class="wrap">
<a class="brand" href="{self.rel(path, f"{lang}/index.html")}">🥷 {esc(self.cfg["brand"])}</a>
<nav class="main">{nav}</nav><span class="lang">{switch}</span>
</div></header>
{body}
{self.footer(lang, path)}
</body>
</html>
"""

    def footer(self, lang: str, path: str) -> str:
        c = load_content(lang)["footer"]
        links = [
            (self.rel(path, f"{lang}/terms.html"), c["terms"]),
            (self.rel(path, f"{lang}/privacy.html"), c["privacy"]),
        ]
        if self.cities:
            links.append((self.rel(path, f"{lang}/guide/index.html"), c["guides"]))
        links.append((self.cfg["repo_url"], c["source"]))
        anchors = "".join(f'<a href="{esc(h)}">{esc(t)}</a>' for h, t in links)
        return (
            f'<footer><div class="wrap"><p>{anchors}</p>'
            f'<p class="small">{esc(c["attribution"])}</p><p class="small">{esc(c["disclaimer"])}</p></div></footer>'
        )

    # --- страницы ---
    def landing(self, lang: str) -> None:
        c = load_content(lang)
        bot = self.cfg["bot_username"]  # в текстах стоит «@{bot}»

        def fill(text: str) -> str:
            return text.replace("{bot}", bot)
        cta = self.bot_url(f"site_{lang}")
        steps = "".join(
            f'<li class="card step"><h3>{esc(s["title"])}</h3><p>{esc(s["text"])}</p></li>' for s in c["how"]["steps"]
        )
        items = [i for i in c["pains"]["items"] if i.get("id") != "guides" or self.cities]
        pains = "".join(
            f'<div class="card"><p class="pain">{esc(i["pain"])}</p><p>{esc(fill(i["fix"]))}</p></div>'
            for i in items
        )
        p = c["pricing"]
        pricing = (
            f'<div class="card"><h3>{esc(p["free_title"])}</h3><p>{esc(p["free_text"])}</p></div>'
            if not self.cfg.get("money_enabled")
            else ""
        ) + f'<div class="card"><h3>{esc(p["paid_title"])}</h3><p>{esc(p["paid_text"])}</p></div>'
        faq = "".join(
            f'<details><summary>{esc(i["q"])}</summary><p>{esc(fill(i["a"]))}</p></details>'
            for i in c["faq"]["items"]
        )
        comm = ""
        labels = c["community"]
        for key, url in (("channel", "channel_url"), ("chat", "chat_url"), ("support", "support_url")):
            if self.cfg.get(url):
                comm += f'<a class="btn" href="{esc(self.cfg[url])}">{esc(labels[key])}</a> '
        community = f'<section id="community"><h2>{esc(labels["title"])}</h2><p>{comm}</p></section>' if comm else ""
        body = f"""<main class="wrap">
<section class="hero"><h1>{esc(c["hero"]["h1"])}</h1><p class="lead">{esc(c["hero"]["lead"])}</p>
<a class="btn" href="{esc(cta)}">{esc(c["hero"]["cta"])}</a><p class="note">{esc(c["hero"]["cta_note"])}</p></section>
<section id="pains"><h2>{esc(c["pains"]["title"])}</h2><p class="lede">{esc(c["pains"]["lead"])}</p><div class="grid">{pains}</div></section>
<section id="how"><h2>{esc(c["how"]["title"])}</h2><ol class="steps grid">{steps}</ol></section>
<section id="pricing"><h2>{esc(p["title"])}</h2><div class="grid">{pricing}</div></section>
<section id="faq"><h2>{esc(c["faq"]["title"])}</h2>{faq}</section>
{community}
</main>"""
        self.page(lang, f"{lang}/index.html", c["meta"]["title"], c["meta"]["description"], body)

    def legal(self, lang: str, kind: str) -> None:
        c = load_content(lang)[kind]
        terms_text = get_string("terms_text", lang=lang)
        parts = [f"<h1>{esc(c['title'])}</h1>"]
        if kind == "privacy":
            date = self.cfg["policy_date"]
            parts.append(f'<p class="note">{esc(c["updated"].format(date=date))}. {esc(c["note"])}</p>')
        for sec in c["sections"]:
            parts.append(f"<h2>{esc(sec['h'])}</h2>")
            for para in sec["p"]:
                parts.append(f"<p>{esc(para.replace('{terms_text}', terms_text))}</p>")
        body = '<main class="wrap"><article>' + "".join(parts) + "</article></main>"
        self.page(lang, f"{lang}/{kind}.html", c["title"], c["title"], body)

    def guides(self, lang: str) -> None:
        if not self.cities:
            return
        g = load_content(lang)["guides"]
        ru = lang == "ru"
        index_path = f"{lang}/guide/index.html"
        cards = "".join(
            f'<a class="card" href="{self.rel(index_path, lang + "/guide/" + c["id"] + ".html")}" style="text-decoration:none">'
            f'<h3>{esc(c["name_ru"] if ru else c["name_en"])}</h3><p>{len(c["_dishes"])}</p></a>'
            for c in self.cities
        )
        body = f'<main class="wrap"><article><h1>{esc(g["title"])}</h1><p>{esc(g["lead"])}</p><div class="grid">{cards}</div></article></main>'
        self.page(lang, index_path, g["title"], g["lead"], body)
        for city in self.cities:
            name = city["name_ru"] if ru else city["name_en"]
            path = f"{lang}/guide/{city['id']}.html"
            blocks = []
            for d in city["_dishes"]:
                desc = d["short_desc"] if ru else d.get("short_desc_en") or d["short_desc"]
                tip = d["how_to_eat"] if ru else d.get("how_to_eat_en") or d["how_to_eat"]
                srcs = "".join(
                    f'<li><a href="{esc(u)}" rel="nofollow noopener">{esc(u)}</a></li>' for u in d.get("sources", [])
                )
                blocks.append(
                    f'<div class="dish"><h3>{esc(specialties.dish_title(d, lang))}</h3><p>{esc(desc)}</p>'
                    f'<p class="tip">💡 {esc(g["how_to_eat"])}: {esc(tip)}</p><ul class="small">{srcs}</ul></div>'
                )
            cta = f'<p><a class="btn" href="{esc(self.bot_url(f"site_guide_{lang}"))}">{esc(g["open_bot"])}</a></p>'
            back = f'<p><a href="{self.rel(path, index_path)}">← {esc(g["back"])}</a></p>'
            title = f"{name} — {g['title']}"
            body = f'<main class="wrap"><article><h1>{esc(name)}</h1>{"".join(blocks)}{cta}{back}</article></main>'
            self.page(lang, path, title, f"{name}: {g['lead']}", body)

    def extras(self) -> dict[str, str]:
        urls = "".join(f"<url><loc>{self.base}/{p}</loc></url>" for p in sorted(self.pages))
        sitemap = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n'
        robots = f"User-agent: *\nAllow: /\nSitemap: {self.base}/sitemap.xml\n"
        redirect = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{esc(self.cfg["brand"])}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="canonical" href="{self.base}/en/index.html">
<link rel="alternate" hreflang="ru" href="{self.base}/ru/index.html"><link rel="alternate" hreflang="en" href="{self.base}/en/index.html">
<script>var l=((navigator.languages&&navigator.languages[0])||navigator.language||"en").toLowerCase();location.replace((l.indexOf("ru")===0?"ru":"en")+"/index.html");</script>
<noscript><meta http-equiv="refresh" content="0; url=en/index.html"></noscript></head>
<body><p><a href="ru/index.html">Русский</a> · <a href="en/index.html">English</a></p></body></html>
"""
        not_found = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>404</title><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><p>404 · <a href="{self.base}/ru/index.html">Русский</a> · <a href="{self.base}/en/index.html">English</a></p></body></html>
"""
        return {"sitemap.xml": sitemap, "robots.txt": robots, "index.html": redirect, "404.html": not_found, ".nojekyll": ""}


def build(out: Path, config: dict[str, Any] | None = None) -> list[str]:
    """Собирает сайт в out; возвращает список созданных файлов (относительные пути)."""
    site = Site(config or load_config())
    for lang in LANGS:
        site.landing(lang)
        site.legal(lang, "terms")
        site.legal(lang, "privacy")
        site.guides(lang)
    files = {**site.pages, **site.extras()}
    if out.exists():
        shutil.rmtree(out)
    for rel_path, content in files.items():
        target = out / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    shutil.copytree(SRC / "static", out / "static")
    return sorted([*files, *(f"static/{p.name}" for p in (SRC / "static").iterdir())])


_HREF = re.compile(r'(?:href|src)="([^"]+)"')


def check(out: Path) -> list[str]:
    """Проверка собранного сайта: внутренние ссылки ведут на существующие файлы, у страниц есть
    title и lang, на сайт не попали фигурные плейсхолдеры."""
    errors: list[str] = []
    for page in sorted(out.rglob("*.html")):
        rel_page = page.relative_to(out).as_posix()
        text = page.read_text(encoding="utf-8")
        if "<title>" not in text or '<html lang="' not in text:
            errors.append(f"{rel_page}: нет title или lang")
        if re.search(r"\{[a-z_]+\}", re.sub(r"<script.*?</script>", "", text, flags=re.DOTALL)):
            errors.append(f"{rel_page}: остался плейсхолдер {{...}}")
        for href in _HREF.findall(text):
            if href.startswith(("http://", "https://", "mailto:", "tg:", "#", "data:")):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel_page), href.split("#")[0].split("?")[0]))
            if not (out / target).exists():
                errors.append(f"{rel_page}: битая ссылка {href}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site")
    parser.add_argument("--check", action="store_true", help="после сборки проверить ссылки и структуру")
    args = parser.parse_args()
    errs = check_content()
    if errs:
        sys.exit("\n".join(errs))
    out = Path(args.out)
    files = build(out)
    print(f"собрано {len(files)} файлов в {out}/")
    if args.check:
        problems = check(out)
        if problems:
            sys.exit("\n".join(problems))
        print("проверка: ок")


if __name__ == "__main__":
    main()
