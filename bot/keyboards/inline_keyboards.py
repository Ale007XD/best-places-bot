# bot/keyboards/inline_keyboards.py
"""Клавиатуры: язык, экран параметров (радиус + рейтинг + «Искать»), результаты."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

RADII = (200, 500, 1000, 2000)
MIN_RATINGS = (0.0, 4.0, 4.5)  # 0.0 = «любой»


def get_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
            [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
            [InlineKeyboardButton(text="🇨🇳 简体中文", callback_data="lang_zh")],
        ]
    )


def get_location_keyboard(_) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=True,
        keyboard=[[KeyboardButton(text=_("send_location_btn"), request_location=True)]],
    )


def _mark(text: str, selected: bool) -> str:
    return f"✅ {text}" if selected else text


def get_params_keyboard(_, radius: int, min_rating: float) -> InlineKeyboardMarkup:
    unit = _("unit_m")
    radius_row = [
        InlineKeyboardButton(
            text=_mark(f"{r} {unit}", r == radius), callback_data=f"radius_{r}"
        )
        for r in RADII
    ]
    rating_row = [
        InlineKeyboardButton(
            text=_mark(
                _("rating_any_btn") if m == 0 else f"⭐️ {m:.1f}+",
                m == min_rating,
            ),
            callback_data=f"minrating_{m}",
        )
        for m in MIN_RATINGS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            radius_row,
            rating_row,
            [InlineKeyboardButton(text=_("search_btn"), callback_data="search_go")],
        ]
    )


def get_results_keyboard(_, places: list[dict]) -> InlineKeyboardMarkup:
    """Кнопка «открыть на карте» для каждого места + действия."""
    rows = []
    for i, p in enumerate(places, start=1):
        if p.get("lat") is None or p.get("lon") is None:
            continue
        name = str(p.get("name") or "—")
        name = name if len(name) <= 28 else name[:27] + "…"
        url = (
            "https://www.google.com/maps/search/?api=1&query="
            f"{p['lat']},{p['lon']}"
        )
        rows.append([InlineKeyboardButton(text=f"🗺 {i} · {name}", url=url)])
    rows.append(
        [
            InlineKeyboardButton(text=_("change_params_btn"), callback_data="change_params"),
            InlineKeyboardButton(text=_("new_location_btn"), callback_data="new_search"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_retry_keyboard(_) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("change_params_btn"), callback_data="change_params"
                ),
                InlineKeyboardButton(
                    text=_("new_location_btn"), callback_data="new_search"
                ),
            ]
        ]
    )
