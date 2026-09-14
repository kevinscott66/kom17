"""Coin top-up (``/topup``) inline-keyboard CallbackData factories (A1: L-80/L-84).

Wire formats for the self-service top-up menu — the *coin purchase*
flow legacy ran under ``/buy`` (``bot.py:18106 cmd_buy`` and the
``pay_*`` / ``stars_amt_*`` / ``crypto_inv_*`` string ladder around
``bot.py:18042-18372``). The new pipeline's ``/buy`` is the SHOP buy,
so this surface lives under ``/topup`` + ``/buy_coins`` and a fresh
``tp*`` prefix family that cannot collide with either the shop
callbacks or the legacy ladder.

Money-path posture: NO amount travels on the wire. Stars packs and
crypto USD amounts are carried as *indexes* into the server-side
constant tables in :mod:`telegram_invite_bot.handlers.topup`
(``STARS_PACKS`` / ``CRYPTO_USD_AMOUNTS``) — legacy stuffed the raw
``stars``/``coins`` numbers into the callback payload
(``stars_amt_{stars}_{coins}``, ``bot.py:18070``), which trusts a
client-editable value on a money path. An index can only select a
known pack; a tampered index is rejected by a bounds check.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class TopupMethod(CallbackData, prefix="tpm"):
    """Payment-method row on the ``/topup`` menu.

    ``method`` is one of ``stars`` / ``crypto`` / ``yookassa`` /
    ``stripe`` (validated against a literal set in the handler —
    an unknown method renders a generic error, never crashes).
    """

    method: str


class TopupBack(CallbackData, prefix="tpb"):
    """🔙 back to the method-selection menu from any sub-screen."""


class TopupStarsPack(CallbackData, prefix="tps"):
    """One ⭐ Stars pack row. ``idx`` indexes ``STARS_PACKS``.

    (Named ``idx`` rather than ``pack`` — ``CallbackData.pack`` is the
    aiogram serialisation method and a field cannot shadow it.)
    """

    idx: DbInt


class TopupCryptoAsset(CallbackData, prefix="tpca"):
    """Crypto-asset choice (USDT / BTC / TON) under 💎 Crypto Pay."""

    asset: str


class TopupCryptoInvoice(CallbackData, prefix="tpci"):
    """Create a Crypto Pay invoice: ``amount`` indexes
    ``CRYPTO_USD_AMOUNTS``; ``asset`` was validated one screen back
    and is re-validated on receipt."""

    asset: str
    amount: DbInt


class TopupRollyPayAmount(CallbackData, prefix="tpr"):
    """Create a RollyPay payment: ``amount`` indexes ``RUB_AMOUNTS``.

    Carries an index, not a rouble figure, for the reason the module
    docstring gives — but note the stake is smaller here than it looks.
    Even a forged amount could not inflate a credit: the coins come from
    what RollyPay says was *paid*, recomputed on the webhook. What the
    index protects is the price *offered*: an editable amount would let
    someone open a 1 ₽ page and pay it, which is a support ticket rather
    than a theft, and still not something to leave lying around.
    """

    amount: DbInt
