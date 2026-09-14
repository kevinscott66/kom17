"""Aiogram :class:`CallbackData` factories for inline-keyboard buttons.

Stage 23 lands this package as the foundation for porting the legacy
callback_query handlers (``/shop`` browse, ``/buy`` flow, ``/help``
pagination, ``/faq`` continue). Every callback_data wire format used
by a new-pipeline router must declare a :class:`CallbackData`
subclass here — not a free-form string — so prefix conflicts are
caught at import time rather than at runtime when two routers fight
for the same opaque payload.

Why a dedicated package instead of inlining the classes next to
their handlers:

* The wire format (the ``prefix`` and field layout) is the
  contract; keeping all contracts in one place makes a prefix
  conflict (``"shop_buy"`` vs ``"shop_b"`` — the latter is a
  prefix of the former, which would route ambiguously through
  aiogram's :meth:`CallbackData.filter`) a one-grep audit.
* Some callbacks (e.g. the inline "Continue → part 2" button under
  ``/faq``) are rendered from one handler and consumed from
  another; storing the class with the rendering handler would
  invert the dependency direction.
* The legacy bot uses bare strings (``"faq_part2"``, ``"shop_buy_42"``)
  and a 200-line ``if call.data.startswith(...)`` ladder. Porting
  in lockstep means each new :class:`CallbackData` here is paired
  with one legacy ladder branch deleted — the package's existence
  is the migration ledger.

Stage 23 ships one concrete class (:class:`FaqContinue`) as proof of
life; subsequent stages add ``ShopBrowse``, ``BuyConfirm``, ``HelpPage``,
etc.
"""

from telegram_invite_bot.keyboards.builders.admin_panel import AdminNav
from telegram_invite_bot.keyboards.builders.admin_withdraw import (
    WithdrawApprove,
    WithdrawReject,
)
from telegram_invite_bot.keyboards.builders.couple_activities import CoupleActivity
from telegram_invite_bot.keyboards.builders.duel import (
    DuelAccept,
    DuelDecline,
    DuelRoll,
)
from telegram_invite_bot.keyboards.builders.faq import FaqContinue
from telegram_invite_bot.keyboards.builders.profile import ProfilePanel, ProfileRefresh
from telegram_invite_bot.keyboards.builders.rps import RpsAccept, RpsDecline
from telegram_invite_bot.keyboards.builders.rps import RpsMove as RpsMoveCallback
from telegram_invite_bot.keyboards.builders.shop import (
    InventoryBack,
    InventoryInspect,
    InventoryPage,
    InventoryUse,
    ShopBuyCancel,
    ShopBuyConfirm,
    ShopBuyPrompt,
    ShopGroupMenu,
    ShopGroupPick,
    ShopPage,
)
from telegram_invite_bot.keyboards.builders.topup import (
    TopupBack,
    TopupCryptoAsset,
    TopupCryptoInvoice,
    TopupMethod,
    TopupRollyPayAmount,
    TopupStarsPack,
)
from telegram_invite_bot.keyboards.builders.withdraw import (
    WithdrawCancel,
    WithdrawConfirm,
)

__all__ = [
    "AdminNav",
    "CoupleActivity",
    "DuelAccept",
    "DuelDecline",
    "DuelRoll",
    "FaqContinue",
    "InventoryBack",
    "InventoryInspect",
    "InventoryPage",
    "InventoryUse",
    "ProfilePanel",
    "ProfileRefresh",
    "RpsAccept",
    "RpsDecline",
    "RpsMoveCallback",
    "ShopBuyCancel",
    "ShopBuyConfirm",
    "ShopBuyPrompt",
    "ShopGroupMenu",
    "ShopGroupPick",
    "ShopPage",
    "TopupBack",
    "TopupCryptoAsset",
    "TopupCryptoInvoice",
    "TopupMethod",
    "TopupRollyPayAmount",
    "TopupStarsPack",
    "WithdrawApprove",
    "WithdrawCancel",
    "WithdrawConfirm",
    "WithdrawReject",
]
