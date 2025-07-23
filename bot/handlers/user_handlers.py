from aiogram import Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.config import settings

# Создаем роутер для обработчиков
router = Router()

# Определяем состояния для машины состояний (FSM)
class SearchSteps(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_rating = State()

# --- Обработчики команд ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start."""
    await state.clear() # Сбрасываем состояние при новом старте
    
    location_button = KeyboardButton(text="📍 Отправить геопозицию", request_location=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True)
    
    await message.answer(
        "Привет! Я \"Гид по заведениям\". Помогу найти лучшие кафе и рестораны рядом с вами.\n\n"
        "Чтобы начать, пожалуйста, отправьте вашу геолокацию.",
        reply_markup=keyboard
    )
    await state.set_state(SearchSteps.waiting_for_location)

# --- Обработчики сообщений и колбэков по шагам FSM ---

@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext):
    """Ловит геолокацию пользователя."""
    await state.update_data(
        latitude=message.location.latitude,
        longitude=message.location.longitude
    )
    
    await message.answer(
        "Спасибо! Теперь выберите радиус поиска:",
        reply_markup=inline_keyboards.get_radius_keyboard()
    )
    # Убираем клавиатуру с кнопкой геолокации
    await message.answer("...", reply_markup=ReplyKeyboardRemove(), show_alert=False)

    await state.set_state(SearchSteps.waiting_for_radius)

@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext):
    """Ловит радиус поиска из инлайн-кнопки."""
    radius = int(callback.data.split('_')[1])
    await state.update_data(radius=radius)
    
    await callback.message.edit_text(
        "Отлично. Какой минимальный рейтинг вас интересует?",
        reply_markup=inline_keyboards.get_rating_keyboard()
    )
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_rating, F.data.startswith('rating_'))
async def get_rating_and_search(callback: types.CallbackQuery, state: FSMContext):
    """Ловит рейтинг, запускает поиск и отправляет результат."""
    min_rating = float(callback.data.split('_')[1])
    await state.update_data(min_rating=min_rating)
    
    user_data = await state.get_data()
    await state.clear()
    
    await callback.message.edit_text("Ищу лучшие заведения для вас... 🕵️‍♂️")
    
    places = await find_places(
        api_key=settings.GOOGLE_MAPS_API_KEY,
        lat=user_data['latitude'],
        lon=user_data['longitude'],
        radius=user_data['radius'],
        min_rating=min_rating
    )
    
    await callback.message.delete() # Удаляем сообщение "Ищу..."
    
    if not places:
        await callback.message.answer(
            "К сожалению, по вашему запросу ничего не найдено. 🙁\n"
            "Попробуйте увеличить радиус поиска. Для нового поиска введите /start"
        )
    else:
        await callback.message.answer("Вот что удалось найти:")
        for place in places:
            # Формируем сообщение для каждого заведения
            text = (
                f"<b>Название:</b> {place['name']}\n"
                f"<b>Рейтинг:</b> ⭐️ {place['rating']}\n"
                f"<b>Адрес:</b> {place['address']}"
            )
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=inline_keyboards.get_google_maps_link_button(
                place_id=place['place_id'], 
                place_name=place['name']
            )
        )
        await callback.message.answer("Хотите выполнить новый поиск? /start")

    await callback.answer()

# --- Обработчики некорректного ввода ---

@router.message(SearchSteps.waiting_for_location)
async def incorrect_location(message: Message):
    """Сообщает об ошибке, если вместо геолокации прислали что-то другое."""
    await message.answer("Пожалуйста, используйте кнопку '📍 Отправить геопозицию' для отправки вашей геолокации.")
