from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus # <-- ДОБАВИТЬ ЭТОТ ИМПОРТ

def get_radius_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора радиуса поиска (50 м, 100 м, 200 м)."""
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора минимального рейтинга (4.5+, 4.8+, 5.0)."""
    buttons = [
        [InlineKeyboardButton(text="4.5+", callback_data="rating_4.5")],
        [InlineKeyboardButton(text="4.8+", callback_data="rating_4.8")],
        [InlineKeyboardButton(text="Только 5.0", callback_data="rating_5.0")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_google_maps_link_button(place_id: str, place_name: str) -> InlineKeyboardMarkup:
    """
    Создает кнопку-ссылку на Google Карты для конкретного заведения.
    Теперь использует и название, и ID для максимальной точности.
    """
    # Кодируем название заведения для использования в URL
    encoded_name = quote_plus(place_name)
    
    # Создаем максимально надежный URL
    url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}&query_place_id={place_id}"
    
    button = InlineKeyboardButton(text="Открыть в Google Картах", url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
