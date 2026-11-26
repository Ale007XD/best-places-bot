"""Тесты ИИ-конвейеров (OpenRouter): клиент, объявления, подтверждение в боте, словарь, переводы.

Сеть не используется: httpx.MockTransport и заглушки модели.
"""

import asyncio
import copy
import json
import re
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis as fakeredis
import httpx
from aiogram.exceptions import TelegramAPIError

from bot.config import settings
from bot.handlers import account_handlers as acc
from scripts.ai import announce, dictionary, llm, translate_locales
from scripts.ai.llm import Client, LLMError, QuotaExhausted

ROOT = Path(__file__).resolve().parent.parent


def mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def reply(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": text}}]})


class StubLLM:
    """Вместо модели: отдаёт заранее заданные ответы, проверяет их тем же check, что и Client.json."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def json(self, messages, check, **_kw):
        self.calls += 1
        data = self.answers.pop(0)
        problems = check(data)
        if problems:
            raise LLMError("ответ не прошёл проверку: " + "; ".join(problems))
        return data


def test_llm() -> None:
    # JSON из «болтливого» ответа модели
    assert llm.extract_json('Вот:\n```json\n{"a": "br}ace \\\\ \\" q", "b": {"c": [1]}}\n```') == {"a": 'br}ace \\ " q', "b": {"c": [1]}}
    assert llm.extract_json("text [1, {\"a\": 2}] tail") == [1, {"a": 2}]
    try:
        llm.extract_json("нет json")
        raise AssertionError
    except ValueError:
        pass

    # каталог: только бесплатные «:free» с цены 0 и достаточным контекстом, по убыванию контекста
    catalog = {"data": [
        {"id": "a/small:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 8000},
        {"id": "b/big:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 128000},
        {"id": "c/mid:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 32000},
        {"id": "d/paid", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 99999},
        {"id": "e/trap:free", "pricing": {"prompt": "0.5", "completion": "0"}, "context_length": 99999},
    ]}
    assert llm.free_models(catalog) == ["b/big:free", "c/mid:free"]

    try:
        Client("")
        raise AssertionError
    except LLMError:
        pass

    # запасная модель: 500 → следующая; заголовок авторизации есть, ключ нигде не утекает
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=catalog)
        body = json.loads(request.content)
        seen.append({"model": body["model"], "auth": request.headers["authorization"]})
        return httpx.Response(500, text="boom") if body["model"] == "b/big:free" else reply("ok")

    client = Client("SECRET-KEY", http=mock(handler), sleep=lambda _s: None)
    assert client.models == ["b/big:free", "c/mid:free"]  # обнаружены через /models
    assert client.complete([{"role": "user", "content": "hi"}]) == "ok"
    assert [s["model"] for s in seen] == ["b/big:free", "c/mid:free"] and client.requests == 2
    assert seen[0]["auth"] == "Bearer SECRET-KEY"

    # суточный лимит → сразу QuotaExhausted, без перебора остальных моделей
    calls = []

    def daily(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text='{"error": {"message": "Rate limit exceeded: free-models-per-day"}}')

    c = Client("k", ["m1:free", "m2:free"], http=mock(daily), sleep=lambda _s: None)
    try:
        c.complete([{"role": "user", "content": "x"}])
        raise AssertionError
    except QuotaExhausted:
        assert len(calls) == 1

    # поминутный 429 и пустой ответ → следующая модель; 401 → остановка, ключ в сообщении не светится
    answers = iter([httpx.Response(429, text="slow down"), reply("  "), reply("третья")])
    c = Client("k", ["m1:free", "m2:free", "m3:free"], http=mock(lambda r: next(answers)), sleep=lambda _s: None)
    assert c.complete([{"role": "user", "content": "x"}]) == "третья"
    c = Client("SECRET-KEY", ["m1:free"], http=mock(lambda r: httpx.Response(401, text="no")), sleep=lambda _s: None)
    try:
        c.complete([{"role": "user", "content": "x"}])
        raise AssertionError
    except LLMError as e:
        assert "SECRET-KEY" not in str(e)

    # json(): одна повторная попытка с текстом ошибки; потом отказ
    seq = iter([reply("не json"), reply('{"ru": "ok"}')])
    c = Client("k", ["m1:free"], http=mock(lambda r: next(seq)), sleep=lambda _s: None)
    assert c.json([{"role": "user", "content": "x"}], lambda d: []) == {"ru": "ok"} and c.requests == 2
    seq2 = iter([reply('{"a": 1}'), reply('{"a": 1}')])
    c = Client("k", ["m1:free"], http=mock(lambda r: next(seq2)), sleep=lambda _s: None)
    try:
        c.json([{"role": "user", "content": "x"}], lambda d: ["нужен b"])
        raise AssertionError
    except LLMError as e:
        assert "нужен b" in str(e) and c.requests == 2  # лимит расходуем экономно: 1 + 1 повтор


def test_announce() -> None:
    ok = {"ru": "Теперь бот скрывает <b>закрытые</b> места.", "en": "The bot now hides <b>closed</b> places."}
    assert announce.check(ok) == []
    assert announce.check({"ru": "", "en": ""}) == []  # «нового для пользователей нет»
    bad = {
        "ru": "Привет <script>x</script>", "en": "Hi " + "x" * 950,
    }
    errs = announce.check(bad)
    assert any("тег <script>" in e for e in errs) and any("длиннее" in e for e in errs)
    assert any("неразрешённый" in e for e in announce.check({"ru": 'Смотри <a href="https://evil.example/x">тут</a>', "en": "ok"}))
    assert announce.check({"ru": 'Сайт <a href="https://t.me/NearbyNinjaBot">бот</a>', "en": 'Site <a href="https://u.github.io/r/en/">x</a>'}) == []
    assert any("секрет" in e for e in announce.check({"ru": "Ключ 123456789:ABCdefGhIJKlmNoPQRsTUVwxyz0123456789 тут", "en": "ok"}))
    assert any("только один язык" in e for e in announce.check({"ru": "Привет", "en": ""}))
    assert any("нет русского" in e for e in announce.check({"ru": "Hello", "en": "Hello"}))
    assert any("есть русский" in e for e in announce.check({"ru": "Привет", "en": "Привет"}))
    assert any("<" in e for e in announce.check({"ru": "a < b", "en": "ok"}))
    text = announce.compose(ok)
    assert text.startswith("🇷🇺 Теперь") and "———" in text and len(text) < 4096

    # доставка: approval → админу с кнопками; auto → в канал + копия админу
    sent: list[dict] = []

    def tg(request: httpx.Request) -> httpx.Response:
        assert "/bot123:tok/sendMessage" in str(request.url)
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    http = mock(tg)
    assert "подтверждение" in announce.deliver(http, "123:tok", text, "approval", "42", "")
    kb = sent[0]["reply_markup"]["inline_keyboard"][0]
    assert sent[0]["chat_id"] == "42" and [b["callback_data"] for b in kb] == ["ann:ok", "ann:no"] and sent[0]["parse_mode"] == "HTML"
    sent.clear()
    assert "канал" in announce.deliver(http, "123:tok", text, "auto", "42", "@chan")
    assert [m["chat_id"] for m in sent] == ["@chan", "42"] and "reply_markup" not in sent[0]
    for kwargs in (("approval", "", "@c"), ("auto", "42", "")):
        try:
            announce.deliver(http, "123:tok", text, *kwargs)
            raise AssertionError
        except LLMError:
            pass
    # ошибка Telegram не раскрывает токен
    err_http = mock(lambda r: httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    try:
        announce.telegram(err_http, "123:SECRETTOKEN", "sendMessage", {})
        raise AssertionError
    except LLMError as e:
        assert "SECRETTOKEN" not in str(e) and "chat not found" in str(e)

    # тема коммитов: служебные отфильтрованы
    assert announce._IGNORED_SUBJECTS.match("fix: typo") and announce._IGNORED_SUBJECTS.match("Merge branch x")
    assert not announce._IGNORED_SUBJECTS.match("Кнопка «Закрыто» скрывает места")

    # main: сухой прогон, approval, квота, пустой релиз
    import os

    os.environ.update(RELEASE_NOTES="Добавили кнопку «Закрыто»", TELEGRAM_BOT_TOKEN="123:tok", ADMIN_ID="42", CHANNEL_ID="@c")
    os.environ.pop("DRY_RUN", None)
    sent.clear()
    stub = StubLLM(ok)
    assert announce.main(["--tag", "v1", "--prev", "v0", "--dry-run"], http=http, client=stub) == 0 and sent == []
    assert announce.main(["--tag", "v1", "--prev", "v0", "--mode", "approval"], http=http, client=StubLLM(ok)) == 0
    assert len(sent) == 1 and sent[0]["chat_id"] == "42"
    assert announce.main(["--tag", "v1", "--prev", "v0"], http=http, client=StubLLM({"ru": "", "en": ""})) == 0 and len(sent) == 1

    class Exhausted:
        def json(self, *a, **k):
            raise QuotaExhausted("лимит")

    assert announce.main(["--tag", "v1", "--prev", "v0"], http=http, client=Exhausted()) == 0  # не падаем: повторим завтра
    assert announce.main([], http=http, client=stub) == 2  # нет тега


async def test_announce_handler() -> None:
    redis = fakeredis.FakeRedis(decode_responses=True)

    class FakeMessage:
        def __init__(self):
            self.chat = SimpleNamespace(id=7)
            self.message_id = 99
            self.html_text = "🇷🇺 Текст <b>объявления</b>"
            self.edit_reply_markup = AsyncMock()

    real_message = acc.Message
    acc.Message = FakeMessage  # type: ignore[misc,assignment]
    settings.ADMIN_ID, settings.CHANNEL_ID = 42, "@chan"
    try:
        def cb(data: str, uid: int = 42):
            return SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), message=FakeMessage(), answer=AsyncMock())

        bot = SimpleNamespace(send_message=AsyncMock())
        # чужой пользователь — ничего не публикуется
        c = cb("ann:ok", uid=1)
        await acc.announce_decision(c, bot, redis)
        assert bot.send_message.await_count == 0 and c.message.edit_reply_markup.await_count == 0
        # админ подтверждает: публикуется ровно показанный текст
        c = cb("ann:ok")
        await acc.announce_decision(c, bot, redis)
        args, kwargs = bot.send_message.await_args
        assert args == ("@chan", "🇷🇺 Текст <b>объявления</b>") and kwargs["parse_mode"] == "HTML"
        new_kb = c.message.edit_reply_markup.await_args.kwargs["reply_markup"]
        assert new_kb.inline_keyboard[0][0].text == "✅ Опубликовано"
        # повторное нажатие на то же сообщение не публикует второй раз
        again = SimpleNamespace(data="ann:ok", from_user=SimpleNamespace(id=42), message=c.message, answer=AsyncMock())
        await acc.announce_decision(again, bot, redis)
        assert bot.send_message.await_count == 1 and again.answer.await_args.args == ("Уже обработано",)
        # отклонение: не публикует
        c2 = cb("ann:no")
        c2.message.message_id = 100
        await acc.announce_decision(c2, bot, redis)
        assert bot.send_message.await_count == 1
        assert c2.message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text == "❌ Отклонено"
        # канал не задан → предупреждение, блокировка снята (можно повторить после настройки)
        settings.CHANNEL_ID = ""
        c3 = cb("ann:ok")
        c3.message.message_id = 101
        await acc.announce_decision(c3, bot, redis)
        assert c3.answer.await_args.kwargs["show_alert"] is True and bot.send_message.await_count == 1
        settings.CHANNEL_ID = "@chan"
        await acc.announce_decision(c3, bot, redis)
        assert bot.send_message.await_count == 2
        # ошибка Telegram (бот не админ канала) → сообщение админу и блокировка снята
        bot.send_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="not enough rights"))  # type: ignore[arg-type]
        c4 = cb("ann:ok")
        c4.message.message_id = 102
        await acc.announce_decision(c4, bot, redis)
        assert "not enough rights" in c4.answer.await_args.args[0] and c4.message.edit_reply_markup.await_count == 0
        assert await redis.get("ann:lock:7:102") is None
    finally:
        acc.Message = real_message  # type: ignore[misc]
        settings.ADMIN_ID, settings.CHANNEL_ID = 0, ""


def test_dictionary() -> None:
    from bot.services import specialties

    base = json.loads(specialties.DATA_PATH.read_text(encoding="utf-8"))
    city = {"id": "pattaya", "name_ru": "Паттайя", "name_en": "Pattaya", "country_code": "TH",
            "center": {"lat": 12.9236, "lon": 100.8825}, "radius_km": 12.0, "hint": "h"}

    def dish(i: str, local: str, ascii_: str, sources: list[str], **extra) -> dict:
        return {"id": i, "title_ru": f"{ascii_} (блюдо)", "title_local": local, "title_ascii": ascii_,
                "short_desc": "Жареная лапша с яйцом и арахисом.", "how_to_eat": "Подают с лаймом и перцем.",
                "search_tokens": [ascii_.lower()], "cuisine_tags": ["thai"], "title_en": ascii_,
                "short_desc_en": "Fried noodles with egg and peanuts.", "how_to_eat_en": "Served with lime and chili.",
                "sources": sources, "verified": True, **extra}

    pages = {  # url → (status, текст)
        "https://a.example/pad": (200, "<html>Best Pad Thai in town</html>"),
        "https://b.example/pad": (200, "<html>pad thai guide</html>"),
        "https://a.example/pad2": (200, "<html>another pad thai page</html>"),
        "https://c.example/404": (404, "nope"),
        "https://d.example/other": (200, "<html>about something else</html>"),
        "https://e.example/som": (200, "<html>som tam recipe</html>"),
    }

    def site(request: httpx.Request) -> httpx.Response:
        status, body = pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body, headers={"content-type": "text/html"})

    http = mock(site)
    pad = dish("pad_thai", "ผัดไทย", "Pad Thai", ["https://a.example/pad", "https://b.example/pad"], venue_tokens=["pad thai"])
    same_domain = dish("pad_same", "ผัดไทย2", "Pad Thai", ["https://a.example/pad", "https://a.example/pad2"])
    dead = dish("som_tam", "ส้มตำ", "Som Tam", ["https://c.example/404", "https://e.example/som"])
    unrelated = dish("khao_soi", "ข้าวซอย", "Khao Soi", ["https://d.example/other", "https://a.example/pad"])

    kept, report = dictionary.verify_sources(http, pad)
    assert kept == pad["sources"] and all(r.startswith("✓") for r in report)
    kept, report = dictionary.verify_sources(http, dead)
    assert kept == ["https://e.example/som"] and "HTTP 404" in report[0]
    kept, report = dictionary.verify_sources(http, unrelated)
    assert "название блюда на странице не найдено" in report[0]
    assert dictionary.enough_sources(["https://a.example/1", "https://b.example/2"])
    assert not dictionary.enough_sources(["https://a.example/1", "https://www.a.example/2"])  # один сайт
    assert not dictionary.enough_sources(["https://a.example/1"])

    # город: в словарь попадают только блюда с ≥ 2 рабочими источниками на разных сайтах;
    # verified принудительно false; лишние поля модели отбрасываются
    payload = {"specialties": [pad, same_domain, dead, unrelated], "notes": {"pad_thai": "тип в: везде есть"}}
    payload["specialties"][0]["лишнее"] = "x"
    merged, report_md = dictionary.run_city(base, city, StubLLM(payload), http)
    added = merged["cities"]["pattaya"]["specialties"]
    assert [d["id"] for d in added] == ["pad_thai"] and added[0]["verified"] is False
    assert "лишнее" not in added[0] and added[0]["sources"] == pad["sources"]
    assert "ОТБРОШЕНО" in report_md and "тип в: везде есть" in report_md
    assert "pattaya" not in base["cities"]  # исходный словарь не изменился
    assert specialties.validate(merged) == []
    # ни одно блюдо не прошло — ошибка, словарь не меняется
    try:
        dictionary.run_city(base, city, StubLLM({"specialties": [dead, unrelated]}), http)
        raise AssertionError
    except LLMError:
        pass
    # невалидный ответ модели (нет обязательных полей) не проходит проверку
    assert dictionary.check_payload(city)({"specialties": [{"id": "x"}]})
    assert dictionary.next_city(base, {"cities": [city]})["id"] == "pattaya"
    assert dictionary.next_city(merged, {"cities": [city]}) is None

    # venue_tokens: общие слова отбрасываются проверкой, остальное записывается
    bare = copy.deepcopy(base)  # словарь без venue_tokens: независимо от того, сколько их уже заполнено
    for c in bare["cities"].values():
        for d in c["specialties"]:
            d.pop("venue_tokens", None)
    ordered = [f"{cid}:{d['id']}" for cid, c in bare["cities"].items() for d in c["specialties"]]
    first_ref = ordered[0]
    check = dictionary.check_tokens({first_ref})
    assert check({"venue_tokens": {first_ref: ["cafe"]}}) and check({"venue_tokens": {"x:y": ["pho"]}})
    assert check({"venue_tokens": {first_ref: ["pho", "phở"]}}) == []
    answer = {"venue_tokens": {ref: ["pho"] for ref in ordered[:8]}}
    out, tokens_md = dictionary.run_tokens(bare, StubLLM(answer), limit=1)
    assert sum(1 for c in out["cities"].values() for d in c["specialties"] if d.get("venue_tokens") == ["pho"]) == 8
    assert "Добавлено полей: 8" in tokens_md

    # CLI: запись в файл и сухой прогон (путь словаря подменён)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "specialties.json"
        path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        backlog = Path(tmp) / "backlog.json"
        backlog.write_text(json.dumps({"cities": [city]}), encoding="utf-8")
        real_path, real_backlog = specialties.DATA_PATH, dictionary.BACKLOG
        specialties.DATA_PATH, dictionary.BACKLOG = path, backlog
        try:
            report_file = Path(tmp) / "r.md"
            assert dictionary.main(["city", "next", "--report", str(report_file)], client=StubLLM(copy.deepcopy(payload)), http=http) == 0
            assert "pattaya" not in json.loads(path.read_text(encoding="utf-8"))["cities"] and report_file.exists()  # без --write не пишем
            assert dictionary.main(["city", "next", "--write"], client=StubLLM(copy.deepcopy(payload)), http=http) == 0
            assert "pattaya" in json.loads(path.read_text(encoding="utf-8"))["cities"]
            assert dictionary.main(["city", "next"], client=StubLLM(), http=http) == 0  # бэклог пуст — нечего делать
        finally:
            specialties.DATA_PATH, dictionary.BACKLOG = real_path, real_backlog


def test_translate() -> None:
    src = "Hello, {name}! <b>Open</b> /start"
    assert translate_locales.check_translation(src, "Xin chào, {name}! <b>Mở</b> /start") == []
    assert any("плейсхолдеры" in e for e in translate_locales.check_translation(src, "Xin chào, {nam}! <b>Mở</b>"))
    assert any("теги" in e for e in translate_locales.check_translation(src, "Xin chào, {name}! Mở"))
    assert any("длинный" in e for e in translate_locales.check_translation("Hi", "x" * 40))
    assert translate_locales.check_translation(src, "  ") == ["пустой перевод"]

    en = {"a": "Hello {n}", "b": "Search <b>now</b>", "c": "Bye"}
    ru = {"a": "Привет {n}", "b": "Ищите <b>сейчас</b>", "c": "Пока"}
    assert translate_locales.missing_keys(en, {"a": "Xin chào {n}"}) == ["b", "c"]
    assert translate_locales.merged_file(en, {"a": "готово", "z": "старое"}, {"b": "Tìm <b>ngay</b>", "c": "Tạm biệt"}) == {
        "a": "готово", "b": "Tìm <b>ngay</b>", "c": "Tạm biệt", "z": "старое"}

    good = {"b": "Tìm <b>ngay</b>", "c": "Tạm biệt"}
    assert translate_locales.translate(StubLLM(good), "vi", en, ru, ["b", "c"], limit=2) == good
    try:  # потеря тега → ответ не принят
        translate_locales.translate(StubLLM({"b": "Tìm ngay", "c": "Tạm biệt"}), "vi", en, ru, ["b", "c"], limit=2)
        raise AssertionError
    except LLMError:
        pass

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for lang, data in (("en", en), ("ru", ru), ("zh", {"a": "你好 {n}"})):
            (folder / f"{lang}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        real = translate_locales.LOCALES
        translate_locales.LOCALES = folder
        try:
            report = folder / "r.md"
            assert translate_locales.main(["--existing", "--report", str(report)], client=StubLLM({"b": "沿着 <b>现在</b>", "c": "再见"})) == 0
            assert json.loads((folder / "zh.json").read_text(encoding="utf-8")) == {"a": "你好 {n}"}  # без --write не пишем
            stub = StubLLM({"b": "沿着 <b>现在</b>", "c": "再见"})
            assert translate_locales.main(["--existing", "--write"], client=stub) == 0
            assert json.loads((folder / "zh.json").read_text(encoding="utf-8")) == {"a": "你好 {n}", "b": "沿着 <b>现在</b>", "c": "再见"}
            assert translate_locales.main(["--existing", "--write"], client=StubLLM()) == 0  # всё переведено — модель не нужна
            assert translate_locales.main(["--langs", "vi", "--write"], client=StubLLM({"a": "Xin chào {n}", "b": "Tìm <b>ngay</b>", "c": "Tạm biệt"})) == 0
            assert set(json.loads((folder / "vi.json").read_text(encoding="utf-8"))) == {"a", "b", "c"}
        finally:
            translate_locales.LOCALES = real


def test_workflows() -> None:
    """Защита от инъекций в workflow: в `run:` нет ${{ … }} — данные идут через env."""
    for name in ("announce", "dictionary", "translate"):
        lines = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
            if not m:
                continue
            indent, rest = len(m.group(1)), m.group(2)
            body = [rest] if rest and rest not in ("|", ">") else []
            for nxt in lines[i + 1 :]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                body.append(nxt)
            assert not any("${{" in b for b in body), (name, body)
    ann = (ROOT / ".github/workflows/announce.yml").read_text(encoding="utf-8")
    assert "ANNOUNCE_MODE: ${{ inputs.mode || vars.ANNOUNCE_MODE || 'approval' }}" in ann  # по умолчанию — подтверждение
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts/ai/open_pr.sh")], check=False).returncode == 0


def main() -> None:
    test_llm()
    test_announce()
    asyncio.run(test_announce_handler())
    test_dictionary()
    test_translate()
    test_workflows()
    print("ai ok")


if __name__ == "__main__":
    main()
