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


def get_params_keyboard(
    _,
    radius: int,
    min_rating: float,
    show_home: bool = False,
    rating_locked: bool = False,
) -> InlineKeyboardMarkup:
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
    if rating_locked:  # тизер использован: фильтр рейтинга недоступен — вместо него «открыть»
        rating_row = [InlineKeyboardButton(text=_("rating_locked_btn"), callback_data="unlock")]
    point_row = [InlineKeyboardButton(text=_("btn_other_point"), callback_data="pick_point")]
    if show_home:  # поиск идёт в выбранной точке — даём вернуться «рядом со мной»
        point_row.append(InlineKeyboardButton(text=_("btn_home"), callback_data="use_home"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            radius_row,
            rating_row,
            point_row,
            [InlineKeyboardButton(text=_("search_btn"), callback_data="search_go")],
        ]
    )


def get_results_actions_keyboard(_, show_unlock: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под заголовком выдачи (у самих карточек — свои кнопки, см. services/cards.py)."""
    rows = []
    if show_unlock:
        rows.append([InlineKeyboardButton(text=_("btn_unlock"), callback_data="unlock")])
    rows.append(
        [
            InlineKeyboardButton(text=_("change_params_btn"), callback_data="change_params"),
            InlineKeyboardButton(text=_("new_location_btn"), callback_data="new_search"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_compact_keyboard(
    _, places: list[dict], map_urls: list[str], share_urls: list[str]
) -> InlineKeyboardMarkup:
    """Режим «одно сообщение»: по ряду на место — 🗺 · 📤 · 👍 · 👎 (номер = номер карточки)."""
    rows = []
    for i, p in enumerate(places, start=1):
        row = [
            InlineKeyboardButton(text=f"🗺 {i}", url=map_urls[i - 1]),
            InlineKeyboardButton(text=f"📤 {i}", url=share_urls[i - 1]),
        ]
        pid = p.get("place_id")
        if pid and len(f"vu:{pid}".encode()) <= 64:  # лимит callback_data
            row += [
                InlineKeyboardButton(text=f"👍 {i}", callback_data=f"vu:{pid}"),
                InlineKeyboardButton(text=f"👎 {i}", callback_data=f"vd:{pid}"),
            ]
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text=_("change_params_btn"), callback_data="change_params"),
            InlineKeyboardButton(text=_("new_location_btn"), callback_data="new_search"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_geo_choices_keyboard(labels: list[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📍 {label}"[:60], callback_data=f"geo_{i}")]
            for i, label in enumerate(labels)
        ]
    )


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


def get_paywall_keyboard(
    _, prices: dict[str, int], invite_url: str, bonus: int
) -> InlineKeyboardMarkup:
    """Paywall / /buy: паки, Pro, заморозка серии и приглашение друга."""
    rows = []
    for key, price in prices.items():
        label_key = f"btn_{key}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=_(label_key).format(price=price), callback_data=f"buy_{key}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_("btn_invite").format(bonus=bonus), url=invite_url
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
