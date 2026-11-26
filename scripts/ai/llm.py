"""Тонкий клиент OpenRouter (OpenAI-совместимый API) под бесплатные модели.

Правила, из-за которых клиент устроен именно так:
  • Бесплатный тариф OpenRouter — 20 запросов/мин и 50/сутки на аккаунт (1000/сутки после $10
    кредитов), неудачные попытки тоже считаются → мало попыток: по одной на модель из цепочки.
  • Идентификаторы бесплатных моделей (":free") появляются и исчезают, поэтому цепочка задаётся
    OPENROUTER_MODELS (через запятую); если пусто — берётся из каталога /models: бесплатные
    (цена 0) с контекстом ≥ 16k.
  • Бесплатные endpoint'ы могут логировать запросы и обучаться на них: в промпты кладём только
    содержимое репозитория (заметки релиза, словарь, строки локалей), НИКОГДА данные пользователей.
  • Ответ — только текст; JSON просим в промпте и разбираем сами (response_format поддерживают не все).
"""

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

BASE_URL = "https://openrouter.ai/api/v1"
MIN_CONTEXT = 16_000
MAX_MODELS = 4


class LLMError(Exception):
    """Не удалось получить пригодный ответ."""


class QuotaExhausted(LLMError):
    """Суточный лимит бесплатных запросов исчерпан — повторять сегодня бессмысленно."""


def free_models(catalog: dict[str, Any], limit: int = MAX_MODELS) -> list[str]:
    """Бесплатные модели каталога OpenRouter (id с «:free», цена 0), по убыванию контекста."""
    found = []
    for m in catalog.get("data", []):
        pricing = m.get("pricing") or {}
        free = str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0"
        if m.get("id", "").endswith(":free") and free and int(m.get("context_length") or 0) >= MIN_CONTEXT:
            found.append((int(m["context_length"]), m["id"]))
    return [mid for _ctx, mid in sorted(found, reverse=True)[:limit]]


def extract_json(text: str) -> Any:
    """JSON из ответа модели: снимает ```-ограждения и берёт первый сбалансированный объект/массив."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    for start, ch in enumerate(cleaned):
        if ch not in "{[":
            continue
        depth, in_str, esc = 0, False, False
        for end in range(start, len(cleaned)):
            c = cleaned[end]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : end + 1])
                    except ValueError:
                        break
    raise ValueError("в ответе нет корректного JSON")


class Client:
    def __init__(
        self,
        api_key: str,
        models: list[str] | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        referer: str = "https://github.com/Ale007XD/best-places-bot",
    ):
        if not api_key:
            raise LLMError("не задан OPENROUTER_API_KEY")
        self._http = http or httpx.Client(timeout=120)
        self._sleep = sleep
        self._headers = {"Authorization": f"Bearer {api_key}", "HTTP-Referer": referer, "X-Title": "NearbyNinja automation"}
        self.models = models or self._discover()
        self.requests = 0  # сколько запросов потрачено (для лога: бюджет суток ограничен)

    @classmethod
    def from_env(cls, **kw: Any) -> "Client":
        raw = os.environ.get("OPENROUTER_MODELS", "")
        models = [m.strip() for m in raw.split(",") if m.strip()]
        return cls(os.environ.get("OPENROUTER_API_KEY", ""), models or None, **kw)

    def _discover(self) -> list[str]:
        r = self._http.get(f"{BASE_URL}/models", headers=self._headers)
        r.raise_for_status()
        models = free_models(r.json())
        if not models:
            raise LLMError("в каталоге OpenRouter нет подходящих бесплатных моделей; задайте OPENROUTER_MODELS")
        return models

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 2500, temperature: float = 0.2) -> str:
        """Текст ответа первой модели цепочки, которая ответила; остальные — запасные."""
        last = "нет моделей"
        for model in self.models:
            self.requests += 1
            try:
                r = self._http.post(
                    f"{BASE_URL}/chat/completions",
                    headers=self._headers,
                    json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                )
            except httpx.HTTPError as e:
                last = f"{model}: {type(e).__name__}"
                continue
            if r.status_code == 429:
                body = r.text.lower()
                if "per-day" in body or "per day" in body or "daily" in body:
                    raise QuotaExhausted(f"{model}: суточный лимит бесплатных запросов")
                last = f"{model}: 429"
                self._sleep(3)
                continue
            if r.status_code in (401, 402, 403):
                raise LLMError(f"OpenRouter отказал ({r.status_code}): проверьте ключ и лимиты")
            if r.status_code >= 400:
                last = f"{model}: HTTP {r.status_code}"
                continue
            try:
                text = r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError, TypeError):
                text = ""
            if text.strip():
                return text
            last = f"{model}: пустой ответ"
        raise LLMError(f"ни одна модель не ответила ({last})")

    def json(
        self,
        messages: list[dict[str, str]],
        check: Callable[[Any], list[str]],
        *,
        max_tokens: int = 2500,
        retries: int = 1,
    ) -> Any:
        """JSON-ответ, прошедший check (возвращает список ошибок). При ошибках просит исправить —
        не чаще `retries` раз (каждая попытка тратит суточный лимит)."""
        msgs = list(messages)
        problems: list[str] = []
        for _ in range(retries + 1):
            text = self.complete(msgs, max_tokens=max_tokens)
            try:
                data = extract_json(text)
            except ValueError as e:
                problems = [str(e)]
            else:
                problems = check(data)
                if not problems:
                    return data
            msgs = [*messages, {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": "Ответ не принят: " + "; ".join(problems[:6]) + ". Верни исправленный JSON целиком, без пояснений."}]
        raise LLMError("ответ не прошёл проверку: " + "; ".join(problems[:6]))
