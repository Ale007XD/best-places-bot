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


_WORD = re.compile(r"\w+")


def _spelled(text: Any) -> list[str]:
    """Слова в исходном написании (NFC, без регистра): «Phở Hòa» → [«phở», «hòa»]."""
    return _WORD.findall(unicodedata.normalize("NFC", str(text or "")).casefold())


def matches(haystack: Any, needles: Iterable[str], spellings: Iterable[str] = ()) -> bool:
    """Есть ли среди needles такая, что встречается в haystack.

    Латиница — целыми словами («pho» найдёт «Phở Hòa», но не «Phong Lan»); остальные письма
    (тайский не разделяет слова пробелами) — подстрокой. Сравнение после fold().

    spellings — написания блюда с диакритикой («Phở bò», «Bún chả»). Без них диакритика не
    различается вовсе: «Phố» (улица) = «Phở» (суп) = «pho». С ними слово названия, совпавшее с
    латинской иглой, должно быть либо без диакритики («Pho 10»), либо записано так же, как в одном
    из spellings («Phở Hòa»); чужие знаки («Phố», «Phó») — другое слово, это не блюдо."""
    hay = fold(haystack)
    if not hay:
        return False
    spaced = f" {hay} "
    allowed = {w for sp in spellings for w in _spelled(sp)}
    words = _spelled(haystack) if allowed else []
    folded = [fold(w) for w in words]
    for raw in needles:
        needle = fold(raw)
        if not needle:
            continue
        if _LATIN_ONLY.match(needle):
            if f" {needle} " not in spaced:
                continue
            if not allowed or _marks_ok(needle.split(), words, folded, allowed):
                return True
        elif needle in hay:
            return True
    return False


def _marks_ok(need: list[str], words: list[str], folded: list[str], allowed: set[str]) -> bool:
    """Хотя бы одно вхождение иглы (по словам), где каждое слово — без диакритики или «своё»."""
    n = len(need)
    for i in range(len(folded) - n + 1):
        if folded[i : i + n] != need:
            continue
        if all(w == f or w in allowed for w, f in zip(words[i : i + n], folded[i : i + n], strict=True)):
            return True
    return False
