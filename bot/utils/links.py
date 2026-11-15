# bot/utils/links.py
"""Ссылки на карту и «поделиться»."""

from typing import Any
from urllib.parse import quote, quote_plus

from bot.services.translator import get_string


def map_url(place: dict[str, Any]) -> str:
    """
    Ссылка на Google Maps, которая открывает ЗАВЕДЕНИЕ по названию и адресу, а не голую точку.

    Раньше было `query=lat,lon` — Google показывает в таком случае просто координаты.
    Place ID Google у нас нет (данные Foursquare/OSM), поэтому ищем по тексту «название,
    адрес» и центрируем карту на координатах места (`/@lat,lon,18z`) — так находится именно
    это заведение, а не тёзка в другом городе.
    """
    name = str(place.get("name") or "").strip()
    addr = str(place.get("vicinity") or "").strip()
    lat, lon = place.get("lat"), place.get("lon")

    if lat is None or lon is None:
        q = ", ".join(x for x in (name, addr) if x)
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"
    if not name:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

    q = ", ".join(x for x in (name, addr) if x)
    return f"https://www.google.com/maps/search/{quote(q, safe='')}/@{lat},{lon},18z"


def share_url(place: dict[str, Any], ref_link: str, lang_code: str) -> str:
    """t.me/share/url — открывает выбор получателя; в тексте название, адрес и ссылка на бота."""
    text = get_string("share_text", lang=lang_code).format(
        name=place.get("name") or "—",
        address=place.get("vicinity") or "",
        link=ref_link,
    ).rstrip(": ")
    return f"https://t.me/share/url?url={quote(map_url(place), safe='')}&text={quote(text, safe='')}"
