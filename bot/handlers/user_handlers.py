import logging
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from bot.utils.geospatial import calculate_distance, calculate_bearing, bearing_to_direction
from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.utils.analytics import Analytics # <-- Импорт аналитики
from bot.config import settings

router = Router()
analytics = Analytics() # <-- Создаем экземпляр для сбора статистики

# --- Определяем все состояния FSM ---
class SearchSteps(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_manual_radius = State()
    waiting_for_rating = State()
    waiting_for_manual_rating = State()

class FeedbackState(StatesGroup):
    waiting_for_feedback = State()


# --- Админ-панель для статистики ---
@router.message(Command(commands=['stats']), F.from_user.id == settings.ADMIN_ID)
async def cmd_stats(message: Message):
    """Отправляет администратору отчет по статистике за сегодня."""
    stats = await analytics.get_today_stats()
    
    radius_stats = "\n".join([f"  - {k} м: {v} раз" for k, v in stats['radius_usage'].items()]) or "  - нет данных"
    rating_stats = "\n".join([f"  - {k.replace('_', ' - ')}: {v} раз" for k, v in stats['rating_usage'].items()]) or "  - нет данных"

    text = (
        f"📊 *Статистика за сегодня:*\n\n"
        f"👤 *Активных пользователей:* {stats['active_users']}\n"
        f"🔍 *Всего поисков:* {stats['searches']}\n"
        f"🤷 *'Ничего не найдено':* {stats['empty_results']}\n"
        f"💡 *Запросов фидбэка:* {stats['feedback']}\n\n"
        f"*Использование фич:*\n"
        f"Радиусы:\n{radius_stats}\n\n"
        f"Рейтинги:\n{rating_stats}"
    )
    await message.answer(text, parse_mode="Markdown")


# --- Вспомогательная функция для поиска и отправки ---
async def process_and_send_results(chat_id: int, bot: Bot, state: FSMContext, min_rating: float, max_rating: float):
    user_data = await state.get_data()
    await state.clear()
    
    all_candidates = await find_places(settings.GOOGLE_MAPS_API_KEY, user_data['latitude'], user_data['longitude'], user_data['radius'], min_rating)
    final_places = [p for p in all_candidates if p['rating'] <= max_rating]

    if not final_places:
        await analytics.track_empty_result()
        await bot.send_message(chat_id, "К сожалению, в этом диапазоне ничего не найдено. 🙁", reply_markup=inline_keyboards.get_new_search_keyboard())
    else:
        await analytics.track_search_request()
        await bot.send_message(chat_id, "Вот что удалось найти:")
        for i, place in enumerate(final_places[:3], 1):
            distance = calculate_distance(user_data['latitude'], user_data['longitude'], place['lat'], place['lng'])
            bearing = calculate_bearing(user_data['latitude'], user_data['longitude'], place['lat'], place['lng'])
            direction = bearing_to_direction(bearing)
            text = (f"<b>{i}. {place['name']}</b>\n🍽️ {place['main_type']}\n⭐️ Рейтинг: {place['rating']}\n📍 {distance} м {direction} от вас\n🗺️ Адрес: {place['address']}")
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=inline_keyboards.get_google_maps_link_button(place, distance, direction))
            # Если у заведения есть саммари от Google, добавляем его
        if place.get('summary'):
            text += f"\n\n💬 *От Google:* «{place['summary']}»"
        await bot.send_message(chat_id, "Хотите выполнить новый поиск?", reply_markup=inline_keyboards.get_new_search_keyboard())


# --- Обработчики основного сценария ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await analytics.track_user(message.from_user.id)
    logging.info(f"Получена команда /start от пользователя ID: {message.from_user.id} ({message.from_user.full_name})") # лог статистики
    await state.clear()
    location_button = KeyboardButton(text="📍 Отправить геопозицию", request_location=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer('Привет! Я "Гид по заведениям". Помогу найти лучшие кафе и рестораны рядом с вами.\n\nЧтобы начать, пожалуйста, отправьте вашу геолокацию.', reply_markup=keyboard)
    await state.set_state(SearchSteps.waiting_for_location)

@router.message(Command(commands=['feedback']))
async def cmd_feedback(message: Message, state: FSMContext):
    await analytics.track_feedback_request()
    await state.set_state(FeedbackState.waiting_for_feedback)
    await message.answer("Поделитесь вашими мыслями! Что вам нравится, что можно улучшить, или, может, вы нашли ошибку? Просто отправьте ваше сообщение, и я передам его разработчику.")

@router.message(FeedbackState.waiting_for_feedback)
async def process_feedback(message: Message, state: FSMContext, bot: Bot):
    await bot.forward_message(chat_id=settings.ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id)
    await message.answer("Спасибо! Ваше сообщение отправлено. Вы очень помогаете сделать бота лучше! 👍\nДля нового поиска введите /start")
    await state.clear()

@router.callback_query(F.data == "new_search")
async def new_search_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete()
    await cmd_start(callback.message, state)

@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext):
    await state.update_data(latitude=message.location.latitude, longitude=message.location.longitude)
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await message.answer("Теперь выберите радиус поиска:", reply_markup=inline_keyboards.get_radius_keyboard())
    await state.set_state(SearchSteps.waiting_for_radius)

@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext):
    radius = int(callback.data.split('_')[1])
    await analytics.track_feature_use("radius", radius)
    await state.update_data(radius=radius)
    await callback.message.edit_text("Отлично. Какой диапазон рейтинга вас интересует?", reply_markup=inline_keyboards.get_rating_keyboard())
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_rating, F.data.startswith('rating_'))
async def get_rating_from_button(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    await callback.message.edit_text("Ищу лучшие заведения для вас... 🕵️‍♂️")
    parts = callback.data.split('_')
    rating_range = f"{parts[1]}_{parts[2]}"
    await analytics.track_feature_use("rating", rating_range)
    await process_and_send_results(callback.message.chat.id, bot, state, float(parts[1]), float(parts[2]))
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_radius, F.data == 'manual_radius_input')
async def ask_for_manual_radius(callback: types.CallbackQuery, state: FSMContext):
    await analytics.track_feature_use("radius", "manual")
    await callback.message.edit_text("Введите желаемый радиус в метрах (например: 350, макс. 5000).")
    await state.set_state(SearchSteps.waiting_for_manual_radius)
    await callback.answer()

@router.message(SearchSteps.waiting_for_manual_radius)
async def get_manual_radius(message: Message, state: FSMContext):
    try:
        radius = int(message.text)
        if not 1 <= radius <= 5000: raise ValueError("Радиус вне диапазона.")
        await state.update_data(radius=radius)
        await message.answer("Отлично. Какой диапазон рейтинга вас интересует?", reply_markup=inline_keyboards.get_rating_keyboard())
        await state.set_state(SearchSteps.waiting_for_rating)
    except (ValueError, TypeError):
        await message.answer("Неверный формат. Пожалуйста, введите целое число от 1 до 5000.")

@router.callback_query(SearchSteps.waiting_for_rating, F.data == 'manual_rating_input')
async def ask_for_manual_rating(callback: types.CallbackQuery, state: FSMContext):
    await analytics.track_feature_use("rating", "manual")
    await callback.message.edit_text("Введите минимальный желаемый рейтинг (например: 3.2 или 4).")
    await state.set_state(SearchSteps.waiting_for_manual_rating)
    await callback.answer()

@router.message(SearchSteps.waiting_for_manual_rating)
async def get_manual_rating(message: Message, state: FSMContext, bot: Bot):
    try:
        min_rating = float(message.text.replace(',', '.'))
        if not 1.0 <= min_rating <= 5.0: raise ValueError("Рейтинг вне диапазона.")
        loading_message = await message.answer("Ищу лучшие заведения для вас... 🕵️‍♂️")
        await process_and_send_results(message.chat.id, bot, state, min_rating, 5.0)
        await loading_message.delete()
    except (ValueError, TypeError):
        await message.answer("Неверный формат. Пожалуйста, введите число от 1.0 до 5.0.")

@router.message(SearchSteps.waiting_for_location)
async def incorrect_location(message: Message):
    await message.answer("Пожалуйста, нажмите на кнопку '📍 Отправить геопозицию'...")
