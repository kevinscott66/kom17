"""``/group_pay`` (``/выплата_из_казны``) — owner withdraws the group treasury.

Ports legacy ``cmd_group_pay`` (bot.py:25022-25062). Gates, in legacy
order:

1. group-only (``ensure_user_access(message, require_group=True)``,
   bot.py:25026) — router-level chat-type filter here;
2. owner-only via ``is_chat_owner`` (bot.py:25031): DEVELOPER bypass +
   live Telegram *creator* check (``is_chat_owner``, bot.py:7521-7526,
   backed by ``get_chat_administrators`` status == "creator",
   bot.py:7109-7117). NOT the ``bot_groups.added_by`` attribution and
   NOT plain admin status — strictly the chat creator. Fail-closed on
   Telegram-API error (legacy's cache helper returns None → not owner).
3. self-only: the owner may withdraw onto their OWN balance only.
   Legacy resolves a target from reply / ``text_mention`` entities
   (``_resolve_target_user_from_message``, bot.py:22712-22719) and
   refuses when it differs from the caller (bot.py:25035-25038). Plain
   ``@username`` strings resolve to None in legacy and are simply
   ignored by the gate — mirrored.
4. amount = the first all-digit token in the message text
   (bot.py:25041-25046); missing/zero → usage hint;
5. minimum amount ``GROUP_TREASURY_MIN_WITHDRAWAL`` (default 1000,
   bot.py:2563 / 3175) → ``min_withdrawal`` knob here (wired from
   settings in ``main_router``).

The money core lives in :class:`GroupTreasuryService` (debit treasury →
credit owner wallet → ledger row → history snapshot, with a SAVEPOINT
making debit+credit atomic — see that module for the legacy citations).

i18n: NEW ``h_group_pay_*`` keys, HTML render (legacy used Markdown).
The insufficient-funds line was a hardcoded RU f-string in legacy
(bot.py:10967, rendered via the generic ``❌ {err}`` reply at
bot.py:25062, where the legacy handler ends); it is properly localised
here.

``/groupstats`` alignment (task item c): legacy ``cmd_groupstats``
(bot.py:24987-25018) renders ``group_xp`` (with a ``total_donations``
fallback for pre-XP rows) — it does NOT display the treasury balance
anywhere; the only place the balance surfaces is this command's
insufficient-funds error (bot.py:10967). The ported
``handlers/groupstats.py`` already mirrors that, so no change is needed
or made there.
"""

from __future__ import annotations

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
from telegram_invite_bot.repositories.treasury_repo import TreasuryRepo
from telegram_invite_bot.services.treasury_service import (
    GroupTreasuryService,
    PayoutOutcome,
)
from telegram_invite_bot.utils.aiogram import (
    command_body,
    reply_or_send,
    require_from_user,
)
from telegram_invite_bot.utils.html import html_user_mention
from telegram_invite_bot.utils.numbers import is_int_token

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="handlers.group_pay")

# Same coin glyph the other economy cards render (handlers/groupstats.py).
_COIN = "🪙"

# Legacy default for ``group_treasury_min_withdrawal`` (bot.py:2563).
DEFAULT_MIN_WITHDRAWAL = 1000

# Both legacy spellings (bot.py:25022).
GROUP_PAY_COMMANDS = ("group_pay", "gpay", "выплата_из_казны")


async def _caller_is_owner(
    bot: Bot, settings: Settings, *, chat_id: int, user_id: int
) -> bool | None:
    """Port of ``is_chat_owner`` (bot.py:7521-7526): developer bypass,
    else the caller must be the live Telegram *creator* of the chat.

    ``None`` on Telegram-API error → the handler refuses with a "try
    again" instead of granting (fail-closed; legacy's
    ``get_chat_creator_id`` swallows the error into None → not owner,
    bot.py:7118-7120). ``get_chat_member`` over ``get_chat_administrators``
    — one membership probe instead of the whole admin list; the status
    comparison is identical (creator only — admins are NOT enough,
    unlike the ``/rating_include`` gate).
    """
    if settings.bot.is_developer(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — re-surface via None contract
        log.bind(chat=chat_id, user=user_id, exc=repr(exc)).warning(
            "group_pay owner-check get_chat_member failed"
        )
        return None
    return member.status == "creator"


def _foreign_target_id(message: Message, caller_id: int) -> int | None:
    """Resolve the legacy target the way ``_resolve_target_user_from_message``
    does (reply first, then ``text_mention`` entities — bot.py:22712-22719)
    and return its id IF it differs from the caller, else ``None``.

    Plain ``@username`` mention entities carry no user object and resolve
    to None in legacy too (bot.py:22716-22718 checks ``text_mention``
    only) — they fall through to the amount parser unchanged.
    """
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        return reply.from_user.id if reply.from_user.id != caller_id else None
    for ent in message.entities or []:
        if ent.type == "text_mention" and ent.user is not None:
            return ent.user.id if ent.user.id != caller_id else None
    return None


def parse_amount(text: str) -> int | None:
    """First all-digit token of the message text, or ``None``.

    Verbatim port of the legacy scan (bot.py:25041-25046): split on
    whitespace, take the first ``str.isdigit`` token. The command token
    (``/group_pay``) can never be all digits, so scanning the full text
    is equivalent to scanning the args."""
    for part in (text or "").split():
        if is_int_token(part):
            return int(part)
    return None


async def handle_group_pay(
    message: Message,
    bot: Bot,
    lang: str,
    *,
    registry: EngineRegistry,
    settings: Settings,
    min_withdrawal: int,
) -> None:
    tg_user = require_from_user(message)
    chat_id = message.chat.id

    # Gate 2: owner-only (bot.py:25031-25033), fail-closed on API error.
    allowed = await _caller_is_owner(bot, settings, chat_id=chat_id, user_id=tg_user.id)
    if allowed is None:
        await message.reply(t("h_group_pay_retry_later", lang))
        return
    if not allowed:
        await message.reply(t("h_group_pay_owner_only", lang))
        return

    # Gate 3: self-only (bot.py:25035-25038).
    if _foreign_target_id(message, tg_user.id) is not None:
        await message.reply(t("h_group_pay_self_only", lang))
        return

    # Gate 4: amount (bot.py:25041-25049).
    amount = parse_amount(command_body(message))
    if amount is None or amount < 1:
        await message.reply(t("h_group_pay_specify_amount", lang))
        return

    # Gate 5: minimum (bot.py:25050-25051).
    if amount < min_withdrawal:
        await message.reply(t("h_group_pay_min_amount", lang, min=min_withdrawal, sign=_COIN))
        return

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        service = GroupTreasuryService(
            TreasuryRepo(session),
            EconomyRepo(session),
            TransactionsRepo(session),
            DonationsRatingRepo(session),
        )
        result = await service.payout(group_id=chat_id, to_user_id=tg_user.id, amount=amount)
        if result.outcome is PayoutOutcome.SUCCESS:
            await session.commit()
        else:
            await session.rollback()

    if result.outcome is PayoutOutcome.INSUFFICIENT_FUNDS:
        # Legacy renders this as a raw RU f-string with the available
        # balance (bot.py:10967) → localised key here.
        await message.reply(
            t("h_group_pay_insufficient", lang, available=result.available, sign=_COIN)
        )
        return
    if result.outcome is not PayoutOutcome.SUCCESS:
        # INVALID_AMOUNT can't reach here (gate 4); CREDIT_FAILED is the
        # wallet-cap edge — generic failure line, money already rolled back.
        await message.reply(t("h_group_pay_failed", lang))
        return

    mention = html_user_mention(tg_user.id, tg_user.first_name or str(tg_user.id))
    # The payout committed above, in this handler's own session — the
    # middleware cannot take it back. So the confirmation must not be a
    # bare ``reply``: if the owner's command message is gone, the raise
    # reaches ``handlers.errors`` and the chat reads "⚠️ Произошла
    # ошибка" over a payout that went through. The obvious next move for
    # an owner who sees that is to run ``/group_pay`` again, and the
    # treasury pays twice.
    if not await reply_or_send(
        message, t("h_group_pay_done", lang, amount=amount, sign=_COIN, mention=mention)
    ):
        log.bind(chat_id=chat_id, uid=tg_user.id, amount=amount).warning(
            "/group_pay confirmation undeliverable"
        )
    log.bind(chat_id=chat_id, uid=tg_user.id, amount=amount).info("/group_pay done")

    # Best-effort DM receipt (bot.py:25057-25060) — the recipient IS the
    # caller, so the middleware-resolved ``lang`` is the right locale.
    try:
        await bot.send_message(
            tg_user.id,
            t("h_group_pay_recipient_notify", lang, amount=amount, sign=_COIN),
        )
    except Exception:  # noqa: BLE001 — DMs closed is normal, not an error
        log.bind(uid=tg_user.id).debug("group_pay DM receipt undeliverable")


def build_router(
    registry: EngineRegistry,
    settings: Settings,
    *,
    min_withdrawal: int = DEFAULT_MIN_WITHDRAWAL,
) -> Router:
    """Group-only at the router level (legacy ``require_group=True``,
    bot.py:25026). ``min_withdrawal`` is passed in from
    ``settings.economy.group_treasury_min_withdrawal`` by the call site
    in ``routers/main_router.py``; the default is only the fallback for a
    caller that omits it, and mirrors legacy's ``bot_settings.json``
    value (bot.py:3175).

    No scoped middleware: the economy session is opened explicitly per
    payout (same posture as the ``/rating_include`` write path in
    ``handlers/rating.py``) because four of the five gates short-circuit
    without ever needing a DB connection.
    """
    router = Router(name="group_pay")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(message: Message, bot: Bot, lang: str) -> None:
        await handle_group_pay(
            message,
            bot,
            lang,
            registry=registry,
            settings=settings,
            min_withdrawal=min_withdrawal,
        )

    router.message.register(
        _entry,
        Command(*GROUP_PAY_COMMANDS, ignore_case=True),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="group")
