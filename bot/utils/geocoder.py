# bot/utils/geocoder.py
"""
Произвольная точка поиска из текста:
  1) координаты («12.2335, 109.1963», geo:12.2,109.1);
  2) ссылка на карту (Google Maps — в т.ч. короткая maps.app.goo.gl, OSM, Apple Maps);
  3) адрес или название места — через OpenStreetMap Nominatim (1 запрос/сек по их правилам,
     нужен User-Agent). Текст запроса уходит на сервер Nominatim — сказано в README.

Короткие ссылки раскрываются только для белого списка доменов (защита от SSRF), тело ответа
не скачивается — берётся только итоговый URL.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, NamedTuple
from urllib.parse import unquote

import httpx
from redis.exceptions import RedisError

from bot.config import settings
from bot.utils.http import get_client

logger = logging.getLogger(__name__)

_NUM = r"-?\d{1,3}\.\d+"
# Голые координаты в тексте — минимум 3 знака после точки, чтобы не принять «4.5 10.2» за точку.
_NUM3 = r"-?\d{1,3}\.\d{3,}"
_PLAIN = re.compile(rf"(?<![\d.])({_NUM3})\s*[,;/ ]\s*({_NUM3})(?![\d.])")
_URL_PATTERNS = (
    re.compile(rf"!3d({_NUM})!4d({_NUM})"),  # Google: координаты самого места
    re.compile(rf"@({_NUM}),({_NUM})"),  # Google: центр карты
    re.compile(rf"[?&](?:q|ll|query|destination|center|sll)=({_NUM})(?:,|%2C)({_NUM})", re.IGNORECASE),
    re.compile(rf"[?&]mlat=({_NUM})&mlon=({_NUM})", re.IGNORECASE),  # OSM
    re.compile(rf"#map=\d+/({_NUM})/({_NUM})"),  # OSM
    re.compile(rf"^geo:({_NUM}),({_NUM})", re.IGNORECASE),
)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co"}

_nominatim_lock = asyncio.Lock()
_last_call = 0.0


class Candidate(NamedTuple):
    lat: float
    lon: float
    label: str


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _parse_url_coords(url: str) -> tuple[float, float] | None:
    for _ in range(2):  # %40 → @, %2C → , (consent-редиректы Google кодируют дважды)
        for pat in _URL_PATTERNS:
            m = pat.search(url)
            if m and _valid(float(m.group(1)), float(m.group(2))):
                return float(m.group(1)), float(m.group(2))
        url = unquote(url)
    return None


async def _resolve_short(url: str) -> str | None:
    """Итоговый URL после редиректов — только для коротких ссылок Google Maps."""
    host = (httpx.URL(url).host or "").lower()
    if host not in _SHORT_HOSTS:
        return None
    try:
        async with get_client().stream(
            "GET", url, follow_redirects=True, timeout=6.0
        ) as r:
            return str(r.url)
    except httpx.HTTPError as e:
        logger.warning("short link resolve failed: %s", e)
        return None


async def parse_point(text: str) -> tuple[float, float] | None:
    """Координаты из текста / ссылки на карту; None — если это не координаты."""
    text = text.strip()
    m = _URL_RE.search(text)
    if m:
        url = m.group(0).rstrip(").,>")
        pt = _parse_url_coords(url)
        if pt:
            return pt
        final = await _resolve_short(url)
        if final:
            return _parse_url_coords(final)
        return None

    pt = _parse_url_coords(text)  # geo:lat,lon
    if pt:
        return pt
    m = _PLAIN.search(text)
    if m and _valid(float(m.group(1)), float(m.group(2))):
        return float(m.group(1)), float(m.group(2))
    return None


def _short_label(display_name: str) -> str:
    parts = [p.strip() for p in display_name.split(",") if p.strip()]
    label = ", ".join(parts[:2]) if parts else display_name
    return label if len(label) <= 60 else label[:59] + "…"


async def geocode(
    text: str,
    lang_code: str,
    redis_conn,
    near: tuple[float, float] | None = None,
    limit: int = 3,
) -> list[Candidate]:
    """Адрес/название → до `limit` кандидатов. Пустой список — не найдено ИЛИ сбой."""
    global _last_call
    query = text.strip()[:200]
    # Ключ учитывает окрестность пользователя (ячейка ~11 км — под размер viewbox) и limit:
    # иначе «Nguyễn Huệ» из одного города отдавалась бы жителю другого.
    near_cell = f"{round(near[0], 1)},{round(near[1], 1)}" if near else "-"
    raw_key = f"{query.lower()}|{lang_code}|{near_cell}|{limit}"
    cache_key = "geo:v2:" + hashlib.md5(raw_key.encode()).hexdigest()

    try:
        cached = await redis_conn.get(cache_key)
        if cached:
            return [Candidate(*c) for c in json.loads(cached)]
    except (RedisError, ValueError, TypeError) as e:
        logger.warning("geo cache read failed: %s", e)

    params: dict[str, Any] = {
        "q": query,
        "format": "jsonv2",
        "limit": limit,
        "accept-language": lang_code,
    }
    if near:  # мягкое смещение к окрестности пользователя, не жёсткое ограничение
        lat, lon = near
        params["viewbox"] = f"{lon - 0.6},{lat + 0.6},{lon + 0.6},{lat - 0.6}"
        params["bounded"] = 0

    async with _nominatim_lock:  # правило Nominatim: не чаще 1 запроса в секунду
        wait = 1.1 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            r = await get_client().get(
                settings.GEOCODER_URL,
                params=params,
                headers={"User-Agent": "best-places-bot/1.0"},
                timeout=8.0,
            )
            r.raise_for_status()
            rows = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("geocoder failed: %s", e)
            return []
        finally:
            _last_call = time.monotonic()

    out: list[Candidate] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if _valid(lat, lon):
            out.append(Candidate(lat, lon, _short_label(str(row.get("display_name") or query))))

    if out:
        try:
            await redis_conn.setex(cache_key, 86400, json.dumps(out))
        except (RedisError, TypeError) as e:
            logger.warning("geo cache write failed: %s", e)
    return out
