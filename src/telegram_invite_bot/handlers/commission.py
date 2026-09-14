"""``/commission`` — lifetime referral earnings + current commission rate.

Legacy ``/commission`` (bot.py:25105) is the minimal sibling of
``/referrals``: same SUM-over-transactions read (filter
``type='referral'``), no list of invitees, plus the current commission
percent so the user can see "how much they've earned" alongside
"what they'll earn going forward".

One read, two pieces of state:

* ``economy.transactions`` ``SUM(amount) WHERE to_id=? AND
  type='referral'`` — identical shape to ``_fetch_earnings`` in
  ``handlers/referrals.py``. Not extracted to a shared helper yet
  because both call sites live one screen apart and a free function
  in ``handlers/`` that two handlers import would set the wrong
  precedent — utility code lives under ``utils/`` or ``services/``,
  not next to handlers.
* ``settings.economy.referral_commission_percent`` — env-driven
  config landed in Stage 28 (``EconomyConfig``).

Behaviour parity & deltas:

* Any chat type. Legacy gates on ``ensure_user_access`` (role/ban)
  which the new pipeline doesn't model yet — same gap ``/referral``
  and ``/ping`` accept.
* HTML rendering (``<b>...</b>``) instead of legacy's Markdown to
  match the bot-wide ``parse_mode=HTML`` posture. The card has zero
  user-supplied strings — every interpolated value is an integer —
  so no ``html.escape`` needed.
* Trailing pointer lines (``/referral — your link`` / ``/referrals
  — referral list``) are hardcoded plain text rather than threaded
  through new i18n keys. They're terse, identical across locales
  except for two words, and adding two new keys for a four-character
  English/Russian swap on the right-hand side of a dash isn't worth
  the translation-table noise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import func, select

from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import require_from_user

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.commission")

_COIN = "🪙"


async def _fetch_earnings(registry: EngineRegistry, referrer_id: int) -> int:
    """``SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE to_id=?
    AND type='referral'`` — see ``handlers/referrals.py`` for the WHY
    on COALESCE (the displayed number must never render as ``None``).
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.to_id == referrer_id,
                Transaction.type == "referral",
            )
        )
        return int(result.scalar_one())


def _render(lang: str, *, total: int, percent: int) -> str:
    title = t("my_commissions_title", lang)
    intro = t("commission_intro_line", lang)
    earned_label = t("referrals_earned_label", lang)
    percent_label = t("commission_current_percent", lang)
    return "\n".join(
        [
            f"<b>{title}</b>",
            "",
            intro,
            f"{earned_label}: <b>{total}</b> {_COIN}",
            f"{percent_label}: <b>{percent}%</b>",
            "",
            "/referral",
            "/referrals",
        ]
    )


async def handle_commission(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    user = await user_service.touch(require_from_user(message))
    # #1983: end the bookkeeping transaction here rather than hold
    # ``users.db``'s single writer slot across the reply below. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    total = await _fetch_earnings(registry, user.user_id)
    percent = settings.economy.referral_commission_percent
    await message.answer(_render(user.language, total=total, percent=percent))
    log.bind(uid=user.user_id, total=total, percent=percent).info("/commission rendered")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Bare-form only (``magic=F.args.is_(None)``). No future arg-flavoured
    variant is planned, but pinning bare costs nothing and matches the
    posture of every other read-only command in this directory.
    """
    router = Router(name="commission")

    async def _entry(
        message: Message,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_commission(message, settings, registry, user_service, checkpoint)

    router.message.register(
        _entry,
        Command(
            "commission",
            "fees",
            "комиссии",
            "мои_комиссии",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    return router
