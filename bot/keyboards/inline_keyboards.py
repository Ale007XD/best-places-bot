from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def get_radius_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора радиуса поиска."""
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard() -> InlineKeyboardMarkup:
    """Создает клавиатуру для выбора минимального рейтинга."""
    buttons = [
        [InlineKeyboardButton(text="4.5+", callback_data="rating_4.5")],
        [InlineKeyboardButton(text="4.8+", callback_data="rating_4.8")],
        [InlineKeyboardButton(text="Только 5.0", callback_data="rating_5.0")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_google_maps_link_button(place_id: str) -> InlineKeyboardMarkup:
    """Создает кнопку-ссылку на Google Карты для конкретного заведения."""
    url = f"https://www.google.com/maps/search/?api=1&query_place_id={place_id}"
    button = InlineKeyboardButton(text="Открыть в Google Картах", url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
