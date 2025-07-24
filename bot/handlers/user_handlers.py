from aiogram import Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# Импортируем наши модули
from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.config import settings

# Создаем роутер, который будет обрабатывать все команды и сообщения от пользователя
router = Router()

# Определяем состояния для машины состояний (FSM), чтобы вести пользователя по шагам
class SearchSteps(StatesGroup):
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_rating = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """
    Обработчик команды /start.
    Сбрасывает предыдущее состояние, приветствует пользователя и запрашивает геолокацию.
    """
    await state.clear()
    
    # Создаем reply-кнопку для удобного запроса геолокации
    location_button = KeyboardButton(text="📍 Отправить геопозицию", request_location=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True, one_time_keyboard=True)
    
    await message.answer(
        'Привет! Я "Гид по заведениям". Помогу найти лучшие кафе и рестораны рядом с вами.\n\n'
        'Чтобы начать, пожалуйста, отправьте вашу геолокацию.',
        reply_markup=keyboard
    )
    # Переводим бота в состояние ожидания геолокации
    await state.set_state(SearchSteps.waiting_for_location)


@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext):
    """
    Ловит сообщение с геолокацией, сохраняет координаты в FSM,
    убирает reply-клавиатуру и запрашивает радиус поиска.
    """
    await state.update_data(
        latitude=message.location.latitude,
        longitude=message.location.longitude
    )
    
    # Убираем клавиатуру с кнопкой геолокации, она больше не нужна
    await message.answer("Спасибо!", reply_markup=ReplyKeyboardRemove())
    
    await message.answer(
        "Теперь выберите радиус поиска:",
        reply_markup=inline_keyboards.get_radius_keyboard()
    )
    # Переводим бота в состояние ожидания радиуса
    await state.set_state(SearchSteps.waiting_for_radius)


@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext):
    """
    Ловит нажатие на инлайн-кнопку с радиусом, сохраняет его
    и запрашивает минимальный рейтинг.
    """
    radius = int(callback.data.split('_')[1])
    await state.update_data(radius=radius)
    
    # Редактируем предыдущее сообщение, заменяя клавиатуру
    await callback.message.edit_text(
        "Отлично. Какой минимальный рейтинг вас интересует?",
        reply_markup=inline_keyboards.get_rating_keyboard()
    )
    # Переводим бота в состояние ожидания рейтинга
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer() # Отвечаем на колбэк, чтобы убрать "часики"


@router.callback_query(SearchSteps.waiting_for_rating, F.data.startswith('rating_'))
async def get_rating_and_search(callback: types.CallbackQuery, state: FSMContext):
    """
    Ловит нажатие на кнопку с рейтингом, запускает поиск,
    обрабатывает результаты и отправляет их пользователю.
    """
    min_rating = float(callback.data.split('_')[1])
    user_data = await state.get_data()
    await state.clear() # Завершаем FSM, так как все данные собраны
    
    await callback.message.edit_text("Ищу лучшие заведения для вас... 🕵️‍♂️")
    
    # Вызываем нашу основную функцию поиска
    places = await find_places(
        api_key=settings.GOOGLE_MAPS_API_KEY,
        lat=user_data['latitude'],
        lon=user_data['longitude'],
        radius=user_data['radius'],
        min_rating=min_rating
    )
    
    await callback.message.delete() # Удаляем сообщение "Ищу..." для чистоты диалога
    
    if not places:
        await callback.message.answer(
            "К сожалению, по вашему запросу ничего не найдено. 🙁\n"
            "Попробуйте увеличить радиус или выбрать рейтинг пониже. Для нового поиска введите /start"
        )
    else:
        await callback.message.answer("Вот что удалось найти:")
        
        # --- КЛЮЧЕВОЙ МОМЕНТ ---
        # Итерируемся по списку найденных мест и отправляем отдельное сообщение для каждого.
        for place in places:
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
    """
    Обрабатывает некорректный ввод на шаге ожидания геолокации
    (например, если пользователь прислал текст).
    """
    await message.answer("Пожалуйста, нажмите на кнопку '📍 Отправить геопозицию', чтобы поделиться вашим местоположением.")
