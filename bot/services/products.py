# bot/services/products.py
"""Каталог товаров за Telegram Stars (валюта XTR) и их выдача."""

from bot.config import settings
from bot.services import quota

PACK_SIZE = 20

# ключ товара → имя настройки с ценой в Stars
_PRICE_SETTING = {
    "pack20": "STARS_PRICE_PACK20",
    "pro_week": "STARS_PRICE_PRO_WEEK",
    "pro_month": "STARS_PRICE_PRO_MONTH",
    "freeze": "STARS_PRICE_FREEZE",
}
PRODUCT_KEYS = tuple(_PRICE_SETTING)


def price(key: str) -> int:
    return int(getattr(settings, _PRICE_SETTING[key]))


async def grant(r, uid: int, key: str) -> dict:
    """Выдаёт товар. Возвращает детали для сообщения пользователю."""
    if key == "pack20":
        balance = await quota.add_credits(r, uid, PACK_SIZE)
        return {"amount": PACK_SIZE, "balance": balance}
    if key in ("pro_week", "pro_month"):
        until = await quota.grant_pro(r, uid, 7 if key == "pro_week" else 30)
        return {"pro_until": until}
    if key == "freeze":
        return {"freezes": await quota.add_freezes(r, uid, 1)}
    raise ValueError(f"unknown product: {key}")
