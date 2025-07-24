from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import quote_plus

def get_radius_keyboard() -> InlineKeyboardMarkup:
    # ... эта функция без изменений ...
    buttons = [
        [InlineKeyboardButton(text="50 м", callback_data="radius_50")],
        [InlineKeyboardButton(text="100 м", callback_data="radius_100")],
        [InlineKeyboardButton(text="200 м", callback_data="radius_200")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="manual_radius_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_rating_keyboard() -> InlineKeyboardMarkup:
    # ... эта функция без изменений ...
    buttons = [
        [InlineKeyboardButton(text="⭐️ 4.5 - 4.7", callback_data="rating_4.5_4.79")],
        [InlineKeyboardButton(text="⭐️ 4.8 - 4.9", callback_data="rating_4.8_4.99")],
        [InlineKeyboardButton(text="⭐️ Только 5.0", callback_data="rating_5.0_5.0")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="manual_rating_input")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_google_maps_link_button(place: dict, distance: int, direction: str) -> InlineKeyboardMarkup:
    """
    Создает кнопки для карточки заведения.
    Теперь принимает весь словарь 'place' и данные о расстоянии/направлении
    для создания богатого текста для 'switch_inline_query'.
    """
    place_name = place['name']
    place_id = place['place_id']

    # 1. Создаем URL для кнопки "Открыть в Google Картах"
    encoded_name = quote_plus(place_name)
    google_maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}&query_place_id={place_id}"

    # 2. --- НОВАЯ ЛОГИКА: ФОРМИРУЕМ ТЕКСТ ДЛЯ РЕПОСТА ---
    share_text = (
        f"Зацените, какую классную локацию я нашел(ла) с помощью @NearbyNinjaBot! 🤖\n\n"
        f"*{place['name']}*\n"
        f"🍽️ {place['main_type']} | ⭐️ Рейтинг: {place['rating']}\n"
        f"📍 {distance} м {direction}\n\n"
        f"[📍 Открыть в Google Картах]({google_maps_url})"
    )
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    # 3. Собираем итоговые кнопки
    buttons = [
        [InlineKeyboardButton(text="📍 Открыть в Google Картах", url=google_maps_url)],
        # Вставляем наш отформатированный текст в switch_inline_query
        [InlineKeyboardButton(text="🚀 Поделиться находкой!", switch_inline_query=share_text)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
