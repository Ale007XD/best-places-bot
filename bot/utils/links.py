# bot/utils/links.py
"""Ссылки на карту и «поделиться»."""

import re
from typing import Any
from urllib.parse import quote, quote_plus

from bot.services.translator import get_string


def _has_locality(addr: str) -> bool:
    """В адресе есть город/район: после улицы идёт ещё хотя бы один сегмент («…, Nha Trang»).
    Скобки («85 (Phạm Văn Chiêu), Hồ Chí Minh») в счёт сегментов не входят."""
    outside = re.sub(r"\([^)]*\)", " ", addr)
    return sum(1 for seg in outside.split(",") if seg.strip()) >= 2


def map_url(place: dict[str, Any]) -> str:
    """
    Ссылка на Google Maps.

    Радиуса или границ в Google Maps URL нет (только query/query_place_id), а `/@lat,lon,z`
    задаёт лишь центр карты: если по тексту ничего не нашлось рядом, Google ищет по всему
    миру («тёзка в Китае»). Поэтому по тексту «название, адрес[, город]» ищем, только когда
    в нём есть город; иначе — по координатам (точный пин, без тёзок, но без карточки Google).
    """
    # Подпись «Кафе без названия» — не название: ищем по координатам.
    name = "" if place.get("unnamed") else str(place.get("name") or "").strip()
    addr = str(place.get("vicinity") or "").strip()
    locality = str(place.get("locality") or "").strip()
    lat, lon = place.get("lat"), place.get("lon")

    parts = [x for x in (name, addr) if x]
    if locality and locality.casefold() not in addr.casefold():
        parts.append(locality)
    q = ", ".join(parts)

    if lat is None or lon is None:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"
    if not name or not (locality or _has_locality(addr)):
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    return f"https://www.google.com/maps/search/{quote(q, safe='')}/@{lat},{lon},18z"


def share_url(place: dict[str, Any], ref_link: str, lang_code: str) -> str:
    """t.me/share/url — открывает выбор получателя; в тексте название, адрес и ссылка на бота."""
    text = get_string("share_text", lang=lang_code).format(
        name=place.get("name") or "—",
        address=place.get("vicinity") or "",
        link=ref_link,
    ).rstrip(": ")
    return f"https://t.me/share/url?url={quote(map_url(place), safe='')}&text={quote(text, safe='')}"
