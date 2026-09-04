# bot/keyboards/inline_keyboards.py
# -*- coding: utf-8 -*-
"""
Инлайн-клавиатуры для выбора языка, радиуса, рейтинга и шаринга.
Расширены предустановки радиуса до 200/500/1000 м для практичного охвата.
"""

from urllib.parse import quote_plus

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_language_keyboard() -> InlineKeyboardMarkup:
    """
    Выбор языка интерфейса.
    """
    buttons = [
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇨🇳 简体中文", callback_data="lang_zh")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_radius_keyboard(_) -> InlineKeyboardMarkup:
    """
    Быстрый выбор радиуса поиска;
    """
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
        [
            InlineKeyboardButton(
                text=_("manual_input_btn"), callback_data="manual_radius_input"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_rating_keyboard(_) -> InlineKeyboardMarkup:
    """
    Предустановленные узкие диапазоны рейтинга + ручной ввод для гибкости.
    """
    buttons = [
        [
            InlineKeyboardButton(
                text=_("rating_range_1"), callback_data="rating_4.5_4.7"
            )
        ],
        [
            InlineKeyboardButton(
                text=_("rating_range_2"), callback_data="rating_4.8_4.9"
            )
        ],
        [
            InlineKeyboardButton(
                text=_("rating_range_3"), callback_data="rating_5.0_5.0"
            )
        ],
        [
            InlineKeyboardButton(
                text=_("manual_input_btn"), callback_data="manual_rating_input"
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_new_search_keyboard(_) -> InlineKeyboardMarkup:
    """
    Кнопка для запуска нового поиска.
    """
    buttons = [
        [InlineKeyboardButton(text=_("new_search_btn"), callback_data="new_search")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_share_keyboard(_, share_text: str, url: str) -> InlineKeyboardMarkup:
    """
    Клавиатура для шаринга найденного места в Телеграм.
    """
    tg_share_link = (
        f"https://t.me/share/url?text={quote_plus(share_text)}&url={quote_plus(url)}"
    )
    buttons = [
        [InlineKeyboardButton(text=_("share_find_btn"), url=tg_share_link)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
