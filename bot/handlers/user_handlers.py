import redis.asyncio as redis
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
# Убираем экземпляр аналитики, так как он теперь не используется в этом файле
# analytics = Analytics()

# --- Определяем все состояния FSM ---
class SearchSteps(StatesGroup):
    waiting_for_language = State() # Новое состояние для первого выбора языка
    waiting_for_location = State()
    # ... остальные состояния

# ... (остальные классы FSM)

# --- ОБНОВЛЕННЫЙ `process_and_send_results` ---
async def process_and_send_results(chat_id: int, bot: Bot, state: FSMContext, min_rating: float, max_rating: float, _: callable):
    # ...
    # Передаем `_` во все внутренние вызовы
    distance_str = _("distance_template", distance=distance, direction=direction)
    text = _("card_template", i=i, place_name=place['name'], main_type=place['main_type'], rating=place['rating'], distance_str=distance_str, address=place['address'])
    # ...
    await bot.send_message(chat_id, _("new_search_prompt"), reply_markup=inline_keyboards.get_new_search_keyboard(_))

# --- ОБНОВЛЕННЫЙ `cmd_start` ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, _: callable):
    await state.clear()
    await message.answer(_("welcome_message"))
    await message.answer(_("select_language"), reply_markup=inline_keyboards.get_language_keyboard())
    await state.set_state(SearchSteps.waiting_for_language)

# --- НОВЫЕ ОБРАБОТЧИКИ ЯЗЫКА ---
@router.message(Command(commands=['language']))
async def cmd_language(message: Message, _: callable):
    await message.answer(_("select_language"), reply_markup=inline_keyboards.get_language_keyboard())

@router.callback_query(F.data.startswith("lang_"))
async def select_language(callback: types.CallbackQuery, state: FSMContext, redis_conn: redis.Redis):
    lang_code = callback.data.split("_")[1]
    await redis_conn.set(f"user_lang:{callback.from_user.id}", lang_code)
    
    # Отвечаем на новом языке
    _ = lambda key, **kwargs: get_string(key, lang_code).format(**kwargs)
    
    await callback.message.edit_text(_("language_selected"))
    
    # Если мы были в состоянии выбора языка, продолжаем диалог
    current_state = await state.get_state()
    if current_state == SearchSteps.waiting_for_language:
        location_button = KeyboardButton(text=_("send_location_btn"), request_location=True)
        keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True, one_time_keyboard=True)
        await callback.message.answer(_("request_location"), reply_markup=keyboard)
        await state.set_state(SearchSteps.waiting_for_location)
    
    await callback.answer()

# --- ВСЕ ОСТАЛЬНЫЕ ОБРАБОТЧИКИ ---
# Теперь они все должны принимать `_` и использовать его для текстов
# Пример:
@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext, _: callable):
    # ...
    await message.answer(_("thanks"), reply_markup=ReplyKeyboardRemove())
    await message.answer(_("select_radius"), reply_markup=inline_keyboards.get_radius_keyboard(_))
    # ...
