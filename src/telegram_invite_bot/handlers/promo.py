"""``/promo`` + ``/promo_create`` — redeemable gift codes (L-96).

A NEW, isolated subsystem (not a stub port): a developer mints a code
that grants a flat number of coins, bounded by a global ``max_uses`` cap
and an optional per-user-once constraint; users redeem it with
``/promo <code>``.

Commands
--------
* ``/promo`` / ``/промокод``:
    - no args → render an info/help blurb.
    - one arg (a code) → redeem flow. Maps each
      :class:`RedeemOutcome` to an i18n message; on OK credits the
      redeemer and shows the new balance.
* ``/promo_create`` / ``/создать_промокод``: DEV-ONLY
  (``settings.bot.is_developer``). Syntax::

      /promo_create <CODE> <reward_coins> [max_uses] [once=true|false]

  ``max_uses`` defaults to 1 (a single redeem) since T-019, and an
  explicit ``0`` is REJECTED: 0 means "unlimited", which no mint budget
  can bound, so :meth:`PromoService.create_code` refuses it outright
  (``promo_service.py:159``) and the developer gets
  ``h_promo_create_invalid`` (#470). ``once`` defaults to true
  (per-user-once). Renders the minted code for the developer to share.

Parse mode is HTML (the bot default); the user-controlled code field is
routed through :func:`html.escape`. Private-chat-only — group calls are
refused by ``with_chat_type_refusal(scope="private")`` at the bottom of
this module; the legacy bridge they used to fall through to was removed
in T-011 (#469).
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.promo_service import CreateOutcome, RedeemOutcome
from telegram_invite_bot.utils.aiogram import command_args, require_from_user

log = logger.bind(component="handlers.promo")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.promo_service import PromoService


def _utcnow() -> datetime:
    """Naive UTC ``now`` — matches the stored-datetime convention."""
    return datetime.now(UTC).replace(tzinfo=None)


# Map each non-OK redeem outcome to its i18n key. OK interpolates
# reward/balance and is handled separately.
_REDEEM_ERROR_KEYS: dict[RedeemOutcome, str] = {
    RedeemOutcome.EMPTY_CODE: "h_promo_empty_code",
    RedeemOutcome.NOT_FOUND: "h_promo_not_found",
    RedeemOutcome.EXHAUSTED: "h_promo_exhausted",
    RedeemOutcome.ALREADY_REDEEMED: "h_promo_already_redeemed",
    RedeemOutcome.CREDIT_FAILED: "h_promo_credit_failed",
}

#: The only ``key=value`` extras ``/promo_create`` understands. Kept as
#: a whitelist rather than "anything with an ``=`` in it" (#745): the
#: code itself is free-form (``PromoService.create_code`` validates
#: length and emptiness, not the charset), so a code containing ``=``
#: used to be swallowed as an unknown extra and the command silently
#: minted the wrong thing — as did a typo like ``onse=false``, which
#: quietly kept the per-user-once default. Unknown tokens now stay
#: positional and trip the usage reply.
_PROMO_CREATE_EXTRAS = frozenset({"once"})


async def handle_promo(
    message: Message,
    command: CommandObject,
    promo_service: PromoService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/promo`` — info blurb (no args) or redeem flow (one arg = code).

    #1870: the checkpoint sits right after :meth:`PromoService.redeem`
    returns, covering the refusals as well as the OK branch.
    :meth:`PromoRepo.reserve_use` (``repositories/promo_repo.py:117``)
    is a guarded ``UPDATE``, so it opens ``BEGIN IMMEDIATE`` on
    ``economy.db`` even on the EXHAUSTED 0-row match and holds the
    single writer slot across the reply below; the two branches that
    already called ``session.rollback()`` themselves (the racing
    per-user-once insert and CREDIT_FAILED) reach a clean session, so
    committing them is a no-op. On OK three writes have landed — the
    reserve, the redemption row and the credit + ledger pair — and the
    reply is not wrapped: a rollback there un-redeems a code the user
    was never told they had spent, which self-heals but leaves them
    staring at nothing.
    """
    user = require_from_user(message)
    arg = command_args(command)
    if not arg:
        await message.reply(t("h_promo_info", lang))
        return

    # One token only — the code (first word). The service re-normalises.
    code = arg.split()[0]
    result = await promo_service.redeem(user_id=user.id, code=code, now=_utcnow())
    if checkpoint is not None:
        await checkpoint()

    if result.outcome is RedeemOutcome.OK:
        await message.reply(
            t(
                "h_promo_redeemed",
                lang,
                reward=result.reward_coins,
                balance=result.new_balance,
            )
        )
        log.bind(uid=user.id, reward=result.reward_coins).info("/promo redeemed")
        return

    await message.reply(t(_REDEEM_ERROR_KEYS[result.outcome], lang))


async def handle_promo_create(
    message: Message,
    command: CommandObject,
    promo_service: PromoService,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/promo_create`` — DEV-ONLY promo-code mint.

    #1870: the mint INSERT is committed before the three replies below.
    Rolling it back would be self-consistent — no code, no confirmation
    — but the developer would be told the mint failed while the writer
    slot on ``economy.db`` had been held across a Telegram round-trip
    for nothing. Both refusal branches are decided before the INSERT
    and take no lock; committing them is a no-op.
    """
    user = require_from_user(message)

    if not settings.bot.is_developer(user.id):
        await message.reply(t("h_promo_create_dev_only", lang))
        return

    raw = command_args(command)
    parts = raw.split() if raw else []
    if len(parts) < 2:
        await message.reply(t("h_promo_create_usage", lang))
        return

    # Split positional tokens from trailing key=value extras (once=).
    # Only the keys in ``_PROMO_CREATE_EXTRAS`` count as extras; see
    # the note there for why an ``=`` alone is not enough (#745).
    extras: dict[str, str] = {}
    positional: list[str] = []
    for tok in parts:
        key, sep, value = tok.partition("=")
        normalized = key.strip().lower()
        if sep and normalized in _PROMO_CREATE_EXTRAS:
            extras[normalized] = value.strip()
        else:
            positional.append(tok)

    # Both ends are checked: the syntax is <CODE> <reward> [max_uses],
    # so a fourth positional means the developer typed something we do
    # not understand — most likely a misspelt extra (``onse=false``),
    # which used to be dropped on the floor while the command minted a
    # code with the opposite ``once`` setting (#745).
    if not 2 <= len(positional) <= 3:
        await message.reply(t("h_promo_create_usage", lang))
        return

    code = positional[0]
    try:
        reward_coins = int(positional[1])
        # T-019: the default used to be 0 = unlimited, which made the
        # shortest form of the command the unbounded one. A new code now
        # defaults to a single redeem; the mint budget is enforced in
        # ``PromoService.create_code`` (docs/ECONOMY_RATE_AUDIT.md R3).
        max_uses = int(positional[2]) if len(positional) > 2 else 1
    except ValueError:
        await message.reply(t("h_promo_create_usage", lang))
        return

    # ``once`` defaults to true (per-user-once); only an explicit
    # "false" turns it off.
    per_user_once = extras.get("once", "true").lower() != "false"

    result = await promo_service.create_code(
        code=code,
        reward_coins=reward_coins,
        max_uses=max_uses,
        per_user_once=per_user_once,
        created_by=user.id,
        now=_utcnow(),
    )
    if checkpoint is not None:
        await checkpoint()

    if result.outcome is CreateOutcome.INVALID:
        await message.reply(t("h_promo_create_invalid", lang))
        return
    if result.outcome is CreateOutcome.DUPLICATE:
        await message.reply(t("h_promo_create_duplicate", lang, code=html.escape(result.code)))
        return

    # OK — render the minted code. The ``max_uses == 0`` branch is
    # defensive only: the mint guard rejects 0 before we get here
    # (#470), so ``h_promo_unlimited`` is unreachable through
    # ``/promo_create``. It stays for codes seeded directly into the DB
    # by an operator, which the redeem path still honours.
    uses = result.max_uses if result.max_uses > 0 else t("h_promo_unlimited", lang)
    once = t("h_promo_yes", lang) if result.per_user_once else t("h_promo_no", lang)
    await message.reply(
        t(
            "h_promo_create_ok",
            lang,
            code=html.escape(result.code),
            reward=result.reward_coins,
            uses=uses,
            once=once,
        )
    )
    log.bind(uid=user.id, code=result.code, reward=result.reward_coins).info("/promo_create minted")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` + ``EconomyMiddleware`` per call.

    Mirrors ``handlers/checks.py``: private-chat-only filter, the economy
    middleware on the message side (injects ``promo_service``), and the
    command registrations. ``settings`` is captured for the dev-gate on
    ``/promo_create``.
    """
    router = Router(name="promo")
    # Private-chat-only — a group call gets the #123 refusal twin.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.middleware(EconomyMiddleware(registry))

    async def _handle_promo(
        message: Message,
        command: CommandObject,
        promo_service: PromoService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_promo(message, command, promo_service, lang, checkpoint)

    router.message.register(
        _handle_promo,
        Command("promo", "промокод", ignore_case=True),
        F.from_user,
    )

    async def _handle_promo_create(
        message: Message,
        command: CommandObject,
        promo_service: PromoService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_promo_create(message, command, promo_service, settings, lang, checkpoint)

    router.message.register(
        _handle_promo_create,
        Command("promo_create", "создать_промокод", ignore_case=True),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
