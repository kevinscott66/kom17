"""``/check`` + ``/create_check`` — coin-code vouchers (#26).

Replaces the Stage-14 static ``/check`` stub (removed from
``handlers/support.py``) with the real claim + create flow over
:class:`CheckService` (injected by :class:`EconomyMiddleware`).

Commands
--------
* ``/check`` / ``/чек``:
    - no args → render an info/help blurb.
    - one arg (a code) → claim flow. Maps each
      :class:`ClaimOutcome` to an i18n message; on OK credits the
      claimer and best-effort DMs the creator.
* ``/create_check`` / ``/создать_чек``: DEV-ONLY
  (``settings.bot.is_developer``). Parses the legacy syntax
  (``random <min> <max> <count>`` | ``fixed <amount> <count>`` |
  ``individual <amount> @user`` + optional ``lang=`` / ``premium=`` /
  ``sub=`` / ``expires=`` trailers, all whitelisted) and renders the
  ``t.me?start=check_<code>`` deep link.

Card contents (RR-2 #17/#18)
----------------------------
The create receipt shows the PER-CLAIM amount (a range for ``random``),
the activations count, the individual-target line, and the expiry —
every one of which the monolith→split port had dropped down to a single
"Чек создан! Код … Списано …" line. An individual check additionally
DMs its recipient, and — the part that matters beyond cosmetics — the
handle is now resolved to a ``target_user_id`` so the WRONG_USER gate
actually binds; before this, a "personal" check was claimable by anyone
holding the code. The claim card shows the REMAINING ACTIVATIONS counter
(``∞`` when unlimited) alongside the coin remainder; the port had shown
only the coins, which answers a different question than legacy's card.

Parse mode is HTML (the bot default); every user-controlled field
(code, target handle) is routed through :func:`html.escape`.
Private-chat-only — a group call gets the #123 refusal twin, matching
every other ported economy stub.
"""

from __future__ import annotations

import contextlib
import html
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.checks import (
    CheckCreateCancel,
    CheckCreateConfirm,
    CheckCreateType,
    CheckSubVerify,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.services.check_service import (
    CheckSpec,
    ClaimOutcome,
    ClaimResult,
    CreateOutcome,
)
from telegram_invite_bot.utils.aiogram import (
    command_args,
    edit_card,
    reply_or_send,
    require_from_user,
)
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.language import lang_from_code
from telegram_invite_bot.utils.numbers import parse_int_token


class CheckCreateStates(StatesGroup):
    """FSM states for the interactive ``/check_create`` flow (L-30).

    Defined HERE (not in ``fsm/``) to avoid touching the shared
    ``fsm/__init__.py`` — this handler module wholly owns the create
    interview. No coins are escrowed until the final confirm step (the
    debit lives in :meth:`CheckService.create_check`), so an abandoned
    interview holds NO money; the only lingering effect is the FSM
    busy-lock that ``/cancel`` clears.

    Steps:

    * ``awaiting_amount`` — type already picked (stored in FSM data via
      the type-picker keyboard); the next free-text message is parsed as
      the amount spec for that type (``<min> <max>`` for random, a single
      ``<amount>`` for fixed/individual).
    * ``awaiting_count`` — random/fixed only: number of activations.
      (individual is implicitly single-claim and skips this step.)
    * ``awaiting_confirm`` — summary card with ✅/❌; confirm runs
      ``create_check`` and shows the code + deep link.
    """

    awaiting_amount = State()
    awaiting_count = State()
    awaiting_confirm = State()


# FSM data keys for the create flow.
_CD_LANG = "lang"
_CD_TYPE = "ctype"
_CD_MIN = "min_amount"
_CD_MAX = "max_amount"
_CD_FIXED = "fixed_amount"
_CD_COUNT = "max_claims"

# #1762: one lock per (bot, user) around the create-confirm step's
# read-state -> validate -> clear-state span.
#
# Same registry and same shape as ``handlers/withdraw.py``'s #777 block,
# which documents why nothing else in the stack serialises this for us:
# the dispatcher is built without ``events_isolation``, so two taps on
# the same confirm button are two concurrent coroutines;
# ``StateFilter`` resolves through an awaited ``get_state()`` and both
# taps pass it before either reaches ``clear()``; and the throttling
# bucket is 10 deep.
#
# Unlike ``handlers/shop.py``'s ``_spent_cards`` claim, a lock IS the
# right tool here: this card's FSM data is a real single-use resource
# that the winning tap consumes, so serialising the read-then-consume
# span is exactly what closes the double-fund.
_confirm_locks: KeyedLocks[tuple[int, int]] = KeyedLocks()

# Telegram member statuses that count as "subscribed" for the claim-side
# subscription gate. ``RESTRICTED`` members are still in the chat
# (is_member may be true) but legacy treated only the three "present and
# unrestricted" states as subscribed; we mirror that conservative set.
_SUBSCRIBED_STATUSES = frozenset(
    {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }
)

log = logger.bind(component="handlers.checks")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.check_service import CheckService


def _utcnow() -> datetime:
    """Naive UTC ``now`` — the codebase's stored-datetime convention.

    DIVERGENCE from legacy, deliberate and safe only because the port is
    the sole writer. Legacy stamped ``checks.expires_at`` with
    ``datetime.now()`` — naive LOCAL (MSK) — and stored it via
    ``.isoformat()``, i.e. a ``'T'`` separator (bot.py:25327), reading it
    back through the same local frame (bot.py:10109-10118). The port
    writes naive UTC through SQLAlchemy, whose SQLite bind processor
    renders a SPACE separator, and reads it in that same frame
    everywhere: the claim gates here, ``CheckService.claim_check`` and
    the ``economy_cleanup`` sweep. Internally consistent, but the two
    frames are three hours and one separator byte apart, so a surviving
    legacy row would both compare wrong (see
    :meth:`ChecksRepo.deactivate_expired`, a byte-wise TEXT compare) and
    expire three hours late. None exist: the legacy service does not run
    in production and the ``checks`` table there is empty. Reconciling
    old rows only becomes a question if legacy is ever revived."""
    return datetime.now(UTC).replace(tzinfo=None)


# Map each non-OK claim outcome to its i18n key. OK is handled
# separately (it interpolates amount/remaining).
_CLAIM_ERROR_KEYS: dict[ClaimOutcome, str] = {
    ClaimOutcome.EMPTY_CODE: "h_check_empty_code",
    ClaimOutcome.NOT_FOUND: "h_check_not_found",
    ClaimOutcome.EXPIRED: "h_check_expired",
    ClaimOutcome.MAX_REACHED: "h_check_max_reached",
    ClaimOutcome.WRONG_USER: "h_check_wrong_user",
    ClaimOutcome.ALREADY_CLAIMED: "h_check_already_claimed",
    ClaimOutcome.PREMIUM_ONLY: "h_check_premium_only",
    ClaimOutcome.NO_FUNDS: "h_check_no_funds",
    ClaimOutcome.RACE_LOST: "h_check_race_lost",
    ClaimOutcome.CREDIT_FAILED: "h_check_credit_failed",
    # L-85..L-88 claim-time filter gates.
    ClaimOutcome.BLOCKED_USER: "h_check_blocked_user",
    ClaimOutcome.MIN_AGE: "h_check_min_age",
    ClaimOutcome.MIN_ACTIVITY: "h_check_min_activity",
    ClaimOutcome.COUNTRY_BLOCKED: "h_check_country_blocked",
}


# Legacy's unlimited-activations glyph (bot.py:10188:
# ``max_claims - new_claims if max_claims > 0 else '∞'``). Kept
# language-neutral on purpose — a translated word here would break the
# card's column alignment for no gain.
_UNLIMITED = "∞"


def _claims_left_text(claims_left: int | None) -> str:
    """Render the remaining-activations counter (RR-2 #18).

    ``None`` is the service's "unlimited" sentinel (``max_claims = 0``)
    and renders as ``∞``, exactly as legacy did. Note this counts PEOPLE,
    not coins — the coin figure is ``remaining`` and gets its own line.
    """
    return _UNLIMITED if claims_left is None else str(claims_left)


def _claim_receipt(result: ClaimResult, lang: str) -> str:
    """The claimer-facing card for a successful claim (RR-2 #18).

    Legacy showed "получено" + the activations counter; the port had
    replaced the counter with the coin remainder. We show BOTH — a
    claimer wants to know whether it's worth telling a friend (are there
    activations left?) *and* how fat the check still is.
    """
    return t(
        "h_check_claimed",
        lang,
        amount=result.amount,
        claims_left=_claims_left_text(result.claims_left),
        remaining=result.remaining,
    )


def _claim_notify(result: ClaimResult, *, display: str, lang: str) -> str:
    """The creator-facing DM when someone claims their check.

    Same numbers as the claimer's card plus who took it. ``display`` is
    already HTML-escaped by the caller (it's a Telegram full name — fully
    user-controlled).
    """
    return t(
        "h_check_claim_notify",
        lang,
        code=html.escape(result.code),
        name=display,
        amount=result.amount,
        claims_left=_claims_left_text(result.claims_left),
        remaining=result.remaining,
    )


def _create_details(spec: CheckSpec, *, target_display: str | None, lang: str) -> str:
    """The middle block of the create receipt (RR-2 #17).

    Restores the three lines the split dropped: the PER-CLAIM amount (a
    range for ``random``, a flat figure otherwise), the ACTIVATIONS
    count, and the individual-target line. ``expires`` is a bonus over
    legacy — the ``expires=`` trailer was accepted but never echoed, so
    the creator had no way to confirm it landed.
    """
    lines: list[str] = []
    # ``is not None``, not truthiness, so this renderer's own contract is
    # "was the field supplied?" rather than "is it non-zero?". Today the
    # service rejects a zero amount upstream (``_compute_total`` →
    # INVALID_AMOUNT), so the two spellings agree; keeping the explicit
    # form means relaxing that floor later can't silently drop the amount
    # line — the one number the creator most wants echoed back.
    if spec.type == "random" and spec.min_amount is not None and spec.max_amount is not None:
        lines.append(
            t(
                "h_check_created_amount_random",
                lang,
                min=spec.min_amount,
                max=spec.max_amount,
            )
        )
    elif spec.fixed_amount is not None:
        lines.append(t("h_check_created_amount_fixed", lang, amount=spec.fixed_amount))
    if target_display is not None:
        lines.append(t("h_check_created_target", lang, target=target_display))
    else:
        # A check ADDRESSED to someone is single-claim by construction, so
        # "👥 Активаций: 1" would be noise — legacy's personal receipt
        # (bot.py:25401) shows recipient + amount and nothing else. Every
        # other shape gets the counter, exactly as legacy did.
        lines.append(
            t(
                "h_check_created_claims",
                lang,
                count=spec.max_claims if spec.max_claims > 0 else _UNLIMITED,
            )
        )
    if spec.expires_at is not None:
        # Round UP so a 24h check never advertises "23 h": the creator
        # typed ``expires=24`` and the float division would otherwise
        # shave the tail off by a few microseconds of handler latency.
        seconds = (spec.expires_at - spec.now).total_seconds()
        hours = max(1, -(-int(seconds) // 3600))
        lines.append(t("h_check_created_expires", lang, hours=hours))
    return "\n".join(lines)


def _claim_block(code: str, *, bot_username: str | None, lang: str) -> str:
    """ "How to redeem" footer: deep link when we know the bot's handle,
    the plain ``/check <code>`` command otherwise."""
    if bot_username:
        link = f"https://t.me/{bot_username}?start=check_{code}"
        return t(
            "h_check_created_claim_link",
            lang,
            link=html.escape(link),
            code=html.escape(code),
        )
    return t("h_check_created_claim_code", lang, code=html.escape(code))


def _create_receipt(
    spec: CheckSpec,
    *,
    code: str,
    total: int,
    bot_username: str | None,
    target_display: str | None,
    lang: str,
) -> str:
    """Full create receipt (RR-2 #17): title + details + charge + footer.

    ``target_display`` is the already-escaped ``@handle`` of an
    individual check's recipient (``None`` for every other shape); it
    also selects the "персональный чек" title, matching legacy's two
    distinct success messages.
    """
    title_key = (
        "h_check_created_title_individual"
        if target_display is not None
        else "h_check_created_title"
    )
    return t(
        "h_check_created",
        lang,
        title=t(title_key, lang),
        code=html.escape(code),
        details=_create_details(spec, target_display=target_display, lang=lang),
        total=total,
        claim=_claim_block(code, bot_username=bot_username, lang=lang),
    )


async def _is_subscribed(bot: Bot, channel: str, user_id: int) -> bool:
    """Return True iff ``user_id`` is a present, unrestricted member of
    ``channel`` (member / administrator / creator).

    FAIL-CLOSED: a private channel the bot can't read, the bot not being
    an admin of ``channel``, a deleted channel, or any transient API
    error all surface as :class:`Exception` here — every one is treated
    as "not subscribed" (returns ``False``) rather than crashing or
    fail-open-crediting. The user then sees the subscribe prompt; if the
    misconfiguration is on the operator side (bot not admin) the gate is
    safely closed until it's fixed, never silently bypassed.
    """
    try:
        member = await bot.get_chat_member(channel, user_id)
    except Exception as exc:  # noqa: BLE001 — fail-closed by contract
        log.warning(
            "check sub-gate get_chat_member failed (channel={ch}, user={uid}): {e!r}",
            ch=channel,
            uid=user_id,
            e=exc,
        )
        return False
    return member.status in _SUBSCRIBED_STATUSES


def _sub_prompt_markup(code: str, lang: str) -> InlineKeyboardMarkup:
    """Inline keyboard for the subscription-gate prompt: a single
    "✅ Я подписался" verify button carrying the check ``code``. Clicking
    it re-checks channel membership and, if subscribed, runs the claim.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("check_sub_btn", lang),
            callback_data=CheckSubVerify(code=code).pack(),
        )
    )
    return builder.as_markup()


def _sub_prompt_text(channel: str, lang: str) -> str:
    """Subscription-gate prompt body. Reuses the existing
    ``check_subscribe_required`` line, appends the channel link (when the
    handle is a public ``@username``), and the ``check_sub_hint`` nudge.
    HTML parse mode — the channel handle is operator-controlled config,
    not user input, but we escape defensively anyway.
    """
    lines = [t("check_subscribe_required", lang)]
    if channel.startswith("@"):
        safe = html.escape(channel[1:])
        lines.append(f'➡️ <a href="https://t.me/{safe}">@{safe}</a>')
    lines.append(t("check_sub_hint", lang))
    return "\n".join(lines)


def _channel(settings: Settings) -> str | None:
    """The configured subscription channel handle/id, or ``None``.

    Trimmed so an accidental whitespace-only env value reads as unset.
    """
    raw = settings.economy.subscription_channel
    raw = raw.strip() if raw else ""
    return raw or None


async def _gate_blocked(
    bot: Bot,
    check_service: CheckService,
    settings: Settings,
    *,
    code: str,
    user_id: int,
) -> bool:
    """Return True when the claim must be BLOCKED by the subscription gate.

    Blocked iff: the check requires a subscription, a channel is
    configured, AND the user is not a verified member. When the check
    doesn't require a subscription, we never gate. When it requires one
    but NO channel is configured, there is nothing to verify against — we
    treat it as a PASS (the bool column alone can't name a channel; see
    ``EconomyConfig.subscription_channel``) and let the claim proceed.
    """
    if not await check_service.requires_subscription(code=code):
        return False
    channel = _channel(settings)
    if channel is None:
        return False
    return not await _is_subscribed(bot, channel, user_id)


async def handle_check(
    message: Message,
    command: CommandObject,
    bot: Bot,
    check_service: CheckService,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/check`` — info blurb (no args) or claim flow (one arg = code)."""
    arg = command_args(command)
    if not arg:
        await message.reply(t("h_check_info", lang))
        return

    # One token only — the code. Extra tokens are ignored (the code is
    # the first word). The service re-normalises (strip + upper).
    await _claim_and_render(
        message,
        bot,
        check_service,
        settings,
        code=arg.split()[0],
        lang=lang,
        checkpoint=checkpoint,
    )


async def handle_start_check(
    message: Message,
    command: CommandObject,
    bot: Bot,
    check_service: CheckService,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/start check_<code>`` — deep-link claim entry point.

    The ``/create_check`` flow hands out a ``t.me/<bot>?start=check_<code>``
    link; clicking it opens a private chat and fires ``/start`` with the
    payload ``check_<code>``. The bare-``/start`` handler
    (``handlers/start.py``) only matches when there are NO args, so this
    payload variant lands here. We strip the ``check_`` prefix and route
    through the exact same claim+render path as ``/check <code>`` so the
    two entry points can never diverge.
    """
    payload = command.args or ""
    code = payload[len("check_") :] if payload.startswith("check_") else payload
    if not code:
        await message.reply(t("h_check_empty_code", lang))
        return
    await _claim_and_render(
        message, bot, check_service, settings, code=code, lang=lang, checkpoint=checkpoint
    )


async def _claim_and_render(
    message: Message,
    bot: Bot,
    check_service: CheckService,
    settings: Settings,
    *,
    code: str,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Run a claim for ``code`` and render the outcome.

    Shared by ``/check <code>`` and the ``/start check_<code>`` deep link
    so a future change to the receipt / creator-notification lands in one
    place. On OK, credits the claimer and best-effort DMs the creator.

    #694: the claim is committed at the checkpoint, before the receipt
    and the creator DM. The session middleware otherwise commits only
    after this handler returns (``middlewares/base.py:157-158``), which
    would hold ``economy.db``'s single writer slot across up to two
    Telegram round-trips and push every concurrent writer into its 5 s
    ``busy_timeout`` (``db/pragma.py:63``). Committing early costs
    nothing here because both calls that follow are already best-effort
    by design — see the block comments below and
    :class:`db.session.Checkpoint`.

    CLAIM-SIDE SUBSCRIPTION GATE (#26): if the check requires a
    subscription, verify channel membership BEFORE attempting the claim.
    A non-member gets the subscribe prompt + verify button and NO credit
    / decrement happens; a member falls through to the normal claim.
    """
    user = require_from_user(message)

    # Subscription pre-check. ``requires_subscription`` is a pure data
    # read (no money moves); the membership verification is the handler's
    # job because it needs ``bot.get_chat_member``. We only gate when the
    # check wants it AND a channel is configured — see ``_gate_blocked``.
    if await _gate_blocked(bot, check_service, settings, code=code, user_id=user.id):
        await message.reply(
            _sub_prompt_text(_channel(settings) or "", lang),
            reply_markup=_sub_prompt_markup(code, lang),
            disable_web_page_preview=True,
        )
        return

    result = await check_service.claim_check(
        user_id=user.id,
        code=code,
        is_premium=bool(getattr(user, "is_premium", False)),
        user_lang=lang,
        now=_utcnow(),
    )

    if checkpoint is not None:
        await checkpoint()

    if result.outcome is ClaimOutcome.OK:
        # The claim is spent and, past the checkpoint above, committed:
        # the activation counter is decremented and the coins are on the
        # claimer's wallet. A deep link claimed from a group where the
        # command message has since been deleted must still produce the
        # receipt — losing the reply *target* is not a reason to hand
        # the update to ``handlers.errors`` and show "⚠️ Произошла
        # ошибка" for a check the user really did claim. And unlike the
        # ``/buy`` receipt (handlers/shop.py), an undeliverable receipt
        # here is NOT a reason to unwind: a claim only ever adds coins,
        # so the claimer loses nothing by not seeing the card — the
        # balance is theirs either way. Log it and let the claim stand.
        if not await reply_or_send(message, _claim_receipt(result, lang)):
            log.bind(uid=user.id, amount=result.amount).warning("check claim receipt undeliverable")
        # Best-effort DM to the creator — a delivery failure (creator
        # blocked the bot) must NOT roll back the claim. The catch stays
        # even though the checkpoint has already committed: an escaping
        # ``TelegramAPIError`` would still reach ``handlers.errors`` and
        # tell the claimer their claim failed when it did not.
        if result.creator_id and result.creator_id != user.id:
            display = html.escape(user.full_name or str(user.id))
            try:
                await bot.send_message(
                    result.creator_id, _claim_notify(result, display=display, lang=lang)
                )
            except TelegramAPIError as exc:
                log.warning(
                    "check claim notify failed for {cid}: {e!r}",
                    cid=result.creator_id,
                    e=exc,
                )
        log.bind(uid=user.id, amount=result.amount).info("/check claimed")
        return

    if result.outcome is ClaimOutcome.WRONG_LANG:
        await message.reply(
            t("h_check_wrong_lang", lang, lang=html.escape(result.required_language))
        )
        return

    await message.reply(t(_CLAIM_ERROR_KEYS[result.outcome], lang))


async def handle_check_sub_verify(
    callback: CallbackQuery,
    callback_data: CheckSubVerify,
    bot: Bot,
    check_service: CheckService,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """ "✅ Я подписался" verify button — re-check membership, then claim.

    Re-runs the membership check for the configured channel. STILL not
    subscribed → answer the callback with the ``check_sub_fail`` toast and
    leave the prompt in place (the user can re-press after subscribing).
    Subscribed → run the exact same claim path as ``/check <code>`` and
    post the receipt; the unique-claim guard in
    :meth:`CheckService.claim_check` still prevents a double credit.

    #1201: the claim is committed at the checkpoint, exactly as on the
    message path (:func:`_claim_and_render`). Without it the write
    transaction stayed open across up to three Telegram round-trips —
    holding ``economy.db``'s single writer slot (``db/pragma.py:63``)
    and making the first of those calls able to unwind a claim the user
    had already been told succeeded.
    """
    user = callback.from_user
    code = callback_data.code
    channel = _channel(settings)

    # Re-verify only when a channel is configured AND the check still
    # wants a subscription. If either is no longer true (channel unset, or
    # the check changed), fall through to the claim — the claim path
    # itself re-asserts every money gate.
    if (
        channel is not None
        and await check_service.requires_subscription(code=code)
        and not await _is_subscribed(bot, channel, user.id)
    ):
        await callback.answer(t("check_sub_fail", lang), show_alert=True)
        return

    result = await check_service.claim_check(
        user_id=user.id,
        code=code,
        is_premium=bool(getattr(user, "is_premium", False)),
        user_lang=lang,
        now=_utcnow(),
    )

    if checkpoint is not None:
        await checkpoint()

    # Confirm the subscription was accepted (toast), then render the
    # claim outcome as a fresh message under the prompt.
    await callback.answer(t("check_sub_ok", lang))
    target = callback.message
    if target is None:
        # Inline-message callback with no attached message — nothing to
        # reply under. Past the checkpoint the claim really has committed
        # (or failed); the toast above is the only feedback channel here.
        return

    if result.outcome is ClaimOutcome.OK:
        # #1202: same policy as the message path — an undeliverable
        # receipt is not a reason to unwind, because a claim only ever
        # adds coins and the balance is the claimer's either way. This
        # one was bare while the creator DM below was guarded, so a
        # deleted prompt message took back coins the toast above had
        # already announced.
        try:
            await target.answer(_claim_receipt(result, lang))
        except TelegramAPIError as exc:
            log.bind(uid=user.id, amount=result.amount).warning(
                "check claim receipt undeliverable via sub-verify: {e!r}", e=exc
            )
        if result.creator_id and result.creator_id != user.id:
            display = html.escape(user.full_name or str(user.id))
            try:
                await bot.send_message(
                    result.creator_id, _claim_notify(result, display=display, lang=lang)
                )
            except TelegramAPIError as exc:
                log.warning(
                    "check claim notify failed for {cid}: {e!r}",
                    cid=result.creator_id,
                    e=exc,
                )
        log.bind(uid=user.id, amount=result.amount).info("/check claimed via sub-verify")
        return

    if result.outcome is ClaimOutcome.WRONG_LANG:
        await target.answer(
            t("h_check_wrong_lang", lang, lang=html.escape(result.required_language))
        )
        return

    await target.answer(t(_CLAIM_ERROR_KEYS[result.outcome], lang))


_CHECK_CREATE_EXTRAS = frozenset({"lang", "premium", "sub", "expires"})

# Positional arity per type: ``random <min> <max> <count>``,
# ``fixed <amount> <count>``, ``individual <amount> [@user]`` (the handle
# is optional here and refused further down with a specific message).
_CHECK_CREATE_ARITY: dict[str, tuple[int, int]] = {
    "random": (4, 4),
    "fixed": (3, 3),
    "individual": (2, 3),
}


async def handle_create_check(
    message: Message,
    command: CommandObject,
    bot: Bot,
    check_service: CheckService,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/create_check`` — DEV-ONLY check creation (legacy parity).

    #694: the creation is committed at the checkpoint, before the
    recipient DM and the receipt. The trade-off is deliberate. Left
    open, the funded check would hold ``economy.db``'s single writer
    slot across ``_notify_check_target`` — a DM to a third party, whose
    latency is not ours — and every concurrent writer would burn its 5 s
    ``busy_timeout`` (``db/pragma.py:63``) behind it. Committing early
    means an undeliverable *receipt* no longer unwinds the creation, so
    the receipt goes out via ``reply_or_send`` (same idiom as the claim
    path) and a failure is logged rather than swallowed silently. The
    creator is a developer by the gate below, and the code is on the
    check row either way.
    """
    user = require_from_user(message)

    if not settings.bot.is_developer(user.id):
        await message.reply(t("h_check_create_dev_only", lang))
        return

    raw = command_args(command)
    parts = raw.split() if raw else []
    if len(parts) < 3:
        await message.reply(t("h_check_create_usage", lang))
        return

    # Parse trailing key=value extras (lang= / premium= / sub= / expires=).
    # Only the keys in ``_CHECK_CREATE_EXTRAS`` count. An ``=`` alone used
    # to be enough, so a misspelt ``premuim=true`` was filed under its own
    # typo, left ``req_premium`` False, and the check was still minted with
    # the gate off — after the creator had been debited. That is the #745
    # bug class ``handlers/promo.py:172-187`` already fixed for
    # ``/promo_create`` (#1200).
    extras: dict[str, str] = {}
    positional: list[str] = []
    for tok in parts:
        key, sep, value = tok.partition("=")
        normalized = key.strip().lower()
        if sep and normalized in _CHECK_CREATE_EXTRAS:
            extras[normalized] = value.strip()
        else:
            positional.append(tok)

    # ``len(parts) >= 3`` above does NOT guarantee a positional token —
    # all three could be ``key=value`` extras, leaving ``positional``
    # empty. ``positional[0]`` here is OUTSIDE the try/except that guards
    # the later index accesses, so without this check it raises an
    # uncaught IndexError (BUG audit). Fall back to the usage message.
    if not positional:
        await message.reply(t("h_check_create_usage", lang))
        return

    ctype = positional[0].lower()

    # The whitelist alone only moves an unrecognised token into
    # ``positional``, where the per-type index reads below would ignore
    # it just as silently. The bound is what turns it into a refusal.
    lo, hi = _CHECK_CREATE_ARITY.get(ctype, (0, len(positional)))
    if not lo <= len(positional) <= hi:
        await message.reply(t("h_check_create_usage", lang))
        return

    req_lang = extras.get("lang") or None
    req_premium = extras.get("premium", "").lower() == "true"
    req_sub = extras.get("sub", "").lower() == "true"
    expires_at = None
    if "expires" in extras:
        # ``OverflowError`` sits next to ``ValueError`` because the hour
        # count has two ways of being unusable, and only one of them is a
        # parse failure. A big-enough number parses fine and then fails to
        # become a date: ``timedelta`` refuses past 999999999 days, and a
        # count that survives that still pushes the result past year 9999
        # on the addition. Both raise OverflowError from inside this
        # expression, so before this they escaped as an unhandled error
        # instead of the usage hint every other unusable value gets.
        try:
            hours = int(extras["expires"].lower().replace("h", "") or "0")
            if hours > 0:
                expires_at = _utcnow() + timedelta(hours=hours)
        except (ValueError, OverflowError):
            await message.reply(t("h_check_create_usage", lang))
            return

    now = _utcnow()
    try:
        if ctype == "random":
            spec = CheckSpec(
                type="random",
                now=now,
                min_amount=int(positional[1]),
                max_amount=int(positional[2]),
                max_claims=int(positional[3]),
                required_language=req_lang,
                required_premium=req_premium,
                required_subscription=req_sub,
                expires_at=expires_at,
            )
            target_handle = None
        elif ctype == "fixed":
            spec = CheckSpec(
                type="fixed",
                now=now,
                fixed_amount=int(positional[1]),
                max_claims=int(positional[2]),
                required_language=req_lang,
                required_premium=req_premium,
                required_subscription=req_sub,
                expires_at=expires_at,
            )
            target_handle = None
        elif ctype == "individual":
            # ``individual <amount> @user``. The handle → user_id lookup
            # runs AFTER this block (it needs an await against users.db
            # and must not sit inside the ValueError/IndexError guard,
            # which would swallow a repo error as a "bad syntax" reply).
            target_handle = positional[2] if len(positional) > 2 else None
            spec = CheckSpec(
                type="individual",
                now=now,
                fixed_amount=int(positional[1]),
                max_claims=1,
                required_language=req_lang,
                required_premium=req_premium,
                required_subscription=req_sub,
                expires_at=expires_at,
            )
        else:
            await message.reply(t("h_check_create_usage", lang))
            return
    except (ValueError, IndexError):
        await message.reply(t("h_check_create_usage", lang))
        return

    # RR-2 #17: bind the individual check to a real recipient BEFORE any
    # money moves. Until now the handle was parsed and thrown away, which
    # left ``target_user_id`` NULL — and the WRONG_USER gate in
    # :meth:`CheckService.claim_check` is keyed off exactly that column,
    # so a "personal" check was in fact claimable by the first person who
    # saw the code. Refusing to create an unbindable individual check
    # (legacy: "❌ Пользователь не найден в базе.", bot.py:25380) is the
    # only posture that keeps the word "personal" true.
    target_display: str | None = None
    target_lang = lang
    if ctype == "individual":
        if not target_handle:
            await message.reply(t("h_check_create_target_required", lang))
            return
        handle = target_handle.lstrip("@")
        recipient = await users_repo.get_by_username(handle)
        if recipient is None:
            await message.reply(
                t(
                    "h_check_create_target_unknown",
                    lang,
                    target=html.escape(f"@{handle}"),
                )
            )
            return
        spec = replace(spec, target_user_id=recipient.user_id)
        target_display = html.escape(f"@{handle}")
        # DM the recipient in THEIR language: an explicit /lang choice
        # wins, then the Telegram locale, then the creator's language.
        target_lang = await user_settings_repo.get_language(recipient.user_id) or lang_from_code(
            recipient.language_code
        )

    result = await check_service.create_check(creator_id=user.id, spec=spec)
    if checkpoint is not None:
        await checkpoint()

    if result.outcome is CreateOutcome.INSUFFICIENT_FUNDS:
        # total isn't carried on the failure result; recompute for the
        # message is not worth it — show a generic insufficient note.
        await message.reply(t("h_check_create_insufficient", lang, total="—"))
        return
    if result.outcome is CreateOutcome.INVALID_AMOUNT:
        await message.reply(t("h_check_create_invalid", lang))
        return

    bot_username = await _bot_username(bot)
    receipt = _create_receipt(
        spec,
        code=result.code,
        total=result.total_amount,
        bot_username=bot_username,
        target_display=target_display,
        lang=lang,
    )

    # RR-2 #17: hand the gift to its recipient directly (legacy
    # bot.py:25395). Best-effort — a closed inbox must not undo a check
    # that is already funded, so the failure becomes a line on the
    # creator's receipt telling them to pass the link along. Past the
    # #694 checkpoint the creation is committed, so the swallow no
    # longer guards a rollback; it guards the *receipt*, which the
    # creator needs to learn the code.
    if spec.target_user_id:
        delivered = await _notify_check_target(
            bot,
            target_id=spec.target_user_id,
            sender=html.escape(user.full_name or str(user.id)),
            amount=result.total_amount,
            code=result.code,
            bot_username=bot_username,
            lang=target_lang,
        )
        note_key = (
            "h_check_create_target_dm_sent" if delivered else "h_check_create_target_dm_failed"
        )
        receipt = f"{receipt}\n\n{t(note_key, lang)}"

    if not await reply_or_send(message, receipt):
        # #694: the check is committed, so a lost reply target must not
        # raise into ``handlers.errors`` — that would tell a developer
        # the creation failed when it did not. Same posture as the claim
        # receipt above.
        log.bind(uid=user.id, code=result.code).warning("check create receipt undeliverable")

    log.bind(
        uid=user.id,
        code=result.code,
        total=result.total_amount,
        target=spec.target_user_id,
    ).info("/create_check created")


async def _notify_check_target(
    bot: Bot,
    *,
    target_id: int,
    sender: str,
    amount: int,
    code: str,
    bot_username: str | None,
    lang: str,
) -> bool:
    """DM the recipient of an individual check. ``True`` iff delivered.

    ``sender`` is already HTML-escaped by the caller. Every Telegram
    failure (blocked bot, never-started chat, transient API error) is one
    outcome here — "we couldn't hand it over" — because the creator's
    remedy is identical in all of them: send the link themselves.
    """
    if bot_username:
        link = f"https://t.me/{bot_username}?start=check_{code}"
        text = t(
            "h_check_target_dm",
            lang,
            name=sender,
            amount=amount,
            link=html.escape(link),
        )
    else:
        text = t(
            "h_check_target_dm_code",
            lang,
            name=sender,
            amount=amount,
            code=html.escape(code),
        )
    try:
        await bot.send_message(target_id, text)
    except TelegramAPIError as exc:
        log.warning("check target notify failed for {tid}: {e!r}", tid=target_id, e=exc)
        return False
    return True


async def _bot_username(bot: Bot) -> str | None:
    """Return the bot's @username, or None if the lookup fails.

    ``bot.me()``, not ``bot.get_me()``: only the former memoises on the
    Bot instance (aiogram ``client/bot.py:371-379`` caches into
    ``self._me``; ``get_me`` at ``:1803-1817`` issues a fresh ``getMe``
    every single time). #693: the docstring here used to claim the
    opposite, which is why every ``/create_check`` paid for a Telegram
    round-trip — and paid for it holding the economy write lock, since
    this runs between the check INSERT and the middleware commit. The
    same reasoning is spelled out at ``handlers/chat_scope.py:125-129``.

    A network blip just degrades to the code-only success message.
    """
    try:
        me = await bot.me()
    except TelegramAPIError:
        return None
    return me.username


# ---------------------------------------------------------------------------
# L-30: interactive ``/check_create`` FSM (available to every user).
#
# Distinct from the DEV-ONLY one-shot ``/create_check`` above (legacy
# parity): this is the user-facing "build a check step by step" flow.
# No coins move until the final confirm — every prior step is pure
# input collection, so an abandoned interview costs nothing.
# ---------------------------------------------------------------------------

_CREATE_TYPES = ("random", "fixed", "individual")


async def on_expire_check_create(bot: Bot, key: object, data: dict[str, object]) -> None:
    """FSM-sweeper timeout callback for the ``/check_create`` interview.

    No coins are escrowed until the confirm step runs ``create_check``,
    so an abandoned interview holds NO money — the only lingering effect
    is the busy-lock that ``handle_check_create_start`` rejects re-entry
    against. The sweeper clears the FSM after this returns; we DM the
    user so they know the session lapsed and can ``/check_create`` again.
    """
    user_id = getattr(key, "user_id", None)
    if not isinstance(user_id, int):
        return
    lang_raw = data.get(_CD_LANG)
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(user_id, t("h_check_create_expired", lang))
    log.bind(uid=user_id).info("/check_create interview expired by sweeper")


def _type_keyboard(owner_id: int, lang: str) -> InlineKeyboardMarkup:
    """Type-picker keyboard (random / fixed / individual)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("h_check_create_type_random", lang),
            callback_data=CheckCreateType(owner_id=owner_id, ctype="random").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t("h_check_create_type_fixed", lang),
            callback_data=CheckCreateType(owner_id=owner_id, ctype="fixed").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t("h_check_create_type_individual", lang),
            callback_data=CheckCreateType(owner_id=owner_id, ctype="individual").pack(),
        )
    )
    return builder.as_markup()


def _confirm_keyboard(owner_id: int, lang: str) -> InlineKeyboardMarkup:
    """✅/❌ keyboard under the create-summary card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_check_create_btn_confirm", lang),
                    callback_data=CheckCreateConfirm(owner_id=owner_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_check_create_btn_cancel", lang),
                    callback_data=CheckCreateCancel(owner_id=owner_id).pack(),
                ),
            ]
        ]
    )


async def handle_check_create_start(
    message: Message,
    state: FSMContext,
    lang: str,
) -> None:
    """``/check_create`` — open the interactive create flow.

    Guards against double-entry (a leftover flow) by pointing the user at
    ``/cancel`` rather than stranding a prior card.
    """
    user = require_from_user(message)
    if await state.get_state() is not None:
        await message.reply(t("h_check_create_busy", lang))
        return
    await state.set_state(CheckCreateStates.awaiting_amount)
    await state.set_data({_CD_LANG: lang, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await message.reply(
        t("h_check_create_pick_type", lang),
        reply_markup=_type_keyboard(user.id, lang),
    )
    log.bind(uid=user.id).info("/check_create flow started")


async def handle_check_create_type(
    callback: CallbackQuery,
    callback_data: CheckCreateType,
    state: FSMContext,
) -> None:
    """Type picked — stash it and prompt for the amount(s)."""
    user = callback.from_user
    data = await state.get_data()
    lang = str(data.get(_CD_LANG) or "ru")
    if user.id != callback_data.owner_id:
        await callback.answer(t("h_check_create_foreign", lang), show_alert=True)
        return
    ctype = callback_data.ctype
    if ctype not in _CREATE_TYPES:
        await callback.answer()
        return
    await state.update_data({_CD_TYPE: ctype, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    prompt = "h_check_create_ask_range" if ctype == "random" else "h_check_create_ask_amount"
    target = callback.message
    if isinstance(target, MessageType):
        # The interview state is already stored, so a card that can no
        # longer be redrawn only costs the prompt — but a malformed
        # prompt is our bug and must not hide behind that.
        await edit_card(target, t(prompt, lang))
    await callback.answer()


async def handle_check_create_amount(
    message: Message,
    state: FSMContext,
) -> None:
    """Parse the amount step (range for random, single value otherwise).

    Stays in ``awaiting_amount`` on a bad value with a hint. ``individual``
    is single-claim so it jumps straight to the confirm card; ``random`` /
    ``fixed`` advance to the count step.
    """
    data = await state.get_data()
    lang = str(data.get(_CD_LANG) or "ru")
    ctype = str(data.get(_CD_TYPE) or "")
    tokens = (message.text or "").split()

    if ctype == "random":
        bounds = _pos_ints(tokens[:2]) if len(tokens) >= 2 else None
        if bounds is None:
            await message.reply(t("h_check_create_bad_range", lang))
            return
        lo, hi = bounds
        if lo <= 0 or hi < lo:
            await message.reply(t("h_check_create_bad_range", lang))
            return
        await state.update_data({_CD_MIN: lo, _CD_MAX: hi, STATE_ENTERED_AT_FIELD: utc_now_iso()})
        await state.set_state(CheckCreateStates.awaiting_count)
        await message.reply(t("h_check_create_ask_count", lang))
        return

    # fixed / individual: single amount.
    parsed = _pos_ints(tokens[:1])
    if parsed is None or parsed[0] <= 0:
        await message.reply(t("h_check_create_bad_amount", lang))
        return
    amount = parsed[0]
    await state.update_data({_CD_FIXED: amount, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    if ctype == "individual":
        # Single-claim — no count step; go straight to confirm.
        await state.update_data({_CD_COUNT: 1})
        await _show_create_summary(message, state, lang)
        return
    await state.set_state(CheckCreateStates.awaiting_count)
    await message.reply(t("h_check_create_ask_count", lang))


async def handle_check_create_count(
    message: Message,
    state: FSMContext,
) -> None:
    """Parse the activations count, then render the confirm card."""
    data = await state.get_data()
    lang = str(data.get(_CD_LANG) or "ru")
    tokens = (message.text or "").split()
    parsed = _pos_ints(tokens[:1])
    if parsed is None or parsed[0] <= 0:
        await message.reply(t("h_check_create_bad_count", lang))
        return
    await state.update_data({_CD_COUNT: parsed[0], STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await _show_create_summary(message, state, lang)


def _pos_ints(tokens: list[str]) -> list[int] | None:
    """Every token parsed as a non-negative base-10 integer, else ``None``.

    Returns the VALUES rather than a bool on purpose. The predicate this
    replaced was read by callers that then re-``int()``ed the token, and
    the two disagreed: the gate stripped the leading ``+`` with
    ``lstrip``, which removes EVERY leading ``+``, so ``"++5"`` passed
    and raised ``ValueError`` inside the ``int()`` behind it — an FSM
    step that raises leaves the user parked with no answer. One string,
    one parse, one place to be wrong.
    """
    if not tokens:
        return None
    parsed: list[int] = []
    for token in tokens:
        value = parse_int_token(token, signed=True)
        if value is None or value < 0:
            return None
        parsed.append(value)
    return parsed


def _as_int(value: object) -> int:
    """Coerce an FSM-data value to ``int``; 0 on a missing/odd value.

    FSM data is typed ``dict[str, object]`` by aiogram, so every read
    needs narrowing before arithmetic — this centralises it.
    """
    return value if isinstance(value, int) else 0


def _estimated_total(data: dict[str, object]) -> int:
    """Pre-compute the hold (L-100) for the summary card.

    Mirrors :meth:`CheckService._compute_total` so the user sees the
    exact coins that will be debited BEFORE confirming. The service
    re-derives and re-validates this at create time (the card is
    advisory; the debit is the authority).
    """
    ctype = str(data.get(_CD_TYPE) or "")
    count = _as_int(data.get(_CD_COUNT))
    if ctype == "random":
        lo = _as_int(data.get(_CD_MIN))
        hi = _as_int(data.get(_CD_MAX))
        return ((lo + hi) // 2) * count
    if ctype == "fixed":
        return _as_int(data.get(_CD_FIXED)) * count
    if ctype == "individual":
        return _as_int(data.get(_CD_FIXED))
    return 0


async def _show_create_summary(message: Message, state: FSMContext, lang: str) -> None:
    """Advance to ``awaiting_confirm`` and render the summary card."""
    data = await state.get_data()
    user = require_from_user(message)
    total = _estimated_total(data)
    await state.update_data({STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await state.set_state(CheckCreateStates.awaiting_confirm)
    await message.reply(
        t(
            "h_check_create_summary",
            lang,
            ctype=html.escape(str(data.get(_CD_TYPE) or "")),
            count=_as_int(data.get(_CD_COUNT)),
            total=total,
        ),
        reply_markup=_confirm_keyboard(user.id, lang),
    )


async def handle_check_create_confirm(
    callback: CallbackQuery,
    callback_data: CheckCreateConfirm,
    bot: Bot,
    check_service: CheckService,
    state: FSMContext,
    checkpoint: Checkpoint | None = None,
) -> None:
    """✅ Confirm — build the spec from FSM data and create the check.

    #694: committed before the card edit and the toast, so the funded
    check does not hold ``economy.db``'s writer slot across them. That
    ordering is right and stays; what it costs is that everything after
    the commit is delivery of an outcome the user has already paid for,
    and #2011 found the receipt was not treated that way.

    The receipt is the only place ``result.code`` is ever shown. Nothing
    can look it up afterwards — :class:`ChecksRepo` reaches a check by
    code or by expiry sweep, never by creator — so a receipt that does
    not arrive locks the coins in a check nobody alive can claim. It
    therefore goes out through :func:`_deliver_create_receipt`, which
    falls back to a direct message when the card is gone, matching what
    ``/create_check`` has done through ``reply_or_send`` since #694. The
    interview's other endings (expired, insufficient, invalid) stay on
    plain :func:`_edit_create_card`: they carry no code and no debit, so
    a card that died before them has cost the user nothing.
    """
    user = callback.from_user
    data = await state.get_data()
    lang = str(data.get(_CD_LANG) or "ru")
    if user.id != callback_data.owner_id:
        await callback.answer(t("h_check_create_foreign", lang), show_alert=True)
        return

    async with _confirm_locks.acquire((bot.id, user.id)):
        # Re-read the bag INSIDE the lock. The copy taken above (for the
        # foreign-tap toast's language) predates it, and the tap that
        # lost the race has to see the *consumed* state, not its own
        # stale snapshot.
        data = await state.get_data()
        lang = str(data.get(_CD_LANG) or lang)
        ctype = str(data.get(_CD_TYPE) or "")
        if ctype not in _CREATE_TYPES:
            await state.clear()
            await _edit_create_card(callback, t("h_check_create_expired", lang))
            await callback.answer()
            return

        now = _utcnow()
        spec = CheckSpec(
            type=ctype,
            now=now,
            min_amount=_as_int(data.get(_CD_MIN)) if _CD_MIN in data else None,
            max_amount=_as_int(data.get(_CD_MAX)) if _CD_MAX in data else None,
            fixed_amount=_as_int(data.get(_CD_FIXED)) if _CD_FIXED in data else None,
            max_claims=_as_int(data.get(_CD_COUNT)),
        )
        # Single-shot card: clear regardless of outcome so a stale
        # re-click can't re-create (the FSM data it relies on is
        # consumed here). Inside the lock, so the losing tap's re-read
        # above sees the cleared bag.
        await state.clear()

    result = await check_service.create_check(creator_id=user.id, spec=spec)
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is CreateOutcome.INSUFFICIENT_FUNDS:
        await _edit_create_card(callback, t("h_check_create_insufficient", lang, total="—"))
        await callback.answer()
        return
    if result.outcome is CreateOutcome.INVALID_AMOUNT:
        await _edit_create_card(callback, t("h_check_create_invalid", lang))
        await callback.answer()
        return

    # Same receipt renderer as ``/create_check`` (RR-2 #17) so the two
    # create surfaces can never drift. ``target_display`` is ``None``
    # here: the interview's "individual" button means *single-claim*, not
    # *addressed to someone* — it never collects a handle, so the card
    # must not claim a recipient it doesn't have.
    delivered = await _deliver_create_receipt(
        callback,
        bot,
        _create_receipt(
            spec,
            code=result.code,
            total=result.total_amount,
            bot_username=await _bot_username(bot),
            target_display=None,
            lang=lang,
        ),
        user_id=user.id,
    )
    if not delivered:
        # Same posture as ``/create_check``: the check is committed, so
        # an unreachable creator is logged, not raised. Unlike there,
        # this line is the last trace of the code — the operator reading
        # it is the only remaining route back to the funds.
        log.bind(uid=user.id, code=result.code).warning("check create receipt undeliverable")
    await callback.answer()
    log.bind(uid=user.id, code=result.code, total=result.total_amount).info("/check_create created")


async def handle_check_create_cancel(
    callback: CallbackQuery,
    callback_data: CheckCreateCancel,
    state: FSMContext,
) -> None:
    """❌ Cancel — clear the flow; nothing was debited."""
    user = callback.from_user
    data = await state.get_data()
    lang = str(data.get(_CD_LANG) or "ru")
    if user.id != callback_data.owner_id:
        await callback.answer(t("h_check_create_foreign", lang), show_alert=True)
        return
    await state.clear()
    await _edit_create_card(callback, t("h_check_create_cancelled", lang))
    await callback.answer()
    log.bind(uid=user.id).info("/check_create flow cancelled")


async def _edit_create_card(callback: CallbackQuery, text: str) -> None:
    """Best-effort replace the card text + drop its keyboard.

    Best-effort only for the reasons :func:`edit_card` swallows — an
    expired or already-identical card. A rejected *render* still raises.
    """
    msg = callback.message
    if not isinstance(msg, MessageType):
        return
    await edit_card(msg, text)


async def _deliver_create_receipt(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    *,
    user_id: int,
) -> bool:
    """Get a funded check's receipt to its creator; ``False`` if nothing could.

    #2011. The card is the natural place for it and is tried first, but
    the card is also the one part of this that is allowed to be gone.
    Both ways it dies are ordinary: Telegram answers "message to edit
    not found" for a card old enough to have aged out of the client, and
    a callback from a card the bot may no longer act on arrives carrying
    an :class:`~aiogram.types.InaccessibleMessage` instead of a
    ``Message``. :func:`edit_card` reports the first as ``False`` and
    the isinstance check catches the second, and until this function
    existed both simply ended the handler.

    The direct message is safe to address by ``user_id`` because this
    router is private-only on both sides (``F.chat.type == PRIVATE`` for
    messages, ``F.message.chat.type == PRIVATE`` for callbacks), so the
    chat the card lives in *is* the user. Sending it a second time when
    the edit already worked would be noise, hence the ordering.

    ``False`` means the creator is unreachable outright — blocked the
    bot, deleted the account. That is the one case with no remedy here,
    and the caller logs the code so it is at least recoverable by hand.
    """
    msg = callback.message
    if isinstance(msg, MessageType) and await edit_card(msg, text):
        return True
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        return False
    return True


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` + ``EconomyMiddleware`` per call.

    Mirrors ``handlers/vip.py``: private-chat-only filter, the economy
    middleware on the message side (injects ``check_service``), and the
    command registrations. ``settings`` is captured for the dev-gate on
    ``/create_check``; ``registry`` builds the :class:`EconomyMiddleware`
    that injects ``check_service`` into the handler ``data``.
    """
    router = Router(name="checks")
    # Private-chat-only — a group call gets the #123 refusal twin. The
    # filter applies to messages; the callback side gets its own private
    # filter below so the verify button can't be driven from a group card.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.middleware(EconomyMiddleware(registry))
    # RR-2 #17: ``/create_check individual <amount> @user`` resolves the
    # handle through ``users_repo`` (users.db) and reads the recipient's
    # language override through ``user_settings_repo``. Attached on the
    # router rather than relied upon from the dispatcher-level outer
    # middleware so this router stays self-contained (same pattern as
    # ``handlers/send.py`` and ``handlers/admin/give.py``).
    router.message.middleware(SessionMiddleware(registry))
    # The "✅ Я подписался" verify button is a callback_query — it needs
    # its own EconomyMiddleware instance (separate event type) to inject
    # ``check_service``, mirroring the message side. Private-only.
    router.callback_query.middleware(EconomyMiddleware(registry))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def _handle_check(
        message: Message,
        command: CommandObject,
        bot: Bot,
        check_service: CheckService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_check(message, command, bot, check_service, settings, lang, checkpoint)

    router.message.register(
        _handle_check,
        Command("check", "чек", ignore_case=True),
        F.from_user,
    )

    async def _handle_check_sub_verify(
        callback: CallbackQuery,
        callback_data: CheckSubVerify,
        bot: Bot,
        check_service: CheckService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_check_sub_verify(
            callback, callback_data, bot, check_service, settings, lang, checkpoint
        )

    router.callback_query.register(
        _handle_check_sub_verify,
        CheckSubVerify.filter(),
        F.from_user,
    )

    async def _handle_start_check(
        message: Message,
        command: CommandObject,
        bot: Bot,
        check_service: CheckService,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_start_check(message, command, bot, check_service, settings, lang, checkpoint)

    # ``t.me/<bot>?start=check_<code>`` deep link → claim flow. The
    # bare-/start handler only matches when there are NO args, so this
    # payload variant is unambiguous and lands here. ``deep_link=True``
    # restricts to ``/start <payload>``; the magic filter narrows to our
    # ``check_`` namespace so other start payloads aren't swallowed.
    router.message.register(
        _handle_start_check,
        CommandStart(deep_link=True, magic=F.args.startswith("check_")),
        F.from_user,
    )

    async def _handle_create_check(
        message: Message,
        command: CommandObject,
        bot: Bot,
        check_service: CheckService,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_create_check(
            message,
            command,
            bot,
            check_service,
            users_repo,
            user_settings_repo,
            settings,
            lang,
            checkpoint,
        )

    router.message.register(
        _handle_create_check,
        Command("create_check", "создать_чек", ignore_case=True),
        F.from_user,
    )

    # L-30: interactive ``/check_create`` FSM (user-facing create UI).
    # ``check_service`` is injected by the message-side EconomyMiddleware
    # for the confirm callback; the entry + text steps only touch the FSM.
    router.message.register(
        handle_check_create_start,
        Command("check_create", "newcheck", "чек_создать", ignore_case=True),
        F.from_user,
    )
    router.callback_query.register(
        handle_check_create_type,
        CheckCreateType.filter(),
        StateFilter(CheckCreateStates.awaiting_amount),
        F.from_user,
    )
    router.message.register(
        handle_check_create_amount,
        StateFilter(CheckCreateStates.awaiting_amount),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    router.message.register(
        handle_check_create_count,
        StateFilter(CheckCreateStates.awaiting_count),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    # Both steps above want text; a photo or a voice message sent instead
    # matched nothing at all and left the interview looking dead.
    register_text_expected(
        router,
        CheckCreateStates.awaiting_amount,
        CheckCreateStates.awaiting_count,
    )
    router.callback_query.register(
        handle_check_create_confirm,
        CheckCreateConfirm.filter(),
        StateFilter(CheckCreateStates.awaiting_confirm),
        F.from_user,
    )
    router.callback_query.register(
        handle_check_create_cancel,
        CheckCreateCancel.filter(),
        StateFilter(CheckCreateStates.awaiting_confirm),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
