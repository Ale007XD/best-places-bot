# bot/handlers/account_handlers.py
"""
Аккаунт и монетизация: /me, /buy, paywall, оплата Telegram Stars (XTR),
/paysupport, /terms и админский /refund.

Роутер подключается ПЕРЕД user_handlers (у того в конце есть catch-all).
"""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from bot.config import settings
from bot.keyboards import inline_keyboards as kb
from bot.services import products, quota
from bot.services.translator import get_string

logger = logging.getLogger(__name__)

router = Router()

_username_cache: str | None = None


def _t(lang_code: str):
    return lambda key: get_string(key, lang=lang_code)


async def bot_username(bot: Bot) -> str:
    global _username_cache
    if _username_cache is None:
        _username_cache = (await bot.me()).username or ""
    return _username_cache


async def ref_link(bot: Bot, uid: int) -> str:
    return f"https://t.me/{await bot_username(bot)}?start=ref_{uid}"


async def invite_url(bot: Bot, uid: int, lang_code: str) -> str:
    text = get_string("invite_text", lang=lang_code)
    return (
        f"https://t.me/share/url?url={quote(await ref_link(bot, uid))}"
        f"&text={quote(text)}"
    )


def fmt_date(ts: int) -> str:
    tz_now = datetime.fromtimestamp(ts, UTC) + timedelta(
        hours=settings.QUOTA_UTC_OFFSET_HOURS
    )
    return tz_now.strftime("%d.%m.%Y")


def format_footer(lang_code: str, status: quota.Status, streak_days: int) -> str:
    """Одна строка под результатом: сколько осталось и серия."""
    if status.unlimited:
        return ""
    _ = _t(lang_code)
    if status.is_pro:
        return _("quota_footer_pro").format(
            date=fmt_date(status.pro_until), left=status.left_total, days=streak_days
        )
    return _("quota_footer").format(left=status.left_total, days=streak_days)


def _prices() -> dict[str, int]:
    return {key: products.price(key) for key in products.PRODUCT_KEYS}


async def show_paywall(
    message: Message, bot: Bot, lang_code: str, uid: int, header_key: str
) -> None:
    _ = _t(lang_code)
    await message.answer(
        _(header_key),
        reply_markup=kb.get_paywall_keyboard(
            _,
            _prices(),
            await invite_url(bot, uid, lang_code),
            settings.REFERRAL_BONUS,
        ),
    )


# --- Команды ---


@router.message(Command("me"))
async def cmd_me(message: Message, bot: Bot, lang_code: str, redis_conn):
    if message.from_user is None:
        return
    uid = message.from_user.id
    _ = _t(lang_code)
    st = await quota.get_status(redis_conn, uid)
    plan = (
        _("plan_pro").format(date=fmt_date(st.pro_until)) if st.is_pro else _("plan_free")
    )
    if not st.money_enabled:
        text = _("me_text_nolimit").format(
            days=st.streak_days,
            refs=await quota.referral_count(redis_conn, uid),
            link=await ref_link(bot, uid),
        )
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
        return
    text = _("me_text").format(
        plan=plan,
        free_left=st.free_left,
        limit=st.limit,
        credits=st.credits,
        days=st.streak_days,
        bonus=st.streak_bonus,
        freezes=st.freezes,
        refs=await quota.referral_count(redis_conn, uid),
        link=await ref_link(bot, uid),
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("buy"))
async def cmd_buy(message: Message, bot: Bot, lang_code: str, redis_conn):
    if message.from_user is None:
        return
    if not (await quota.get_config(redis_conn)).enabled:
        await message.answer(get_string("buy_disabled", lang=lang_code))
        return
    await show_paywall(message, bot, lang_code, message.from_user.id, "buy_header")


def _is_admin(message: Message) -> bool:
    return bool(
        settings.ADMIN_ID and message.from_user and message.from_user.id == settings.ADMIN_ID
    )


def _money_status_text(cfg: quota.Config) -> str:
    return (
        f"💰 Монетизация: {'ВКЛ ✅' if cfg.enabled else 'ВЫКЛ ⛔️'}\n"
        f"⭐ Бесплатных поисков С РЕЙТИНГАМИ и фото в день (тизер): {cfg.free_per_day} (+ бонус серии до "
        f"{settings.STREAK_BONUS_CAP})\n"
        f"🥷 Админ без лимитов: {'да' if cfg.admin_exempt else 'нет (тест paywall)'}\n\n"
        "/money on · /money off · /money free N · /money exempt on|off"
    )


@router.message(Command("money"))
async def cmd_money(message: Message, command: CommandObject, redis_conn):
    """Админ: управление монетизацией на лету (хранится в Redis, переживает перезапуск)."""
    if not _is_admin(message):
        return
    args = (command.args or "").lower().split()
    cfg = await quota.get_config(redis_conn)

    if args[:1] == ["on"]:
        cfg = await quota.set_config(redis_conn, enabled=True)
    elif args[:1] == ["off"]:
        cfg = await quota.set_config(redis_conn, enabled=False)
    elif args[:1] == ["free"] and len(args) == 2 and args[1].isdigit():
        cfg = await quota.set_config(redis_conn, free=int(args[1]))
    elif args[:1] == ["exempt"] and len(args) == 2 and args[1] in ("on", "off"):
        cfg = await quota.set_config(redis_conn, exempt=args[1] == "on")
    elif args and args[0] != "status":
        await message.answer("Не понял.\n\n" + _money_status_text(cfg))
        return
    await message.answer(_money_status_text(cfg))


@router.callback_query(F.data == "unlock")
async def cb_unlock(callback: CallbackQuery, bot: Bot, lang_code: str, redis_conn):
    """«🔒 Рейтинги и фото»: показываем, чем открыть (Stars / бонусные поиски / приглашение)."""
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if not (await quota.get_config(redis_conn)).enabled:
        await msg.answer(get_string("buy_disabled", lang=lang_code))
        return
    await show_paywall(msg, bot, lang_code, callback.from_user.id, "unlock_text")


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, lang_code: str):
    await message.answer(get_string("paysupport_text", lang=lang_code))


@router.message(Command("terms"))
async def cmd_terms(message: Message, lang_code: str):
    await message.answer(get_string("terms_text", lang=lang_code))


@router.message(Command("refund"))
async def cmd_refund(
    message: Message, command: CommandObject, bot: Bot, redis_conn
):
    """Админ: /refund <telegram_payment_charge_id>. Товар автоматически НЕ отзывается."""
    if (
        not settings.ADMIN_ID
        or message.from_user is None
        or message.from_user.id != settings.ADMIN_ID
    ):
        return
    charge_id = (command.args or "").strip()
    record = await redis_conn.get(f"payment:{charge_id}") if charge_id else None
    if not record:
        await message.answer("Usage: /refund <charge_id> (платёж не найден)")
        return
    uid, product, _amount = record.split(":")
    try:
        await bot.refund_star_payment(
            user_id=int(uid), telegram_payment_charge_id=charge_id
        )
    except TelegramAPIError as e:
        await message.answer(f"Refund failed: {e}")
        return
    await message.answer(f"✅ Refunded {charge_id} ({product}, user {uid}). Товар не отозван.")


# --- Покупка ---


@router.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: CallbackQuery, lang_code: str):
    key = (callback.data or "")[4:]
    msg = callback.message if isinstance(callback.message, Message) else None
    if key not in products.PRODUCT_KEYS or msg is None:
        await callback.answer()
        return
    await callback.answer()

    _ = _t(lang_code)
    title_key, desc_key = f"prod_{key}_title", f"prod_{key}_desc"
    title = _(title_key)
    try:
        await msg.answer_invoice(
            title=title,
            description=_(desc_key),
            payload=f"{key}:{callback.from_user.id}",
            currency="XTR",  # Stars; provider_token для XTR не нужен
            prices=[LabeledPrice(label=title, amount=products.price(key))],
        )
    except TelegramAPIError:
        logger.exception("invoice failed")
        await msg.answer(_("invoice_error"))


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Нужно ответить за 10 секунд: проверяем, что заказ соответствует каталогу."""
    key, _sep, uid = query.invoice_payload.partition(":")
    ok = (
        key in products.PRODUCT_KEYS
        and uid == str(query.from_user.id)
        and query.currency == "XTR"
        and query.total_amount == products.price(key)
    )
    await query.answer(ok=ok, error_message=None if ok else "Invalid order")


@router.message(F.successful_payment)
async def successful_payment(
    message: Message, lang_code: str, redis_conn, analytics=None
):
    sp = message.successful_payment
    if sp is None or message.from_user is None:
        return
    key, _sep, uid_s = sp.invoice_payload.partition(":")
    uid = message.from_user.id
    if key not in products.PRODUCT_KEYS or uid_s != str(uid):
        logger.error("payment with unexpected payload: %s", sp.invoice_payload)
        return

    # Идемпотентность: Telegram может повторить доставку апдейта.
    first = await redis_conn.set(
        f"payment:{sp.telegram_payment_charge_id}",
        f"{uid}:{key}:{sp.total_amount}",
        nx=True,
    )
    if not first:
        return

    granted = await products.grant(redis_conn, uid, key)
    logger.info("PAID %s uid=%s stars=%s", key, uid, sp.total_amount)
    if analytics:
        await analytics.track_purchase(key, sp.total_amount)

    _ = _t(lang_code)
    if key == "pack20":
        text = _("pay_thanks_pack20").format(**granted)
    elif key == "freeze":
        text = _("pay_thanks_freeze").format(**granted)
    else:
        text = _("pay_thanks_pro").format(date=fmt_date(granted["pro_until"]))
    await message.answer(text)


async def notify_referrer(bot: Bot, redis_conn, referrer_id: int) -> None:
    """ЛС пригласившему (на его языке); игнорируем, если он заблокировал бота."""
    lang = await redis_conn.get(f"user_lang:{referrer_id}") or "ru"
    try:
        await bot.send_message(
            referrer_id,
            get_string("referral_reward_referrer", lang=lang).format(
                bonus=settings.REFERRAL_BONUS
            ),
        )
    except TelegramAPIError as e:
        logger.info("referrer %s not notified: %s", referrer_id, e)
