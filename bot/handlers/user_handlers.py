from aiogram import Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# Импортируем наш новый модуль
from bot.utils.geospatial import calculate_distance, calculate_bearing, bearing_to_direction
from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.config import settings

router = Router()

# ... Класс SearchSteps без изменений ...
class SearchSteps(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_rating = State()

# ... cmd_start, get_location, get_radius без изменений ...
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    location_button = KeyboardButton(text="📍 Отправить геопозицию", request_location=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer(
        'Привет! Я "Гид по заведениям". Помогу найти лучшие кафе и рестораны рядом с вами.\n\n'
        'Чтобы начать, пожалуйста, отправьте вашу геолокацию.',
        reply_markup=keyboard
    )
    await state.set_state(SearchSteps.waiting_for_location)


@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext):
    await state.update_data(
        latitude=message.location.latitude,
        longitude=message.location.longitude
    )
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await message.answer("Теперь выберите радиус поиска:", reply_markup=inline_keyboards.get_radius_keyboard())
    await state.set_state(SearchSteps.waiting_for_radius)


@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext):
    radius = int(callback.data.split('_')[1])
    await state.update_data(radius=radius)
    await callback.message.edit_text(
        "Отлично. Какой диапазон рейтинга вас интересует?",
        reply_markup=inline_keyboards.get_rating_keyboard()
    )
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer()


@router.callback_query(SearchSteps.waiting_for_rating, F.data.startswith('rating_'))
async def get_rating_and_search(callback: types.CallbackQuery, state: FSMContext):
    """
    Основной обработчик, который теперь вычисляет расстояние/направление
    и формирует новую карточку.
    """
    parts = callback.data.split('_')
    min_rating = float(parts[1])
    max_rating = float(parts[2])
    
    user_data = await state.get_data()
    await state.clear()
    
    await callback.message.edit_text("Ищу лучшие заведения для вас... 🕵️‍♂️")
    
    all_candidates = await find_places(
        api_key=settings.GOOGLE_MAPS_API_KEY,
        lat=user_data['latitude'],
        lon=user_data['longitude'],
        radius=user_data['radius'],
        min_rating=min_rating
    )
    
    final_places = [p for p in all_candidates if p['rating'] <= max_rating]

    await callback.message.delete()
    
    if not final_places:
        await callback.message.answer(
            "К сожалению, в этом диапазоне ничего не найдено. 🙁\n"
            "Попробуйте выбрать другой диапазон. Для нового поиска введите /start"
        )
    else:
        await callback.message.answer("Вот что удалось найти:")
        
        for i, place in enumerate(final_places[:3], 1):
            # --- НОВАЯ ЛОГИКА ФОРМИРОВАНИЯ КАРТОЧКИ ---
            distance = calculate_distance(
                lat1=user_data['latitude'], lon1=user_data['longitude'],
                lat2=place['lat'], lon2=place['lng']
            )
            bearing = calculate_bearing(
                lat1=user_data['latitude'], lon1=user_data['longitude'],
                lat2=place['lat'], lon2=place['lng']
            )
            direction = bearing_to_direction(bearing)
            
            # Собираем красивую карточку
            text = (
                f"<b>{i}. {place['name']}</b>\n"
                f"🍽️ {place['main_type']}\n"
                f"⭐️ Рейтинг: {place['rating']}\n"
                f"📍 {distance} м {direction} от вас\n"
                f"🗺️ Адрес: {place['address']}"
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


@router.message(SearchSteps.waiting_for_location)
async def incorrect_location(message: Message):
    await message.answer("Пожалуйста, нажмите на кнопку '📍 Отправить геопозицию', чтобы поделиться вашим местоположением.")
