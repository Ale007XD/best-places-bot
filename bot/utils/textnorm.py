"""Нормализация названий для поиска: без диакритики, регистра и пунктуации.

«Phở» = «Pho» = «PHỞ», «Cơm Tấm» = «com tam». Диакритика снимается только у латиницы: тайские
знаки (гласные и тоны — комбинируемые символы) должны остаться, иначе слово разрушится.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

_LATIN_ONLY = re.compile(r"^[a-z0-9 ]+$")


def fold(text: Any) -> str:
    """«Cơm Tấm Sài Gòn!» → «com tam sai gon»; «ผัดไทย» → «ผัดไทย»."""
    out: list[str] = []
    last_latin = False
    for ch in unicodedata.normalize("NFKD", str(text or "")):
        if unicodedata.category(ch).startswith("M"):  # комбинируемый знак (Mn/Mc/Me)
            if last_latin:  # диакритика латинской буквы — убираем
                continue
            out.append(ch)  # знак чужого письма (тайский и т. п.) — сохраняем
            continue
        if ch in "đĐ":
            ch = "d"
        last_latin = unicodedata.name(ch, "").startswith("LATIN")
        out.append(ch.casefold() if ch.isalnum() else " ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def matches(haystack: Any, needles: Iterable[str]) -> bool:
    """Есть ли среди needles такая, что встречается в haystack.

    Латиница — целыми словами («pho» найдёт «Phở Hòa», но не «Phong Lan»); остальные письма
    (тайский не разделяет слова пробелами) — подстрокой. Сравнение после fold()."""
    hay = fold(haystack)
    if not hay:
        return False
    spaced = f" {hay} "
    for raw in needles:
        needle = fold(raw)
        if not needle:
            continue
        if _LATIN_ONLY.match(needle):
            if f" {needle} " in spaced:
                return True
        elif needle in hay:
            return True
    return False
