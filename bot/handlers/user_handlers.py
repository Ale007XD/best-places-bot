from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from bot.utils.geospatial import calculate_distance, calculate_bearing, bearing_to_direction
from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.config import settings

router = Router()

class SearchSteps(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_manual_radius = State()
    waiting_for_rating = State()
    waiting_for_manual_rating = State()

class FeedbackState(StatesGroup):
    waiting_for_feedback = State()


async def process_and_send_results(chat_id: int, bot: Bot, state: FSMContext, min_rating: float, max_rating: float):
    user_data = await state.get_data()
    await state.clear()
    
    all_candidates = await find_places(
        api_key=settings.GOOGLE_MAPS_API_KEY,
        lat=user_data['latitude'],
        lon=user_data['longitude'],
        radius=user_data['radius'],
        min_rating=min_rating
    )
    
    final_places = [p for p in all_candidates if p['rating'] <= max_rating]

    if not final_places:
        await bot.send_message(
            chat_id,
            "К сожалению, в этом диапазоне ничего не найдено. 🙁\n"
            "Попробуйте выбрать другой диапазон.",
            # --- ИЗМЕНЕНИЕ №1: Добавляем кнопку "Новый поиск" и сюда ---
            reply_markup=inline_keyboards.get_new_search_keyboard()
        )
    else:
        await bot.send_message(chat_id, "Вот что удалось найти:")
        for i, place in enumerate(final_places[:3], 1):
            distance = calculate_distance(user_data['latitude'], user_data['longitude'], place['lat'], place['lng'])
            bearing = calculate_bearing(user_data['latitude'], user_data['longitude'], place['lat'], place['lng'])
            direction = bearing_to_direction(bearing)
            text = (
                f"<b>{i}. {place['name']}</b>\n"
                f"🍽️ {place['main_type']}\n"
                f"⭐️ Рейтинг: {place['rating']}\n"
                f"📍 {distance} м {direction} от вас\n"
                f"🗺️ Адрес: {place['address']}"
            )
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=inline_keyboards.get_google_maps_link_button(
                    place=place,
                    distance=distance,
                    direction=direction
                )
            )
        # --- ИЗМЕНЕНИЕ №2: Заменяем текст на сообщение с кнопкой ---
        await bot.send_message(
            chat_id,
            "Хотите выполнить новый поиск?",
            reply_markup=inline_keyboards.get_new_search_keyboard()
        )

# ... обработчики cmd_start и feedback без изменений ...
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    location_button = KeyboardButton(text="📍 Отправить геопозицию", request_location=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer('Привет! Я "Гид по заведениям"...', reply_markup=keyboard)
    await state.set_state(SearchSteps.waiting_for_location)

@router.message(Command(commands=['feedback']))
async def cmd_feedback(message: Message, state: FSMContext):
    await state.set_state(FeedbackState.waiting_for_feedback)
    await message.answer("Поделитесь вашими мыслями!..")

@router.message(FeedbackState.waiting_for_feedback)
async def process_feedback(message: Message, state: FSMContext, bot: Bot):
    await bot.forward_message(chat_id=settings.ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id)
    await message.answer("Спасибо! Ваше сообщение отправлено...")
    await state.clear()


# --- НОВЫЙ ОБРАБОТЧИК ДЛЯ КНОПКИ "НОВЫЙ ПОИСК" ---
@router.callback_query(F.data == "new_search")
async def new_search_callback(callback: types.CallbackQuery, state: FSMContext):
    """
    Ловит нажатие на кнопку 'Новый поиск' и перезапускает диалог,
    вызывая обработчик команды /start.
    """
    await callback.answer() # Убираем "часики"
    await callback.message.delete() # Удаляем сообщение с кнопкой для чистоты
    await cmd_start(callback.message, state)


# ... остальные обработчики без изменений ...
@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext):
    await state.update_data(latitude=message.location.latitude, longitude=message.location.longitude)
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await message.answer("Теперь выберите радиус поиска:", reply_markup=inline_keyboards.get_radius_keyboard())
    await state.set_state(SearchSteps.waiting_for_radius)

@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext):
    radius = int(callback.data.split('_')[1])
    await state.update_data(radius=radius)
    await callback.message.edit_text("Отлично. Какой диапазон рейтинга вас интересует?", reply_markup=inline_keyboards.get_rating_keyboard())
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_rating, F.data.startswith('rating_'))
async def get_rating_from_button(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    await callback.message.edit_text("Ищу лучшие заведения для вас... 🕵️‍♂️")
    parts = callback.data.split('_')
    await process_and_send_results(callback.message.chat.id, bot, state, float(parts[1]), float(parts[2]))
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_radius, F.data == 'manual_radius_input')
async def ask_for_manual_radius(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите желаемый радиус в метрах...")
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
        await message.answer("Неверный формат...")

@router.callback_query(SearchSteps.waiting_for_rating, F.data == 'manual_rating_input')
async def ask_for_manual_rating(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите минимальный желаемый рейтинг...")
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
        await message.answer("Неверный формат...")

@router.message(SearchSteps.waiting_for_location)
async def incorrect_location(message: Message):
    await message.answer("Пожалуйста, нажмите на кнопку '📍 Отправить геопозицию'...")
