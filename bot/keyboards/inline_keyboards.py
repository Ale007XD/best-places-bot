from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus

def get_language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇨🇳 简体中文", callback_data="lang_zh")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_radius_keyboard(_) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
        [InlineKeyboardButton(text=_("manual_input_btn"), callback_data="manual_radius_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard(_) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=_("rating_range_1"), callback_data="rating_4.5_4.79")],
        [InlineKeyboardButton(text=_("rating_range_2"), callback_data="rating_4.8_4.99")],
        [InlineKeyboardButton(text=_("rating_range_3"), callback_data="rating_5.0_5.0")],
        [InlineKeyboardButton(text=_("manual_input_btn"), callback_data="manual_rating_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_new_search_keyboard(_) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text=_("new_search_btn"), callback_data="new_search")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])

def get_google_maps_link_button(_, place: dict, distance: int, direction: str) -> InlineKeyboardMarkup:
    place_name = place['name']
    place_id = place['place_id']
    encoded_name = quote_plus(place_name)
    google_maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}&query_place_id={place_id}"
    share_text = _("share_text_template", place_name=place_name, main_type=place['main_type'], rating=place['rating'], distance=distance, direction=direction, google_maps_url=google_maps_url)
    buttons = [
        [InlineKeyboardButton(text=_("open_in_maps_btn"), url=google_maps_url)],
        [InlineKeyboardButton(text=_("share_find_btn"), switch_inline_query=share_text)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
