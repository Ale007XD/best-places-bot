# bot/utils/textfmt.py
"""Безопасный текст для карточек: чистка, усечение, экранирование под Telegram HTML."""

import html
import re
import unicodedata

# bidi-управляющие, нулевой ширины и BOM: ломают выравнивание и «переворачивают» строку.
# ZWJ (U+200D) оставляем — он нужен составным эмодзи.
_STRIP = dict.fromkeys(
    map(
        ord,
        "\u200b\u200c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff",
    ),
    None,
)
_WS = re.compile(r"\s+")
_TAIL = "\u200d\ufe0e\ufe0f"  # «хвосты» эмодзи, которые не должны оставаться после обрезки


def clean(value: object) -> str:
    """Название/адрес из внешнего источника → одна строка без невидимых и управляющих символов."""
    s = str(value or "").translate(_STRIP)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cs", "Co"))
    return _WS.sub(" ", s).strip()


def truncate(s: str, limit: int) -> str:
    """Обрезка до limit символов с «…». Не оставляет висящий ZWJ/вариационный селектор."""
    if limit <= 1:
        return "…" if s else ""
    if len(s) <= limit:
        return s
    cut = s[: limit - 1].rstrip()
    while cut and cut[-1] in _TAIL:
        cut = cut[:-1]
    return cut.rstrip() + "…"


def esc(s: str) -> str:
    """Экранирование для parse_mode=HTML: Telegram требует только < > &."""
    return html.escape(s, quote=False)
