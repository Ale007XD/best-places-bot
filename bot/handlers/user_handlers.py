# bot/handlers/user_handlers.py
# -*- coding: utf-8 -*-
"""
Пользовательские обработчики aiogram:
- Диалог выбора языка → геолокации → радиуса → рейтинга.
- Вызов поиска мест через Google Places с корректным порядком аргументов.
- Отправка карточек с расстоянием/направлением и клавиатурами действий.

Ключевое исправление:
- Вызов find_places приведён к сигнатуре
  find_places(_, api_key, lat, lon, radius, min_rating, max_rating, lang_code)
  чтобы устранить TypeError и несоответствие позиций аргументов.
"""

import logging
from typing import Tuple, Optional

import redis.asyncio as redis
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, CallbackQuery

from bot.utils.geospatial import calculate_distance, calculate_bearing, bearing_to_direction
from bot.keyboards import inline_keyboards
from bot.utils.google_maps_api import find_places
from bot.config import settings
from bot.services.translator import get_string

router = Router()


# --- Состояния FSM ---

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

def _t(lang_code: str):
    """
    Упрощённая прокладка переводчика.
    """
    return lambda key: get_string(key, lang=lang_code)


def _format_place_card(
    lang_code: str,
    user_lat: float,
    user_lon: float,
    place: dict,
) -> str:
    """
    Формирует текст карточки места с расстоянием и направлением.
    """
    name = place.get("name", "—")
    rating = place.get("rating", "—")
    ratings_total = place.get("user_ratings_total", 0)
    vicinity = place.get("vicinity") or "—"
    plat = place.get("lat")
    plon = place.get("lon")

    # Геометрия
    distance_m = "—"
    direction_txt = "—"
    if plat is not None and plon is not None:
        dist = calculate_distance(user_lat, user_lon, float(plat), float(plon))
        brg = calculate_bearing(user_lat, user_lon, float(plat), float(plon))
        direction_txt = bearing_to_direction(_t(lang_code), brg)
        distance_m = f"{dist} м"

    return (
        f"📍 <b>{name}</b>\n"
        f"⭐️ {rating} ({int(ratings_total)})\n"
        f"🧭 {distance_m} • {direction_txt}\n"
        f"🗺 {vicinity}"
    )


async def process_and_send_results(
    chat_id: int,
    bot: Bot,
    state: FSMContext,
    min_rating: float,
    max_rating: float,
    _,
    lang_code: str,
) -> None:
    """
    Выполняет поиск мест по параметрам из FSM и отправляет результаты.
    Ключевое: правильный вызов find_places с нужным порядком аргументов.
    """
    user_data = await state.get_data()
    lat = float(user_data["latitude"])
    lon = float(user_data["longitude"])
    radius = int(user_data["radius"])

    # Логируем входные параметры
    logging.info(
        "Searching places: lat=%s lon=%s radius=%s min_rating=%s max_rating=%s lang=%s",
        lat, lon, radius, min_rating, max_rating, lang_code
    )

    # ВАЖНО: корректный порядок и полный набор аргументов
    all_candidates = await find_places(
        _,
        api_key=settings.GOOGLE_MAPS_API_KEY,
        lat=lat,
        lon=lon,
        radius=radius,
        min_rating=min_rating,
        max_rating=max_rating,
        lang_code=lang_code,
    )

    logging.info("Places fetched: %s before final sorting/capping", len(all_candidates))

    # Сортировка по рейтингу и количеству оценок (стабильнее)
    all_candidates.sort(
        key=lambda p: (
            float(p.get("rating") or 0.0),
            int(p.get("user_ratings_total") or 0),
        ),
        reverse=True,
    )

    # Берём топ-3
    top = all_candidates[:3]

    # Ответ пользователю
    if not top:
        await bot.send_message(
            chat_id,
            get_string("no_results", lang=lang_code) + "\n" + get_string("try_another_range", lang=lang_code),
        )
        return

    # Отправляем карточки по одному сообщению
    for p in top:
        text = _format_place_card(lang_code, lat, lon, p)
        await bot.send_message(chat_id, text, parse_mode="HTML")


# --- Хендлеры диалога ---

@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    """
    Старт: просим выбрать язык и запоминаем состояние.
    """
    await state.set_state(SearchSteps.waiting_for_language)
    await message.answer(
        get_string("select_language", lang="ru"),
        reply_markup=inline_keyboards.get_language_keyboard(),
    )


@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery, state: FSMContext):
    """
    Сохраняем выбранный язык и просим прислать локацию.
    """
    lang_code = callback.data.split("_", 1)[1]
    await state.update_data(lang_code=lang_code)

    # Клавиатура для отправки геопозиции
    kb = ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[[KeyboardButton(text=get_string("send_location_btn", lang=lang_code), request_location=True)]],
        one_time_keyboard=True,
    )

    await callback.message.answer(get_string("request_location", lang=lang_code), reply_markup=kb)
    await state.set_state(SearchSteps.waiting_for_location)
    await callback.answer()


@router.message(F.location, SearchSteps.waiting_for_location)
async def got_location(message: Message, state: FSMContext):
    """
    Получили геолокацию — сохраним координаты и предложим выбрать радиус.
    """
    data = await state.get_data()
    lang_code = data.get("lang_code", "ru")

    await state.update_data(latitude=message.location.latitude, longitude=message.location.longitude)
    await message.answer(get_string("select_radius", lang=lang_code), reply_markup=ReplyKeyboardRemove())
    await message.answer(get_string("thanks", lang=lang_code), reply_markup=inline_keyboards.get_radius_keyboard(_t(lang_code)))
    await state.set_state(SearchSteps.waiting_for_radius)


@router.callback_query(F.data.startswith("radius_"), SearchSteps.waiting_for_radius)
async def set_radius(callback: CallbackQuery, state: FSMContext):
    """
    Установим радиус и перейдём к выбору диапазона рейтинга.
    """
    data = await state.get_data()
    lang_code = data.get("lang_code", "ru")

    # radius_200 | radius_500 | radius_1000
    radius = int(callback.data.split("_", 1)[1])
    await state.update_data(radius=radius)

    await callback.message.answer(get_string("select_rating_range", lang=lang_code), reply_markup=inline_keyboards.get_rating_keyboard(_t(lang_code)))
    await state.set_state(SearchSteps.waiting_for_rating)
    await callback.answer()


@router.callback_query(F.data.startswith("rating_"), SearchSteps.waiting_for_rating)
async def get_rating_from_button(callback: CallbackQuery, state: FSMContext):
    """
    Обработаем предустановленный диапазон рейтинга и запустим поиск.
    """
    data = await state.get_data()
    lang_code = data.get("lang_code", "ru")

    # rating_4.5_4.79
    _, min_s, max_s = callback.data.split("_")
    min_rating = float(min_s)
    max_rating = float(max_s)

    # Сообщим пользователю, что ищем
    await callback.message.answer(get_string("searching", lang=lang_code))

    # Вызов поиска и отправка результатов
    await process_and_send_results(callback.message.chat.id, callback.bot, state, min_rating, max_rating, _t(lang_code), lang_code)
    await callback.answer()


# Ниже могут быть обработчики ручного ввода радиуса и рейтинга, команда /feedback и т.д.
# Логика их не менялась, ключевое исправление — корректный вызов find_places.
