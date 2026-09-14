"""``/donate`` (``/донат``) — a member donates coins to the current group.

Ports legacy ``cmd_donate`` (bot.py:24872-24916) over
:meth:`~telegram_invite_bot.services.group_donation_service
.GroupDonationService.donate`, which holds the money core (debit →
donation rows → ``group_xp`` bump → owner payout minus the developer
cut → recalc → today's snapshot, all inside one SAVEPOINT).

Why this file exists at all
---------------------------
T-011 removed legacy and the port replaced the command with a static
"support the bot" blurb registered PRIVATE-only
(``handlers/support.py``). So the surface the command catalog still
advertises as «поддержать группу или автора монетами»
(``h_cmd_donate``, ``core/ranks.py``) answered in a group with the #123
"this works in a DM" refusal, and in the DM with a blurb pointing at a
donation link no setting in this package can hold. Two other live cards
— ``no_groups_with_donates`` and ``h_donaters_empty`` — send the user to
``/donate`` as the way to fund a group, so the dead end was reachable
from the product itself, not only from ``/help``.

Gates, in legacy order (bot.py:24876-24902):

1. group-only (``require_group=True``, bot.py:24876) — router-level
   chat-type filter here, with the localised refusal from
   ``handlers/chat_scope``;
2. a real person behind the message. NEW: legacy had no such check
   because it predates ``sender_chat``. The debit lands on a personal
   wallet, and an anonymous admin's ``from`` id is the shared
   ``GroupAnonymousBot`` account — so legacy would have charged one
   placeholder wallet for every anonymous admin in every group;
3. amount = the first integer token (bot.py:24884-24890) → usage card
   when absent, which also prints the caller's balance and the band;
4. ``DONATE_MIN_AMOUNT`` / ``DONATE_MAX_AMOUNT`` (bot.py:10653-10656),
   knobs here (``EconomyConfig.donate_{min,max}_amount``);
5. the anti-spam gap (bot.py:24878-24882), ``donate_cooldown_seconds``.

Deliberate divergences from legacy
----------------------------------
* **The receipt quotes ``group_xp``, not ``total_donations``.** Legacy
  printed the latter (bot.py:24906) — a column nothing has incremented
  since before the cutover (bot.py:10915 «Сейчас не пополняется») —
  while the leaderboard it was congratulating the user about ranks on
  the former. So its own success card always showed a number that did
  not move.
* **One reply, not two.** Legacy answered the donor and then announced
  the donation to the whole chat (bot.py:24908-24914). The announcement
  named the donor and the sum unconditionally, which turns every
  donation into a public disclosure of one member's spending; the
  receipt is a reply, so the chat sees it in context anyway.
* **The cooldown is bounded.** Legacy's ``_donate_last_time``
  (bot.py:10571) is a plain dict keyed on ``(user, group)`` that nothing
  ever prunes — a slow leak for the lifetime of the process. A
  :class:`TTLLRUCache` expires and caps it.
* **``require_group_feature`` is NOT ported.** Legacy gated the command
  on a per-group feature toggle plus an auto-delete of the command
  message (bot.py:24874). Neither exists in this package yet, so there
  is nothing to gate on; noted here rather than half-implemented,
  because a toggle that silently always says "on" is worse than an
  absent one.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.donations_rating_repo import DonationsRatingRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.group_donation_service import (
    DonateOutcome,
    GroupDonationService,
)
from telegram_invite_bot.utils.aiogram import (
    command_body,
    reply_or_send,
    require_from_user,
)
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.telegram_admin import chat_creator_id
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="handlers.donate")

# Same coin glyph the other economy cards render (handlers/group_pay.py).
_COIN = "🪙"

# Both legacy spellings plus the catalog alias (core/ranks.py entry 118).
DONATE_COMMANDS = ("donate", "донат", "kom_donate")

# Ceiling on the cooldown table. One entry per (donor, group) pair that
# donated inside the window; at the default 10s gap the live set is tiny,
# so this is a leak stop rather than a working limit.
_COOLDOWN_CAPACITY = 4096


def parse_amount(text: str) -> int | None:
    """First all-digit token of the command body, or ``None``.

    Same scan as ``handlers/group_pay.parse_amount`` and as legacy
    (bot.py:24884-24890): a leading ``-`` never reads as an amount, so a
    negative sum arrives here as ``None`` and gets the usage card rather
    than reaching the service's ``amount <= 0`` guard.
    """
    for part in (text or "").split():
        if is_int_token(part):
            return int(part)
    return None


async def handle_donate(
    message: Message,
    bot: Bot,
    lang: str,
    *,
    registry: EngineRegistry,
    settings: Settings,
    cooldown: TTLLRUCache[tuple[int, int], float],
) -> None:
    tg_user = require_from_user(message)
    chat_id = message.chat.id

    # Gate 2: a personal wallet needs a person (see the module docstring).
    if message.sender_chat is not None:
        await message.reply(t("h_donate_anonymous", lang))
        return

    economy = settings.economy
    sessionmaker = registry.session(DBName.ECONOMY)

    # Gate 3: amount. The usage card names the caller's balance, so the
    # next attempt is not a guess — legacy's hint carried the band only
    # (bot.py:24888).
    amount = parse_amount(command_body(message))
    if amount is None:
        async with sessionmaker() as session:
            wallet = await EconomyRepo(session).get_or_create(tg_user.id)
            await session.commit()
        await message.reply(
            t(
                "h_donate_usage",
                lang,
                min=economy.donate_min_amount,
                max=economy.donate_max_amount,
                balance=wallet.balance,
                sign=_COIN,
            )
        )
        return

    # Gate 4: the configured band. Two distinct refusals, each naming the
    # end it enforces (bot.py:10653-10656) — a shared "wrong amount" line
    # would leave the user guessing which one they hit.
    if amount < economy.donate_min_amount:
        await message.reply(t("h_donate_min", lang, min=economy.donate_min_amount, sign=_COIN))
        return
    if amount > economy.donate_max_amount:
        await message.reply(t("h_donate_max", lang, max=economy.donate_max_amount, sign=_COIN))
        return

    # Gate 5: the anti-spam gap. Legacy stamps it only on success
    # (bot.py:24902) so a refusal never costs the user their next
    # window, and that still holds — but the claim is taken HERE, in the
    # same await-free step as the check, and handed back below on the
    # two refusals that can still happen (#2016).
    #
    # It used to be stamped at the very bottom, past
    # ``getChatAdministrators`` and the whole donation transaction, so
    # every ``/donate`` sent during that window read a clear table and
    # the configured gap bounded nothing: three taps, three donations.
    # Claiming here also fixes a smaller drift — the stamp was computed
    # from a ``now`` captured before those awaits, so the window opened
    # late by however long they took.
    key = (tg_user.id, chat_id)
    now = time.monotonic()
    deadline = cooldown.get(key, now)
    if deadline is not None:
        await message.reply(t("h_donate_cooldown", lang, seconds=max(1, math.ceil(deadline - now))))
        return
    cooldown.put(key, now + economy.donate_cooldown_seconds, now)

    # Resolved BEFORE the write transaction opens: SQLite writers are
    # serialised process-wide, so a Telegram round-trip held inside one
    # stalls every other writer (same rule as ``GroupDonationService``).
    owner_id = await chat_creator_id(bot, chat_id)

    async with sessionmaker() as session:
        service = GroupDonationService(
            DonationsRatingRepo(session),
            EconomyRepo(session),
            TransactionsRepo(session),
            percent=economy.purchase_donation_to_group_percent,
            developer_percent=economy.developer_commission_percent,
            developer_id=settings.bot.admin_chat_id,
        )
        result = await service.donate(
            group_id=chat_id,
            user_id=tg_user.id,
            amount=amount,
            title=message.chat.title,
            owner_id=owner_id,
        )
        if result.outcome in {DonateOutcome.DONATED, DonateOutcome.DONATED_NO_OWNER}:
            await session.commit()
        else:
            await session.rollback()

    if result.outcome is DonateOutcome.INSUFFICIENT:
        # Nothing moved, so the claim above goes back: a donor who was
        # outbid to their own balance by a concurrent spend has not used
        # their window.
        cooldown.discard(key)
        await message.reply(t("h_donate_insufficient", lang, balance=result.balance, sign=_COIN))
        return
    if result.outcome is DonateOutcome.INVALID_AMOUNT:
        # Unreachable through the gates above; kept because the service
        # is the authority on what it will accept, not this handler.
        cooldown.discard(key)
        await message.reply(t("h_donate_failed", lang))
        return

    if result.owner_credited:
        card = t(
            "h_donate_done_owner",
            lang,
            amount=result.amount,
            xp=result.group_xp,
            to_owner=result.to_owner,
            balance=result.balance,
            sign=_COIN,
        )
    else:
        # No owner resolved, or the payout was refused — the card must
        # not promise coins nobody got (see ``DonateResult``).
        card = t(
            "h_donate_done",
            lang,
            amount=result.amount,
            xp=result.group_xp,
            balance=result.balance,
            sign=_COIN,
        )
    # The donation committed above, in this handler's own session. A bare
    # ``reply`` on a deleted command message raises into ``handlers.errors``
    # and the chat reads "⚠️ Произошла ошибка" over coins that did move —
    # and the obvious next move for the donor is to donate again.
    if not await reply_or_send(message, card):
        log.bind(chat_id=chat_id, uid=tg_user.id, amount=amount).warning(
            "/donate receipt undeliverable"
        )
    log.bind(
        chat_id=chat_id,
        uid=tg_user.id,
        amount=amount,
        outcome=str(result.outcome),
    ).info("/donate done")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Group-only at the router level (legacy ``require_group=True``).

    The cooldown table is a closure local rather than a module global:
    a module-level cache is shared by every dispatcher in a process, so
    one test's donation would silence the next test's, and a second bot
    instance in the same process would inherit the first one's windows.
    """
    router = Router(name="donate")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    cooldown: TTLLRUCache[tuple[int, int], float] = TTLLRUCache(
        ttl=float(settings.economy.donate_cooldown_seconds),
        capacity=_COOLDOWN_CAPACITY,
    )

    async def _entry(message: Message, bot: Bot, lang: str) -> None:
        await handle_donate(
            message,
            bot,
            lang,
            registry=registry,
            settings=settings,
            cooldown=cooldown,
        )

    router.message.register(
        _entry,
        Command(*DONATE_COMMANDS, ignore_case=True),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="group")
