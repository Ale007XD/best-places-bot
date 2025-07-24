from aiogram import Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.config import settings

router = Router()

class SearchSteps(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_rating = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    # ... этот обработчик без изменений ...
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
    # ... этот обработчик без изменений ...
    await state.update_data(latitude=message.location.latitude, longitude=message.location.longitude)
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    await message.answer("Теперь выберите радиус поиска:", reply_markup=inline_keyboards.get_radius_keyboard())
    await state.set_state(SearchSteps.waiting_for_radius)


@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext):
    # ... этот обработчик без изменений ...
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
    Ловит нажатие на кнопку с ДИАПАЗОНОМ рейтинга, применяет фильтр и отправляет результат.
    """
    # --- НОВАЯ ЛОГИКА ПАРСИНГА ДИАПАЗОНА ---
    parts = callback.data.split('_')
    min_rating = float(parts[1])
    max_rating = float(parts[2])
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    user_data = await state.get_data()
    await state.clear()
    
    await callback.message.edit_text("Ищу лучшие заведения для вас... 🕵️‍♂️")
    
    # Получаем ВСЕХ кандидатов с рейтингом ВЫШЕ нашего минимального
    all_candidates = await find_places(
        api_key=settings.GOOGLE_MAPS_API_KEY,
        lat=user_data['latitude'],
        lon=user_data['longitude'],
        radius=user_data['radius'],
        min_rating=min_rating # Передаем только нижнюю границу
    )
    
    # --- НОВАЯ ЛОГИКА ФИЛЬТРАЦИИ ПО ВЕРХНЕЙ ГРАНИЦЕ ---
    final_places = []
    for place in all_candidates:
        if place['rating'] <= max_rating:
            final_places.append(place)
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    await callback.message.delete()
    
    # Теперь мы работаем с нашим финальным, отфильтрованным списком
    if not final_places:
        await callback.message.answer(
            "К сожалению, в этом диапазоне ничего не найдено. 🙁\n"
            "Попробуйте выбрать другой диапазон. Для нового поиска введите /start"
        )
    else:
        await callback.message.answer("Вот что удалось найти:")
        
        # Берем до 3-х лучших из отфильтрованного списка
        for place in final_places[:3]:
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


@router.message(SearchSteps.waiting_for_location)
async def incorrect_location(message: Message):
    # ... этот обработчик без изменений ...
    await message.answer("Пожалуйста, нажмите на кнопку '📍 Отправить геопозицию', чтобы поделиться вашим местоположением.")
