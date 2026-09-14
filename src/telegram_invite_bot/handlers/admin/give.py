"""``/give`` — admin/dev gift coins to a user without purchase (L-24).

Legacy parity: the admin-panel ``economy_give_coins`` flow
(bot.py:32502 ``process_give_coins``) credited a target user a flat
number of coins via ``add_coins(..., reason="Выдано администратором …",
admin_id=…)`` and DM'd the recipient. This is the slash-command port of
that flow, gated on the same owner set the rest of the dev surface uses
(``settings.bot.is_developer``).

Surface
-------
* ``/give`` / ``/выдать``: developer-only.

  Three target forms (mirrors :mod:`telegram_invite_bot.handlers.send`):

  - reply:      reply to a user + ``/give <amount>``
  - numeric id: ``/give <user_id> <amount>``
  - @username:  ``/give @alice <amount>`` (resolved via
    :class:`UsersRepo.get_by_username`)

Ledger-backed: the credit goes through
:meth:`EconomyService.credit` with ``type="admin_give"`` so the gift
appears in the transaction ledger. ``from_id`` is NULL because these
coins are MINTED — nothing leaves the granting admin's wallet — and
the ledger puts the direction in the ``from_id``/``to_id`` pair, so
naming the admin there claims a payer who never paid (#1971, the same
correction #1476 made for the referral kickback). The granting admin
stays traceable through ``reason``. The target wallet is bootstrapped with
``get_or_create`` first so a /give to a brand-new user can't no-op on a
missing row.

Parse mode is HTML (bot default); every interpolated dynamic value is
routed through :func:`html.escape` defensively even though they are all
integers today.

NOT a money path the user can trigger — this MINTS coins, so the dev
gate is load-bearing. A non-developer caller gets the same refusal the
other dev commands render and the handler returns before any wallet
read.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.language import best_effort_language_for_user
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.economy import validate_credit_amount
from telegram_invite_bot.utils.numbers import parse_int_token

log = logger.bind(component="handlers.admin.give")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.economy_service import EconomyService


def _parse_target_and_amount(
    raw: str, *, has_reply: bool
) -> tuple[str | None, int | None, int] | None:
    """Split ``/give`` args into (username, user_id, amount).

    Returns one of:
      * ``(username, None, amount)`` — ``@alice 100``
      * ``(None, user_id, amount)`` — ``12345 100``
      * ``(None, None, amount)`` — reply form (``100`` with a reply)
    or ``None`` on any parse failure (caller renders usage).

    Mirrors :func:`telegram_invite_bot.handlers.send._parse_args`'s
    precedence: an explicit ``<target> <amount>`` always wins over a
    reply target; the bare ``<amount>`` form is only accepted when a
    reply is present.

    :func:`parse_int_token` on all three numeric tokens rather than a
    bare ``int()`` (#1595). ``int()`` also accepts ``_`` separators
    and non-ASCII digit runs (fullwidth, Arabic-Indic), so a gift
    could be minted from a string no operator reading the audit log
    would recognise as that number. This is the command that MINTS
    coins, and the repository already had the helper: same policy as
    :func:`handlers.rank_admin._parse_rank` and
    :func:`handlers.admin.group_migrate._parse`.

    ``signed=True`` on all three, so a negative amount still parses
    here and is still refused downstream by
    :func:`validate_credit_amount` — the split of responsibility
    this parser's tests pin. The only values dropped besides the
    exotic spellings are those above ``2**63 - 1``, which SQLite
    cannot store in the first place; they now render usage instead
    of the amount error, which is the same refusal either way.
    """
    parts = raw.split()
    if not parts:
        return None

    # Bare amount → reply form (only when replying to someone).
    if len(parts) == 1:
        if not has_reply:
            return None
        amount = parse_int_token(parts[0], signed=True)
        if amount is None:
            return None
        return (None, None, amount)

    # Two+ tokens: first is the target, second is the amount.
    target_raw, amount_raw = parts[0], parts[1]
    amount = parse_int_token(amount_raw, signed=True)
    if amount is None:
        return None

    if target_raw.startswith("@"):
        username = target_raw[1:]
        if not username:
            return None
        return (username, None, amount)

    user_id = parse_int_token(target_raw, signed=True)
    if user_id is None:
        return None
    return (None, user_id, amount)


async def handle_give(
    message: Message,
    command: CommandObject,
    economy_repo: EconomyRepo,
    economy_service: EconomyService,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    bot: Bot,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/give`` — developer-only ledger-backed coin gift.

    #1865: the credit is committed before the admin is answered.
    ``credit`` writes the wallet row and its ledger row, and the
    session middleware otherwise commits only after the handler
    returns (``middlewares/base.py:157-158``), so a failing
    ``message.reply`` — a deleted command message, or a developer who
    has blocked the bot; ``handlers/errors.py`` classes both as benign
    and says nothing to anyone — rolled the minted coins AND their
    ledger row back silently. The admin saw no confirmation and no
    error, and the natural next move is to re-run ``/give``. The
    recipient DM below was already best-effort, which left the reply as
    the only unguarded step.
    """
    admin = require_from_user(message)

    # Dev gate FIRST — before any DB read. Minting coins is owner-only.
    if not settings.bot.is_developer(admin.id):
        await message.reply(t("h_give_dev_only", lang))
        log.bind(uid=admin.id).warning("/give denied — not a developer")
        return

    reply_msg = message.reply_to_message
    reply_target = None
    if reply_msg is not None and reply_msg.from_user is not None and not reply_msg.from_user.is_bot:
        reply_target = reply_msg.from_user.id

    raw = command_args(command)
    parsed = _parse_target_and_amount(raw, has_reply=reply_target is not None)
    if parsed is None:
        await message.reply(t("h_give_usage", lang))
        return

    username, numeric_id, amount = parsed

    # Resolve the target user_id from whichever form was used.
    if username is not None:
        recipient = await users_repo.get_by_username(username)
        if recipient is None:
            await message.reply(t("h_give_user_not_found", lang, target=html.escape(username)))
            return
        target_id = recipient.user_id
    elif numeric_id is not None:
        target_id = numeric_id
    else:
        assert reply_target is not None  # parser guarantees this arm
        target_id = reply_target

    # Amount validation — positive and within the credit cap. Mirrors
    # legacy's ``amount <= 0`` refusal plus the new pipeline's
    # ``_MAX_AMOUNT`` ceiling.
    if not validate_credit_amount(amount):
        await message.reply(t("h_give_invalid_amount", lang))
        return

    # Refuse gifting this bot itself — a credit there is unrecoverable.
    bot_me = await bot.me()
    if target_id == bot_me.id:
        await message.reply(t("h_give_bot_recipient", lang))
        return

    # Bootstrap the wallet so a /give to a never-seen user can't no-op
    # on a missing row (legacy ``add_coins`` auto-created it too).
    await economy_repo.get_or_create(target_id)

    wallet = await economy_service.credit(
        target_id,
        amount,
        type="admin_give",
        reason=f"admin gift by {admin.id}",
        # #1971: NULL, not ``admin.id``. Both readers of this column
        # ignore ``type`` — ``TransactionsRepo.window_stats`` sums
        # ``ABS(amount) WHERE from_id == user`` and ``recent`` renders
        # any row whose ``from_id`` is the viewer as a minus — so every
        # gift grew the operator's own reported "spent" by coins that
        # never left a wallet. This is the shape every other mint in
        # the tree writes; see the enumeration at
        # ``referral_commission_service.py`` (#1476).
        from_id=None,
    )
    if wallet is None:
        # credit() returns None only on validation reject (caught above)
        # or the balance-cap overflow — surface a generic failure.
        await message.reply(t("h_give_failed", lang))
        log.bind(uid=admin.id, target=target_id, amount=amount).error(
            "/give credit failed (balance cap?)"
        )
        return

    # Everything that matters has landed; release ``economy.db``'s
    # writer slot before the two Telegram round-trips below, and make
    # the gift durable regardless of whether either gets through.
    if checkpoint is not None:
        await checkpoint()

    await message.reply(
        t(
            "h_give_success",
            lang,
            target=html.escape(str(target_id)),
            amount=html.escape(str(amount)),
            balance=html.escape(str(wallet.balance)),
        )
    )
    log.bind(uid=admin.id, target=target_id, amount=amount).info("/give credited")

    # Best-effort DM to the recipient (legacy did the same in a
    # try/except). A blocked bot / privacy setting must not fail the
    # whole command — the credit already landed.
    #
    # ``lang`` is the middleware's stamp for whoever typed /give, so it
    # is the wrong language for a message addressed to someone else
    # (#1506): a RU developer gifting an EN user was sending a Russian
    # balance notice. Legacy resolved the two separately —
    # ``bot.py:32534`` for the admin reply, ``bot.py:32541`` for the
    # recipient DM.
    target_lang = await best_effort_language_for_user(
        target_id,
        users_repo=users_repo,
        settings_repo=user_settings_repo,
        fallback=lang,
    )
    try:
        await bot.send_message(
            target_id, t("h_give_notify", target_lang, amount=html.escape(str(amount)))
        )
    except TelegramAPIError as exc:
        log.bind(target=target_id, exc=str(exc)).info(
            "/give: recipient DM failed (blocked/privacy) — credit stands"
        )


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` + economy/users middlewares per call.

    Two middlewares, registration order mirrors ``handlers/send.py``:

    * :class:`EconomyMiddleware` — opens an ``economy.db`` session and
      stamps ``economy_repo`` / ``economy_service`` for the ledger-backed
      credit.
    * :class:`SessionMiddleware` — opens a ``users.db`` session and
      stamps ``users_repo`` for the ``@username`` resolution branch and
      ``user_settings_repo`` for the recipient's language (#1506). Every
      form needs the latter, so the session is no longer optional — not
      that it ever cost much on a dev-only command.

    No chat-type filter: ``/give`` works in private and groups (an admin
    replying to a user in a group is the common case), with the dev gate
    enforced in-handler.
    """
    router = Router(name="admin_give")
    router.message.middleware(EconomyMiddleware(registry))
    router.message.middleware(SessionMiddleware(registry))

    async def _handle_give(
        message: Message,
        command: CommandObject,
        economy_repo: EconomyRepo,
        economy_service: EconomyService,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        bot: Bot,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_give(
            message,
            command,
            economy_repo,
            economy_service,
            users_repo,
            user_settings_repo,
            settings,
            bot,
            lang,
            checkpoint,
        )

    router.message.register(
        _handle_give,
        Command("give", "выдать", ignore_case=True),
        F.from_user,
    )
    return router
