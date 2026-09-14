"""``/withdraw`` — user-side escrow-on-create withdrawal flow (#28, T-027).

Replaces the T-024.1 deferral stub with the real two-step FSM:

1. ``/withdraw`` (or ``/вывод``) opens the flow: shows the balance,
   rate and request band, then asks for an amount and parks in
   :attr:`WithdrawStates.awaiting_amount`.
2. The amount message is parsed and validated against the injected
   limits (``WITHDRAW_MIN_COINS`` / ``WITHDRAW_MAX_COINS``) and the
   live balance. A valid amount renders a confirm card with ✅/❌
   buttons and advances to :attr:`WithdrawStates.awaiting_confirm`.
3. ✅ Confirm calls :meth:`WithdrawService.create`, which escrows the
   coins out of the wallet and writes a ``pending`` row atomically.
   The user is told the request id; an admin approves the payout
   later (chunk D). ❌ Cancel just clears the flow — nothing escrowed.

#237: a created request also pushes an admin DM to
``settings.bot.admin_chat_id``. Legacy notified on all three of its
withdraw paths — crypto (bot.py:20440-20446), card (bot.py:20503-20509)
and instant buyout (bot.py:20637-20644) — and without it the port's only
owner signal was the 24h staleness sweep in
:mod:`telegram_invite_bot.scheduler.economy_cleanup`, so a request could
sit a full day before anyone learned it existed. The DM is best-effort:
the row and the escrow are the source of truth and the user has already
been told the request was created, so a failed send is logged, never
rolled back. Deliberately NOT a port of legacy's card line
(bot.py:20506), which printed the full PAN — #172 removed exactly that
class of leak from the admin panel and it is not coming back through a
push message.

Private-only at the router level: balances + amounts are per-user and
have no place in a group. The money invariants live entirely in
:class:`WithdrawService` — this handler only validates input, renders
i18n copy and drives the FSM. The amount is the FSM data's single
source of truth; the callback payload carries only a ``user_id`` auth
tag (never the amount — a money path must not trust a client value).
"""

from __future__ import annotations

import contextlib
import html
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from loguru import logger

from telegram_invite_bot.fsm.withdraw import WithdrawStates
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import WithdrawCancel, WithdrawConfirm
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.services.withdraw_service import (
    CreateOutcome,
    format_crypto_amount,
)
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.numbers import is_int_token

log = logger.bind(component="handlers.withdraw")

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings, WithdrawConfig
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.db.session import Checkpoint
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.user_service import UserService
    from telegram_invite_bot.services.withdraw_service import WithdrawService

# FSM data keys. ``lang`` is stamped at entry so the confirm callback
# can render in the right language without a second ``users`` read;
# ``amount`` is the validated COM amount (source of truth for create).
_DATA_LANG = "lang"
_DATA_AMOUNT = "amount"

# #777: one lock per (bot, user) around the confirm step's
# read-state → clear-state → create span.
#
# aiogram gives us no per-user serialisation to lean on: the dispatcher
# is built without ``events_isolation`` (the ``dispatcher`` provider in
# ``di/providers.py``), so it falls back to ``DisabledEventIsolation``
# and two taps on the same ✅ are two concurrent coroutines. ``StateFilter`` does not help — it
# resolves through an awaited ``FSMContext.get_state()``, so both taps
# pass it before either reaches ``clear()``. Nor does throttling: the
# bucket is 10 deep (``ThrottlingConfig.capacity``).
#
# Same registry and same shape as ``handlers/rps.py:381-393``, which is
# the house pattern for exactly this. ``KeyedLocks`` refcounts its slots,
# so a lock is created on the first waiter and dropped when the last one
# leaves — the table cannot grow with every user who ever withdrew.
_confirm_locks: KeyedLocks[tuple[int, int]] = KeyedLocks()


async def on_expire_withdraw(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """FSM-sweeper timeout callback for the withdraw interview
    (``awaiting_amount`` / ``awaiting_confirm``) — GAP-1.

    No coins are escrowed until the confirm step actually creates the
    request (the debit lives in ``WithdrawService.create``), so an
    abandoned interview holds NO money — the only lingering effect is the
    busy-lock that makes ``handle_withdraw_start`` reject re-entry with
    ``h_withdraw_busy``. The sweeper clears the FSM after this returns; we
    just DM the user so they know the session lapsed and can ``/withdraw``
    again instead of being silently stuck "busy".
    """
    lang_raw = data.get(_DATA_LANG)
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.user_id, t("h_withdraw_expired", lang))
    log.bind(uid=key.user_id).info("/withdraw interview expired by sweeper")


def _fmt_rate(coins_per_usdt: float) -> str:
    """Render the rate without a trailing ``.0`` for the common whole
    value (``900.0`` → ``"900"``), but keep a fractional rate intact."""
    return str(int(coins_per_usdt)) if coins_per_usdt.is_integer() else str(coins_per_usdt)


async def _balance_of(economy_repo: EconomyRepo, user_id: int) -> int:
    """Current wallet balance, or ``0`` when no wallet row exists yet."""
    wallet = await economy_repo.get(user_id)
    return wallet.balance if wallet is not None else 0


def _confirm_keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup:
    """The ✅/❌ card under the amount confirmation, labelled in ``lang``.

    Both payloads carry only ``user_id`` (auth tag). The amount is read
    from FSM data on click, never from the wire.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_withdraw_btn_confirm", lang),
                    callback_data=WithdrawConfirm(user_id=user_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_withdraw_btn_cancel", lang),
                    callback_data=WithdrawCancel(user_id=user_id).pack(),
                ),
            ]
        ]
    )


async def handle_withdraw_start(
    message: Message,
    state: FSMContext,
    user_service: UserService,
    economy_repo: EconomyRepo,
    withdraw_service: WithdrawService,
    withdraw_config: WithdrawConfig,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/withdraw`` entry — render the intro and park in awaiting_amount.

    The intro exposes the user's remaining daily / monthly quota (L-92)
    alongside the balance and band, so they see up-front how much of
    their rolling cap is still available this cycle before typing an
    amount. The caps reset implicitly at the day / month boundary.

    T-020 (R6) adds the lifetime line under it when the payout cap is
    armed. That one does *not* reset on a clock — it moves only when the
    user tops up — so it belongs on the intro rather than being sprung on
    them after they have already typed an amount.
    """
    tg_user = require_from_user(message)
    user = await user_service.touch(tg_user)
    lang = user.language
    # #778: ``touch`` is an unconditional write, so ``users.db`` is now
    # under ``BEGIN IMMEDIATE`` (``db/engines.py:204-210``) and stays
    # there until the middleware commits after this handler returns
    # (``middlewares/base.py:157-158``). Both exits below reply to
    # Telegram, and the card path first runs three ``economy.db`` reads.
    # Holding one process-wide writer slot across a network round-trip is
    # what turns a slow Telegram into ``database is locked`` for every
    # other update — the same reasoning as ``handlers/rating.py:680-687``.
    # Nothing after this point writes ``users.db``, so one release here
    # covers every path.
    if checkpoint is not None:
        await checkpoint()

    # Guard against double-entry: if the user is mid-flow (theirs or a
    # leftover from another command), point them at /cancel rather than
    # silently restarting and stranding the prior card.
    if await state.get_state() is not None:
        await message.reply(t("h_withdraw_busy", lang))
        return

    balance = await _balance_of(economy_repo, user.user_id)
    quota = await withdraw_service.quota_status(user.user_id)
    headroom = await withdraw_service.payout_headroom(user.user_id)
    card = (
        t(
            "h_withdraw_intro",
            lang,
            balance=balance,
            coins_per_usdt=_fmt_rate(withdraw_config.coins_per_usdt),
            asset=withdraw_config.asset,
            min_coins=withdraw_config.min_coins,
            max_coins=withdraw_config.max_coins,
        )
        + "\n\n"
        + t(
            "h_withdraw_quota",
            lang,
            daily_remaining=quota.daily_remaining,
            daily_limit=quota.daily_limit,
            monthly_remaining=quota.monthly_remaining,
            monthly_limit=quota.monthly_limit,
        )
    )
    # T-020 (R6). Only rendered when the cap is armed: on a deployment
    # that runs with ``WITHDRAW_PAYOUT_RATIO=0`` there is no lifetime
    # limit to report, and a "∞ available" line would be noise.
    if headroom is not None:
        card += "\n\n" + t("h_withdraw_cap_line", lang, remaining=headroom)
    await state.set_state(WithdrawStates.awaiting_amount)
    await state.set_data({_DATA_LANG: lang, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await message.reply(card)
    log.bind(uid=user.user_id, balance=balance).info("/withdraw flow started")


async def handle_withdraw_amount(
    message: Message,
    state: FSMContext,
    economy_repo: EconomyRepo,
    withdraw_service: WithdrawService,
    withdraw_config: WithdrawConfig,
) -> None:
    """Parse + validate the amount; on success show the confirm card.

    Invalid / out-of-band / unaffordable amounts keep the user in
    ``awaiting_amount`` with a hint — no escrow happens here; coins are
    only held when the user confirms (next step).
    """
    tg_user = require_from_user(message)
    data = await state.get_data()
    lang = str(data.get(_DATA_LANG) or "ru")

    raw = (message.text or "").strip().replace(" ", "")
    if not is_int_token(raw):
        await message.reply(t("h_withdraw_amount_nan", lang))
        return
    amount = int(raw)
    if amount <= 0:
        await message.reply(t("h_withdraw_amount_nan", lang))
        return

    if amount < withdraw_config.min_coins:
        await message.reply(t("h_withdraw_below_min", lang, min_coins=withdraw_config.min_coins))
        return
    if amount > withdraw_config.max_coins:
        await message.reply(t("h_withdraw_above_max", lang, max_coins=withdraw_config.max_coins))
        return

    balance = await _balance_of(economy_repo, tg_user.id)
    if balance < amount:
        await message.reply(t("h_withdraw_insufficient", lang, balance=balance, amount=amount))
        return

    # Every limit below refuses the amount *before* the confirm card is
    # drawn, and every one of them is re-enforced server-side in
    # ``WithdrawService.create`` on confirm — defence in depth against a
    # stale card or a request that raced in between.
    #
    # T-019 (R2) / T-020 (R6): the two lifetime gates go first. They are
    # usually the binding ones, and unlike the rolling quotas they do not
    # reopen on a clock, so leading with "you are over today's limit"
    # would be a lie by omission.
    refusal = await withdraw_service.check_lifetime_gate(tg_user.id, amount)
    if refusal is not None:
        if refusal.outcome is CreateOutcome.NO_DEPOSITS:
            await message.reply(t("h_withdraw_no_deposits", lang))
        else:
            await message.reply(t("h_withdraw_payout_cap", lang, remaining=refusal.remaining or 0))
        return

    # L-92: then the rolling daily / monthly caps, each telling the user
    # the headroom left this cycle.
    quota = await withdraw_service.quota_status(tg_user.id)
    if amount > quota.daily_remaining:
        await message.reply(
            t(
                "h_withdraw_daily_quota",
                lang,
                remaining=quota.daily_remaining,
                limit=quota.daily_limit,
            )
        )
        return
    if amount > quota.monthly_remaining:
        await message.reply(
            t(
                "h_withdraw_monthly_quota",
                lang,
                remaining=quota.monthly_remaining,
                limit=quota.monthly_limit,
            )
        )
        return

    crypto = format_crypto_amount(withdraw_service.to_crypto(amount))
    await state.update_data({_DATA_AMOUNT: amount, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await state.set_state(WithdrawStates.awaiting_confirm)
    await message.reply(
        t(
            "h_withdraw_confirm_card",
            lang,
            amount=amount,
            crypto=crypto,
            asset=withdraw_config.asset,
        ),
        reply_markup=_confirm_keyboard(tg_user.id, lang),
    )
    log.bind(uid=tg_user.id, amount=amount).info("/withdraw amount accepted; awaiting confirm")


async def _edit_card(callback: CallbackQuery, text: str) -> None:
    """Best-effort replace the card text + drop its keyboard.

    Swallows the benign races (card already edited, message too old,
    bot blocked) — the FSM has already been cleared by the caller, so a
    failed cosmetic edit must not raise.
    """
    msg = callback.message
    if not isinstance(msg, MessageType):
        return
    try:
        await msg.edit_text(text)
    except (TelegramBadRequest, TelegramForbiddenError):
        log.bind(chat_id=msg.chat.id, message_id=msg.message_id).debug(
            "/withdraw card edit swallowed"
        )


def _format_admin_notification(
    *,
    request_id: int,
    uid: int,
    full_name: str,
    username: str,
    amount_com: int,
    crypto: str,
    asset: str,
) -> str:
    """Admin-chat DM body for a freshly created request (#237).

    RU-only by the same convention as
    ``support._format_admin_notification`` and
    ``ads._format_admin_notification`` — the admin chat is RU and the
    operator is one person, so a locale lookup here would buy nothing.

    Carries no payout destination. Legacy's crypto DM printed the
    address and its card DM printed the PAN (bot.py:20443, 20506); the
    port collects neither at request time, and even once it does, the
    destination belongs on the ``/admin_withdrawals`` card behind a
    private-only router — not in a push message that gets forwarded and
    screenshotted (#172). The id is the handle: the operator opens the
    panel to see the rest.

    Every user-controlled field is HTML-escaped — ``full_name`` and
    ``username`` are whatever Telegram carries.
    """
    handle = f"@{username}" if username else "—"
    return (
        "🏧 <b>Новая заявка на вывод</b>\n\n"
        f"🆔 Номер: <code>#{request_id}</code>\n"
        f"👤 Пользователь: {html.escape(full_name or '—')} ({html.escape(handle)})\n"
        f"🔑 ID: <code>{uid}</code>\n"
        f"💰 Сумма: <code>{amount_com}</code> DLAB → <code>{html.escape(crypto)}</code> "
        f"{html.escape(asset)}\n\n"
        "Обработать: /admin_withdrawals"
    )


async def handle_withdraw_confirm(
    callback: CallbackQuery,
    callback_data: WithdrawConfirm,
    state: FSMContext,
    economy_repo: EconomyRepo,
    withdraw_service: WithdrawService,
    withdraw_config: WithdrawConfig,
    bot: Bot,
    admin_chat_id: int,
) -> None:
    """✅ Confirm — create the escrow request, report the outcome."""
    user = callback.from_user
    data = await state.get_data()
    lang = str(data.get(_DATA_LANG) or "ru")

    # Auth tag: a click whose sender doesn't own the card is rejected
    # without touching the FSM (private chat makes this near-impossible,
    # but the guard is cheap and matches the /cpc / /duel posture).
    if user.id != callback_data.user_id:
        await callback.answer(t("h_withdraw_foreign_click", lang), show_alert=True)
        return

    # #777: read → clear → create is one critical section. Clearing
    # before creating is necessary but NOT sufficient on its own: the
    # read and the clear are two separate awaits, so without the lock a
    # double-tap has both coroutines reading the amount before either
    # clears, and both then call ``create`` against the same headroom.
    # See ``_confirm_locks`` for why aiogram gives us nothing to lean on.
    async with _confirm_locks.acquire((bot.id, user.id)):
        # Re-read the bag INSIDE the lock. The copy taken above (for the
        # auth tag's language) predates it, and the tap that lost the race
        # has to see the *consumed* state, not its own stale snapshot.
        data = await state.get_data()
        lang = str(data.get(_DATA_LANG) or lang)
        amount_raw = data.get(_DATA_AMOUNT)
        if not isinstance(amount_raw, int):
            # Data lost (sweeper reclaimed / restart), or the other half
            # of a double-tap already spent it — nothing to create.
            await state.clear()
            await _edit_card(callback, t("h_withdraw_expired", lang))
            await callback.answer()
            return
        amount = amount_raw

        # Clear *before* creating, not after: a confirmed card is
        # single-shot either way, but consuming the FSM data first means a
        # later re-click finds no amount to spend and lands on
        # ``h_withdraw_expired``. On a transient failure the user re-runs
        # /withdraw rather than re-clicking a stale card.
        await state.clear()
        result = await withdraw_service.create(user_id=user.id, amount_com=amount)

    if result.outcome is CreateOutcome.OK and result.request_id is not None:
        crypto = format_crypto_amount(result.amount_crypto or withdraw_service.to_crypto(amount))
        await _edit_card(
            callback,
            t(
                "h_withdraw_submitted",
                lang,
                request_id=result.request_id,
                amount=amount,
                crypto=crypto,
                asset=withdraw_config.asset,
            ),
        )
        await callback.answer()
        bound = log.bind(uid=user.id, request_id=result.request_id, amount=amount)
        bound.info("/withdraw request created")
        if admin_chat_id:
            try:
                await bot.send_message(
                    admin_chat_id,
                    _format_admin_notification(
                        request_id=result.request_id,
                        uid=user.id,
                        full_name=user.full_name,
                        username=user.username or "",
                        amount_com=amount,
                        crypto=crypto,
                        asset=withdraw_config.asset,
                    ),
                )
            except TelegramAPIError as exc:
                # Best-effort, same posture as ``support._notify_admin``:
                # the escrowed row is the source of truth and the user has
                # already seen the confirmation card, so a failed notify
                # rolls nothing back. The 24h staleness sweep in
                # ``economy_cleanup`` stays the backstop it always was —
                # this DM shortens the usual latency, it does not replace
                # the guarantee.
                bound.warning("admin notify failed: {e!r}", e=exc)
        return

    if result.outcome is CreateOutcome.INSUFFICIENT_FUNDS:
        balance = await _balance_of(economy_repo, user.id)
        await _edit_card(
            callback, t("h_withdraw_insufficient", lang, balance=balance, amount=amount)
        )
    elif result.outcome is CreateOutcome.DAILY_QUOTA_EXCEEDED:
        # The cap was breached between the amount step and this click
        # (e.g. a concurrent request, or the day rolled over a window the
        # FSM card straddled). ``remaining`` is the live headroom.
        await _edit_card(
            callback,
            t(
                "h_withdraw_daily_quota",
                lang,
                remaining=result.remaining or 0,
                limit=withdraw_service.daily_limit_coins,
            ),
        )
    elif result.outcome is CreateOutcome.MONTHLY_QUOTA_EXCEEDED:
        await _edit_card(
            callback,
            t(
                "h_withdraw_monthly_quota",
                lang,
                remaining=result.remaining or 0,
                limit=withdraw_service.monthly_limit_coins,
            ),
        )
    elif result.outcome is CreateOutcome.PAYOUT_CAP_EXCEEDED:
        # T-020 (R6). Sits between the two: the quotas reopen on a clock,
        # NO_DEPOSITS reopens on the first purchase, and this reopens on
        # every purchase — so the copy leads with the live headroom and
        # points at /topup as the lever the user actually controls.
        await _edit_card(
            callback, t("h_withdraw_payout_cap", lang, remaining=result.remaining or 0)
        )
    elif result.outcome is CreateOutcome.NO_DEPOSITS:
        # T-019 (R2). Unlike the quota rejections this does not reopen on
        # a clock boundary, so the card points at /topup rather than at
        # waiting — the account becomes eligible the moment it buys coins.
        await _edit_card(callback, t("h_withdraw_no_deposits", lang))
    else:
        # BELOW_MIN / ABOVE_MAX shouldn't reach here (validated at the
        # amount step against the same limits) — treat as a generic
        # failure rather than leaking an internal outcome name.
        await _edit_card(callback, t("h_withdraw_failed", lang))
    await callback.answer()
    log.bind(uid=user.id, amount=amount, outcome=result.outcome.value).warning(
        "/withdraw create rejected at confirm"
    )


async def handle_withdraw_cancel(
    callback: CallbackQuery,
    callback_data: WithdrawCancel,
    state: FSMContext,
) -> None:
    """❌ Cancel — clear the flow; nothing was escrowed."""
    user = callback.from_user
    data = await state.get_data()
    lang = str(data.get(_DATA_LANG) or "ru")
    if user.id != callback_data.user_id:
        await callback.answer(t("h_withdraw_foreign_click", lang), show_alert=True)
        return
    await state.clear()
    await _edit_card(callback, t("h_withdraw_cancelled", lang))
    await callback.answer()
    log.bind(uid=user.id).info("/withdraw flow cancelled")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Private-only withdraw router.

    Mounts :class:`EconomyMiddleware` (with the withdraw config, so it
    binds ``withdraw_service`` + ``economy_repo``) on BOTH the message
    and callback_query chains — the confirm/cancel buttons need the
    same economy session the amount step used. ``user_service`` rides
    the dispatcher-level :class:`SessionMiddleware` already, so only the
    economy session is mounted here. Aliases mirror legacy:
    ``/withdraw`` + RU ``/вывод``.
    """
    router = Router(name="withdraw")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)
    # #1608: the callback side is scoped too. ``wd_ok``/``wd_no`` are
    # rendered only by the private amount step, no legacy surface
    # emits either prefix (checked against bot.py), and both handlers
    # are already ``StateFilter``-gated on a per-chat FSM key — so a
    # group click could never have matched anyway. The filter makes
    # that scope explicit instead of implied by the storage key.
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    withdraw_config = settings.withdraw
    # #237: read once at wiring time, the way ``build_main_router``
    # already does for the support router. ``Settings`` is built once at
    # startup and nothing reassigns the field, so a per-call read (as in
    # ``ads.py:382``) would return the same value. ``0`` is the field
    # default and means "no admin chat configured": the notify is then
    # skipped rather than sent to chat id 0.
    admin_chat_id = settings.bot.admin_chat_id
    router.message.middleware(EconomyMiddleware(registry, withdraw_config=withdraw_config))
    router.callback_query.middleware(EconomyMiddleware(registry, withdraw_config=withdraw_config))

    async def _start(
        message: Message,
        state: FSMContext,
        user_service: UserService,
        economy_repo: EconomyRepo,
        withdraw_service: WithdrawService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_withdraw_start(
            message,
            state,
            user_service,
            economy_repo,
            withdraw_service,
            withdraw_config,
            checkpoint,
        )

    async def _amount(
        message: Message,
        state: FSMContext,
        economy_repo: EconomyRepo,
        withdraw_service: WithdrawService,
    ) -> None:
        await handle_withdraw_amount(
            message, state, economy_repo, withdraw_service, withdraw_config
        )

    async def _confirm(
        callback: CallbackQuery,
        callback_data: WithdrawConfirm,
        state: FSMContext,
        economy_repo: EconomyRepo,
        withdraw_service: WithdrawService,
        bot: Bot,
    ) -> None:
        await handle_withdraw_confirm(
            callback,
            callback_data,
            state,
            economy_repo,
            withdraw_service,
            withdraw_config,
            bot,
            admin_chat_id,
        )

    router.message.register(
        _start,
        Command("withdraw", "wd", "вывод", ignore_case=True),
        F.from_user,
    )
    router.message.register(
        _amount,
        StateFilter(WithdrawStates.awaiting_amount),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    register_text_expected(router, WithdrawStates.awaiting_amount)
    router.callback_query.register(
        _confirm,
        WithdrawConfirm.filter(),
        StateFilter(WithdrawStates.awaiting_confirm),
        F.from_user,
    )
    router.callback_query.register(
        handle_withdraw_cancel,
        WithdrawCancel.filter(),
        StateFilter(WithdrawStates.awaiting_confirm),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
