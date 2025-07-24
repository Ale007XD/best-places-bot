from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus

def get_radius_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора радиуса с опцией ручного ввода."""
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="manual_radius_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора диапазона рейтинга с опцией ручного ввода."""
    buttons = [
        [InlineKeyboardButton(text="⭐️ 4.5 - 4.7", callback_data="rating_4.5_4.79")],
        [InlineKeyboardButton(text="⭐️ 4.8 - 4.9", callback_data="rating_4.8_4.99")],
        [InlineKeyboardButton(text="⭐️ Только 5.0", callback_data="rating_5.0_5.0")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="manual_rating_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_google_maps_link_button(place_id: str, place_name: str) -> InlineKeyboardMarkup:
    """Кнопки для карточки заведения: ссылка на карты и 'поделиться'."""
    encoded_name = quote_plus(place_name)
    url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}&query_place_id={place_id}"
    buttons = [
        [InlineKeyboardButton(text="📍 Открыть в Google Картах", url=url)],
        [InlineKeyboardButton(text="🚀 Поделиться находкой!", switch_inline_query=f"Нашел(а) крутое место: {place_name}!")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
