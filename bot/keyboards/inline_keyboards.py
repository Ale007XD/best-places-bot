from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus

def get_radius_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора радиуса поиска."""
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard() -> InlineKeyboardMarkup:
    """
    Создает клавиатуру для выбора ДИАПАЗОНА рейтинга.
    Callback_data теперь содержит min и max значения.
    """
    buttons = [
        [InlineKeyboardButton(text="⭐️ 4.5 - 4.7", callback_data="rating_4.5_4.79")],
        [InlineKeyboardButton(text="⭐️ 4.8 - 4.9", callback_data="rating_4.8_4.99")],
        [InlineKeyboardButton(text="⭐️ Только 5.0", callback_data="rating_5.0_5.0")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_google_maps_link_button(place_id: str, place_name: str) -> InlineKeyboardMarkup:
    """Создает кнопку-ссылку на Google Карты для конкретного заведения."""
    encoded_name = quote_plus(place_name)
    url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}&query_place_id={place_id}"
    button = InlineKeyboardButton(text="Открыть в Google Картах", url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
