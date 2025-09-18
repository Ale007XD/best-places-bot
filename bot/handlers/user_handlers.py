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
from bot.services.translator import get_string

router = Router()

class SearchSteps(StatesGroup):
    waiting_for_language = State()
    waiting_for_location = State()
    waiting_for_radius = State()
    waiting_for_manual_radius = State()
    waiting_for_rating = State()
    waiting_for_manual_rating = State()

class FeedbackState(StatesGroup):
    waiting_for_feedback = State()

# --- Вспомогательные функции ---

async def start_dialog(message: Message, state: FSMContext, _: callable):
    await state.clear()
    await message.answer(_("start_onboarding_message"), parse_mode="HTML")
    await message.answer(_("select_language"), reply_markup=inline_keyboards.get_language_keyboard())
    await state.set_state(SearchSteps.waiting_for_language)

async def request_location(message: Message, state: FSMContext, _: callable):
    location_button = KeyboardButton(text=_("send_location_btn"), request_location=True)
    keyboard = ReplyKeyboardMarkup(keyboard=[[location_button]], resize_keyboard=True, one_time_keyboard=True)
    full_text = _("request_location") + "\n\n" + _("location_privacy_info")
    await message.answer(full_text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(SearchSteps.waiting_for_location)

async def process_and_send_results(chat_id: int, bot: Bot, state: FSMContext, min_rating: float, max_rating: float, _: callable, lang_code: str):
    user_data = await state.get_data()
    lang = user_data.get("lang_code", lang_code)
    await state.clear()
    if lang:
        await state.set_data({"lang_code": lang})

    all_candidates = await find_places(lang, settings.GOOGLE_MAPS_API_KEY, user_data['latitude'], user_data['longitude'], user_data['radius'], min_rating)
    final_places = [p for p in all_candidates if p['rating'] <= max_rating]

    if not final_places:
        new_search_keyboard = inline_keyboards.get_new_search_keyboard(_("new_search_btn"))
        await bot.send_message(chat_id, _("no_results") + "\n" + _("try_another_range"), reply_markup=new_search_keyboard)
    else:
        await bot.send_message(chat_id, _("found_results"))
        for i, place in enumerate(final_places[:3], 1):
            # 1. Переводим все текстовые данные перед отправкой
            place_name = _(place['name'])
            address = _(place['address'])
            main_type = _(f"type_{place['main_type']}")
            direction_key = bearing_to_direction(calculate_bearing(user_data['latitude'], user_data['longitude'], place['lat'], place['lng']))
            direction_str = _(direction_key)
            distance = calculate_distance(user_data['latitude'], user_data['longitude'], place['lat'], place['lng'])
            distance_str = _("distance_template", distance=distance, direction=direction_str)

            # 2. Собираем карточку с переведенными данными
            text = _("card_template", i=i, place_name=place_name, main_type=main_type, rating=place['rating'], distance_str=distance_str, address=address)

            # 3. Собираем текст для кнопки "Поделиться"
            share_text = _(
                "share_text_template",
                place_name=place_name,
                main_type=main_type,
                rating=place['rating'],
                distance=distance,
                direction=direction_str
            )

            # 4. Создаем клавиатуру с переведенными кнопками
            maps_link_keyboard = inline_keyboards.get_google_maps_link_button(
                place=place,
                share_text=share_text,
                open_in_maps_btn_text=_("open_in_maps_btn"),
                share_find_btn_text=_("share_find_btn")
            )
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=maps_link_keyboard)

        new_search_keyboard = inline_keyboards.get_new_search_keyboard(_("new_search_btn"))
        await bot.send_message(chat_id, _("new_search_prompt"), reply_markup=new_search_keyboard)


# --- Обработчики команд и основного сценария ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, _: callable):
    await start_dialog(message, state, _)

@router.message(Command(commands=['language']))
async def cmd_language(message: Message, state: FSMContext, _: callable):
    await message.answer(_("select_language"), reply_markup=inline_keyboards.get_language_keyboard())
    await state.set_state(SearchSteps.waiting_for_language)

@router.callback_query(F.data.startswith("lang_"))
async def select_language(callback: types.CallbackQuery, state: FSMContext, redis_conn: redis.Redis):
    lang_code = callback.data.split("_")[1]
    await redis_conn.set(f"user_lang:{callback.from_user.id}", lang_code)
    await state.set_data({"lang_code": lang_code})

    _ = lambda key, **kwargs: get_string(key, lang_code).format(**kwargs)

    await callback.message.edit_text(_("language_selected"))
    await request_location(callback.message, state, _)
    await callback.answer()

@router.message(Command(commands=['feedback']))
async def cmd_feedback(message: Message, state: FSMContext, _: callable):
    await state.set_state(FeedbackState.waiting_for_feedback)
    await message.answer(_("feedback_prompt"))

@router.message(FeedbackState.waiting_for_feedback)
async def process_feedback(message: Message, state: FSMContext, bot: Bot, _: callable):
    await bot.forward_message(chat_id=settings.ADMIN_ID, from_chat_id=message.chat.id, message_id=message.message_id)
    await message.answer(_("feedback_thanks"))
    await state.clear()

@router.callback_query(F.data == "new_search")
async def new_search_callback(callback: types.CallbackQuery, state: FSMContext, _: callable, lang_code: str):
    await callback.answer()
    await callback.message.delete()

    data = await state.get_data()
    current_lang = data.get("lang_code", lang_code)
    await state.clear()
    await state.set_data({"lang_code": current_lang})

    _ = lambda key, **kwargs: get_string(key, current_lang).format(**kwargs)

    await request_location(callback.message, state, _)

@router.message(SearchSteps.waiting_for_location, F.location)
async def get_location(message: Message, state: FSMContext, _: callable):
    await state.update_data(latitude=message.location.latitude, longitude=message.location.longitude)
    await message.answer(_("thanks"), reply_markup=ReplyKeyboardRemove())
    radius_keyboard = inline_keyboards.get_radius_keyboard(_("manual_input_btn"))
    await message.answer(_("select_radius"), reply_markup=radius_keyboard)
    await state.set_state(SearchSteps.waiting_for_radius)

@router.callback_query(SearchSteps.waiting_for_radius, F.data.startswith('radius_'))
async def get_radius(callback: types.CallbackQuery, state: FSMContext, _: callable):
    radius = int(callback.data.split('_')[1])
    await state.update_data(radius=radius)
    rating_keyboard = inline_keyboards.get_rating_keyboard(
        _("rating_range_1"), _("rating_range_2"), _("rating_range_3"), _("manual_input_btn")
    )
    await callback.message.edit_text(_("select_rating_range"), reply_markup=rating_keyboard)
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_rating, F.data.startswith('rating_'))
async def get_rating_from_button(callback: types.CallbackQuery, state: FSMContext, bot: Bot, _: callable, lang_code: str):
    await callback.message.edit_text(_("searching"))
    parts = callback.data.split('_')
    await process_and_send_results(callback.message.chat.id, bot, state, float(parts[1]), float(parts[2]), _, lang_code)
    await callback.answer()

@router.callback_query(SearchSteps.waiting_for_radius, F.data == 'manual_radius_input')
async def ask_for_manual_radius(callback: types.CallbackQuery, state: FSMContext, _: callable):
    await callback.message.edit_text(_("manual_radius_prompt"))
    await state.set_state(SearchSteps.waiting_for_manual_radius)
    await callback.answer()

@router.message(SearchSteps.waiting_for_manual_radius)
async def get_manual_radius(message: Message, state: FSMContext, _: callable):
    try:
        radius = int(message.text)
        if not 1 <= radius <= 5000:
            raise ValueError("Radius out of range.")
        await state.update_data(radius=radius)
        rating_keyboard = inline_keyboards.get_rating_keyboard(
            _("rating_range_1"), _("rating_range_2"), _("rating_range_3"), _("manual_input_btn")
        )
        await message.answer(_("select_rating_range"), reply_markup=rating_keyboard)
        await state.set_state(SearchSteps.waiting_for_rating)
    except (ValueError, TypeError):
        await message.answer(_("manual_radius_error"))

@router.callback_query(SearchSteps.waiting_for_rating, F.data == 'manual_rating_input')
async def ask_for_manual_rating(callback: types.CallbackQuery, state: FSMContext, _: callable):
    await callback.message.edit_text(_("manual_rating_prompt"))
    await state.set_state(SearchSteps.waiting_for_manual_rating)
    await callback.answer()

@router.message(SearchSteps.waiting_for_manual_rating)
async def get_manual_rating(message: Message, state: FSMContext, bot: Bot, _: callable, lang_code: str):
    try:
        min_rating = float(message.text.replace(',', '.'))
        if not 1.0 <= min_rating <= 5.0:
            raise ValueError("Rating out of range.")
        loading_message = await message.answer(_("searching"))
        await process_and_send_results(message.chat.id, bot, state, min_rating, 5.0, _, lang_code)
        await loading_message.delete()
    except (ValueError, TypeError):
        await message.answer(_("manual_rating_error"))

@router.message(SearchSteps.waiting_for_location)
async def incorrect_location(message: Message, state: FSMContext, _: callable):
    await request_location(message, state, _)
