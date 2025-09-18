from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus
from typing import Dict, Any

def get_language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇨🇳 简体中文", callback_data="lang_zh")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_radius_keyboard(manual_input_btn_text: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
        [InlineKeyboardButton(text=manual_input_btn_text, callback_data="manual_radius_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard(rating_range_1_text: str, rating_range_2_text: str, rating_range_3_text: str, manual_input_btn_text: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=rating_range_1_text, callback_data="rating_4.5_4.79")],
        [InlineKeyboardButton(text=rating_range_2_text, callback_data="rating_4.8_4.99")],
        [InlineKeyboardButton(text=rating_range_3_text, callback_data="rating_5.0_5.0")],
        [InlineKeyboardButton(text=manual_input_btn_text, callback_data="manual_rating_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_new_search_keyboard(new_search_btn_text: str) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text=new_search_btn_text, callback_data="new_search")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])

def get_google_maps_link_button(
    place: Dict[str, Any],
    share_text: str,
    open_in_maps_btn_text: str,
    share_find_btn_text: str
) -> InlineKeyboardMarkup:
    # Используем адрес для URL, если название не найдено, чтобы ссылка была более надежной
    query = place['name'] if place['name'] != 'name_not_found' else place['address']
    encoded_query = quote_plus(query)
    place_id = place['place_id']

    google_maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_query}&query_place_id={place_id}"

    buttons = [
        [InlineKeyboardButton(text=open_in_maps_btn_text, url=google_maps_url)],
        [InlineKeyboardButton(text=share_find_btn_text, switch_inline_query=share_text)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
