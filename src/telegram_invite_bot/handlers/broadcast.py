"""Developer mass-broadcast — Cluster A2, L-94 (``/broadcast``).

Legacy contract (``bot.py:25925-26015``):

* ``/broadcast`` is developer-only and private-only (``cmd_broadcast``,
  bot.py:25928-25937 — ``@admin_only`` + ``chat.type != "private"``
  early-return). It enters a "waiting for text" state and prompts the
  developer to type the message body.
* The next message becomes the draft; legacy capped it at 3500 chars
  (``state = f"confirm:{text[:3500]}"``, bot.py:25955) and echoed a
  preview with ✅/❌ inline buttons (bot.py:25956-25966).
* ``broadcast_cancel`` clears state and edits the prompt to a cancelled
  notice (bot.py:25979-25986). ``broadcast_confirm`` reads the draft,
  answers the callback, edits the prompt to "started…", then iterates
  ``SELECT user_id FROM users`` sending with ``time.sleep(0.05)`` between
  sends and swallowing per-user exceptions into a ``failed`` counter
  (bot.py:26000-26012), finally reporting sent/failed counts.

Strangler deltas (deliberate):

* **Photo support** — the draft may be a photo+caption, not just text
  (the legacy admin-panel broadcast editor at bot.py:34617-34619 already
  offered text/photo; the slash-command flow gains the same minimum).
* **Non-blocking send loop** — legacy's synchronous loop froze the
  whole bot for minutes on large audiences. Here the loop runs as an
  ``asyncio`` background task and edits a progress message every
  ``_PROGRESS_EVERY`` sends, so the dispatcher keeps serving updates.
* **FSM sweeper** — ``awaiting_content`` carries the standard
  ``state_entered_at`` stamp and a 600 s :class:`TimeoutRule` (same
  10-minute budget as /support and /ad), registered in ``app.py`` via
  :func:`on_expire_broadcast`. Legacy's module-global ``_broadcast_state``
  dict leaked forever on abandonment.

The audience snapshot (``UsersRepo.all_user_ids``) is taken INSIDE the
confirm callback while its request-scoped session is still open; the
background task receives a plain ``list[int]`` and never touches the DB —
that is what makes detaching from the session lifecycle safe.

Parse mode: HTML throughout (bot default), matching legacy's explicit
``parse_mode="HTML"`` on the fan-out send (bot.py:26005). The developer
authors the body; invalid HTML simply fails per-recipient into the
``failed`` counter, same observable behaviour as legacy.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType  # runtime — isinstance guard
from loguru import logger

from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND
from telegram_invite_bot.i18n import t
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.utils.aiogram import require_from_user

log = logger.bind(component="handlers.broadcast")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.repositories.users_repo import UsersRepo


class BroadcastStates(StatesGroup):
    """FSM for the /broadcast draft flow (kept in-module per cluster spec).

    ``awaiting_content``
        Set by ``/broadcast`` (private, developer-only). The developer's
        next text or photo message becomes the draft and triggers the
        preview; the state survives until Confirm/Cancel, the global
        ``/cancel`` handler, or the 600 s FSM sweeper rule clears it.
        A second content message while waiting simply replaces the draft
        and re-renders the preview (newest-draft-wins, the same shape as
        /ad re-rendering its form).
    """

    awaiting_content = State()


# ── Constants ────────────────────────────────────────────────────────────────

# Legacy draft cap: ``text[:3500]`` (bot.py:25955). Telegram allows 4096,
# legacy reserved headroom for the confirm-prompt framing; we keep the cap
# on the BODY itself so the fan-out send can never trip the length limit.
_TEXT_MAX_LEN = 3500
# Telegram's hard cap for photo captions.
_CAPTION_MAX_LEN = 1024
# Preview slice in the confirm prompt — legacy showed ``text[:500] + "…"``
# (bot.py:25958).
_PREVIEW_LEN = 500
# Legacy inter-send pause: ``time.sleep(0.05)`` (bot.py:26007) ≈ 20 msg/s,
# inside Telegram's ~30 msg/s global bot limit.
_SEND_PAUSE_SECONDS = 0.05
# Progress-message edit cadence (sends between edits).
_PROGRESS_EVERY = 100
# Upper bound on a honoured flood wait. Telegram's ``retry_after`` is
# normally seconds; anything past this is a penalty we should not sit
# through inside one broadcast — we cap the sleep, log it, and let the
# recipient fall into ``failed`` if the retry still bounces.
_MAX_RETRY_AFTER_SECONDS = 60

_CB_CONFIRM = "broadcast:confirm"
_CB_CANCEL = "broadcast:cancel"

# FSM-data field names for the draft.
_F_KIND = "bcast_kind"  # "text" | "photo"
_F_TEXT = "bcast_text"
_F_FILE_ID = "bcast_file_id"
_F_CAPTION = "bcast_caption"

# Strong references to in-flight send loops. ``asyncio.create_task``
# results are weakly held by the loop; without this set a long broadcast
# could be garbage-collected mid-run (asyncio docs' canonical pattern).
#
# The set doubles as the single-flight marker (#1496): non-empty means a
# fan-out is still running, because the done-callback discards each task
# the moment it finishes. A second fan-out is not a cosmetic problem —
# both loops send to the same audience, so every user gets the message
# twice, and the two loops share one outbound rate budget, so the
# per-send pause that keeps the bot under Telegram's ceiling stops
# meaning what it says.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Serialises the confirm handler end to end (#1496). The single-flight
# check above is a read, and the reservation that makes it true — the
# ``create_task`` at the bottom — is a dozen awaits later: the audience
# snapshot alone is a database round trip. Without this lock two taps
# both find the set empty, both snapshot, and both spawn. There is only
# ever one developer, so the lock is uncontended in normal use; it is
# here for the double tap and for the /broadcast started while the last
# one is still going.
_confirm_lock = asyncio.Lock()

# How long shutdown waits for a cancelled fan-out to send its abort
# report (#1815). The report is one Telegram round trip, so this is
# generous; it is a ceiling, not a budget. It has to stay well under
# systemd's ``TimeoutStopSec=90``, because everything ``close()`` does
# after the drain — FSM storage, bot session, five aiosqlite pools, the
# container — is skipped entirely if the unit is SIGKILLed first.
_SHUTDOWN_DRAIN_SECONDS = 10.0


# ── FSM sweeper callback ─────────────────────────────────────────────────────


async def on_expire_broadcast(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """Sweeper timeout for ``BroadcastStates.awaiting_content`` (10 min).

    An abandoned draft has no money side-effect — expiry just clears the
    state (the sweeper does that after this returns) and DMs the
    developer so the silently-dropped draft isn't a surprise. Same GAP-1
    shape as ``support.on_expire_support``.
    """
    lang_raw = data.get("lang")
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.user_id, t("h_broadcast_timeout", lang))
    log.bind(uid=key.user_id).info("/broadcast draft expired by sweeper")


# ── Keyboard ────────────────────────────────────────────────────────────────


def _confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    """✅/❌ row — legacy ``broadcast_confirm`` / ``broadcast_cancel``
    buttons (bot.py:25961-25962), labels localised.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_broadcast_confirm_btn", lang), callback_data=_CB_CONFIRM
                ),
                InlineKeyboardButton(
                    text=t("h_broadcast_cancel_btn", lang), callback_data=_CB_CANCEL
                ),
            ]
        ]
    )


def _preview_slice(body: str) -> str:
    """First ``_PREVIEW_LEN`` chars, ellipsised — legacy bot.py:25958."""
    preview = body[:_PREVIEW_LEN]
    if len(body) > _PREVIEW_LEN:
        preview += "…"
    return preview


# ── /broadcast entry ─────────────────────────────────────────────────────────


async def handle_broadcast_command(
    message: Message,
    state: FSMContext,
    settings: Settings,
    lang: str,
) -> None:
    """``/broadcast`` — developer-only, private-only (bot.py:25928-25937).

    The private-only gate is enforced by the router registration filter;
    the developer gate is re-checked here and answers with a localized
    refusal — which is what legacy did too (#1593). ``@admin_only``
    (bot.py:1331-1356) checks ``DEVELOPER_IDS`` and replies with
    ``t(lang, "admin_only_denied")`` at bot.py:1353; it never swallowed
    the message. This docstring used to claim the opposite and present
    the reply as a deliberate divergence — there is no divergence, and
    the same shape is the established pattern for dev commands here
    (e.g. /admin_tickets).
    """
    user = require_from_user(message)
    if not settings.bot.is_developer(user.id):
        await message.reply(t("h_broadcast_dev_only", lang))
        return

    prior = await state.get_state()
    if prior is not None and prior != BroadcastStates.awaiting_content.state:
        # Some OTHER flow is mid-flight (withdraw confirm, support text…)
        # — don't stomp it; the developer can /cancel first.
        await message.reply(t("h_broadcast_busy", lang))
        return

    await state.set_state(BroadcastStates.awaiting_content)
    # Sweeper contract: stamp entry time + lang for the on_expire DM.
    await state.set_data({STATE_ENTERED_AT_FIELD: utc_now_iso(), "lang": lang})
    await message.reply(t("h_broadcast_prompt", lang))
    log.bind(uid=user.id).info("/broadcast FSM started")


# ── Draft intake (text or photo+caption) ────────────────────────────────────


async def handle_broadcast_content(
    message: Message,
    state: FSMContext,
    settings: Settings,
    lang: str,
) -> None:
    """Receive the draft while in ``awaiting_content``.

    Accepts a plain text message (legacy path, bot.py:25950-25966) or a
    photo with optional caption (admin-panel parity). Anything else gets
    a "need text or photo" nudge and the state stays put.

    Replies with the preview + Confirm/Cancel keyboard. The preview is
    always TWO messages — the draft echoed as recipients will receive
    it, then the confirm prompt — so the confirm prompt is always a
    plain text message and ``edit_text`` on confirm/cancel works
    uniformly.
    """
    user = require_from_user(message)
    if not settings.bot.is_developer(user.id):
        # Defence in depth — legacy popped state for non-devs
        # (bot.py:25944-25945). Should be unreachable: only a developer
        # can have entered the state.
        await state.clear()
        return

    data = await state.get_data()

    if message.photo:
        file_id = message.photo[-1].file_id
        caption = (message.caption or "")[:_CAPTION_MAX_LEN]
        data.update({_F_KIND: "photo", _F_FILE_ID: file_id, _F_CAPTION: caption})
        data.pop(_F_TEXT, None)
        await state.set_data(data)
        await message.answer_photo(file_id, caption=caption or None)
        await message.reply(
            t("h_broadcast_preview", lang, preview=html.escape(_preview_slice(caption))),
            reply_markup=_confirm_keyboard(lang),
        )
        log.bind(uid=user.id, kind="photo").info("/broadcast draft captured")
        return

    text = (message.text or "").strip()
    if not text:
        # Legacy: "Введите непустой текст." (bot.py:25953). Extended to
        # cover stickers/voice/etc. landing in this state.
        await message.reply(t("h_broadcast_need_content", lang))
        return

    truncated = text[:_TEXT_MAX_LEN]
    data.update({_F_KIND: "text", _F_TEXT: truncated})
    data.pop(_F_FILE_ID, None)
    data.pop(_F_CAPTION, None)
    await state.set_data(data)
    # Echo the draft the way recipients will receive it — rendered, under
    # the bot-wide HTML parse mode — exactly as the photo branch above
    # echoes the photo. The confirm card below shows the *source*, and
    # source alone cannot answer the one question a broadcast operator
    # has: does the markup come out right. A body Telegram refuses to
    # parse used to be discovered by the fan-out, where it costs two API
    # calls and an ERROR line per recipient (``ParseModeFallbackMiddleware``
    # resends each refusal with parse mode off); here it costs one, and
    # the operator sees the literal tags before tapping Send.
    await message.answer(truncated)
    await message.reply(
        t("h_broadcast_preview", lang, preview=html.escape(_preview_slice(truncated))),
        reply_markup=_confirm_keyboard(lang),
    )
    log.bind(uid=user.id, kind="text", text_len=len(truncated)).info("/broadcast draft captured")


# ── Confirm / cancel callbacks ───────────────────────────────────────────────


async def _edit_or_answer(callback: CallbackQuery, body: str) -> None:
    """Edit the prompt under the tapped button; fall back to a fresh send.

    Two different shapes hide behind ``callback.message``, and only one
    of them degrades to ``answer``. A deleted or otherwise un-editable
    message raises ``TelegramBadRequest`` and is answered with a fresh
    send. An ``InaccessibleMessage`` (48h+ old) fails the ``isinstance``
    guard and this helper does nothing at all — there is no chat-bound
    object left to answer with. Callers that must reach the developer in
    that case have to send to a chat id they already hold; the confirm
    handler does exactly that for its progress reports.
    """
    if isinstance(callback.message, MessageType):
        try:
            await callback.message.edit_text(body)
        except TelegramBadRequest:
            await callback.message.answer(body)


async def handle_broadcast_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    lang: str,
) -> None:
    """❌ — clear state, edit prompt to "cancelled" (bot.py:25979-25986)."""
    user = callback.from_user
    if user is None or not settings.bot.is_developer(user.id):
        await callback.answer()
        return
    if await state.get_state() == BroadcastStates.awaiting_content.state:
        await state.clear()
    await _edit_or_answer(callback, t("h_broadcast_cancelled", lang))
    await callback.answer()
    log.bind(uid=user.id).info("/broadcast cancelled")


async def handle_broadcast_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    users_repo: UsersRepo,
    settings: Settings,
    lang: str,
) -> None:
    """✅ — snapshot the audience, spawn the send loop, return immediately.

    Legacy ran the fan-out inline and blocked the (single-threaded)
    polling loop for ``0.05 s × N`` (bot.py:26000-26012). Here the
    audience is snapshotted while the request-scoped session is open,
    then a detached ``asyncio`` task does the sending and edits the
    progress message — the dispatcher stays responsive.

    Detaching the loop is also what made a second one possible, which
    legacy's blocking version could not have: the whole body therefore
    runs under ``_confirm_lock`` and refuses while ``_BACKGROUND_TASKS``
    is non-empty (#1496).
    """
    user = callback.from_user
    if user is None or not settings.bot.is_developer(user.id):
        await callback.answer()
        return

    async with _confirm_lock:
        if _BACKGROUND_TASKS:
            # A fan-out is still running. Refusing is the whole point;
            # see ``_BACKGROUND_TASKS`` for what a second one costs.
            await callback.answer(t("h_broadcast_already_running", lang), show_alert=True)
            return

        if await state.get_state() != BroadcastStates.awaiting_content.state:
            # Legacy: "Сессия истекла" toast (bot.py:25991-25993).
            await callback.answer(t("h_broadcast_session_expired", lang))
            return

        data = await state.get_data()
        kind = data.get(_F_KIND)
        if kind not in ("text", "photo"):
            # Confirm tapped before any draft was typed — treat as expired.
            await callback.answer(t("h_broadcast_session_expired", lang))
            return

        # Snapshot the audience NOW: the session dies with this handler.
        user_ids = await users_repo.all_user_ids()
        await state.clear()

        if not user_ids:
            await _edit_or_answer(callback, t("h_broadcast_empty", lang))
            await callback.answer()
            return

        await callback.answer()
        await _edit_or_answer(callback, t("h_broadcast_started", lang, total=len(user_ids)))

        # Progress edits target the prompt message when reachable, else
        # the developer's DM chat (callback.from_user is the dev).
        if isinstance(callback.message, MessageType):
            progress_chat_id = callback.message.chat.id
            progress_message_id: int | None = callback.message.message_id
        else:
            progress_chat_id = user.id
            progress_message_id = None

        task = asyncio.create_task(
            _run_broadcast(
                bot,
                user_ids=user_ids,
                kind=str(kind),
                text=str(data.get(_F_TEXT) or ""),
                file_id=str(data.get(_F_FILE_ID) or ""),
                caption=str(data.get(_F_CAPTION) or ""),
                progress_chat_id=progress_chat_id,
                progress_message_id=progress_message_id,
                lang=lang,
            )
        )
        # Registered before the lock is released, so the next tap's
        # single-flight check sees it.
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    log.bind(uid=user.id, total=len(user_ids), kind=kind).info("/broadcast send loop spawned")


# ── Background send loop ─────────────────────────────────────────────────────


async def _report_progress(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int | None,
    body: str,
) -> None:
    """Best-effort progress render: edit when we have a message id, else
    send. Progress must never crash the loop — all API errors swallowed.
    """
    with contextlib.suppress(TelegramAPIError):
        if message_id is not None:
            await bot.edit_message_text(body, chat_id=chat_id, message_id=message_id)
        else:
            await bot.send_message(chat_id, body)


def _aborted_report(lang: str, sent: int, failed: int, total: int) -> str:
    """Render the "did not reach everyone" report.

    ``remaining`` is the honest number: every recipient the loop never
    reached, including the one it was mid-send on when it died. It is the
    figure ``h_broadcast_done`` cannot express — that key carries only
    ``sent`` and ``failed``, which sum to the attempted audience and say
    nothing about the tail (#888).
    """
    return t(
        "h_broadcast_aborted",
        lang,
        sent=sent,
        failed=failed,
        remaining=max(total - sent - failed, 0),
        total=total,
    )


async def _run_broadcast(
    bot: Bot,
    *,
    user_ids: list[int],
    kind: str,
    text: str,
    file_id: str,
    caption: str,
    progress_chat_id: int,
    progress_message_id: int | None,
    lang: str,
) -> None:
    """The fan-out loop (legacy bot.py:26003-26009, made async).

    Per-recipient failures (blocked bot → ``TelegramForbiddenError``,
    deleted account → ``TelegramBadRequest``, transient network) are
    swallowed into ``failed`` — exactly legacy's bare ``except`` counter.
    ``asyncio.sleep(0.05)`` between sends keeps under the global bot
    rate limit. One deliberate divergence: legacy slept only after a
    SUCCESSFUL send (bot.py:26007, inside the try), so a run against a
    mostly-blocked audience raced ahead at full speed; this port sleeps
    after every attempt, which is the safer reading of the same limit.
    Every ``_PROGRESS_EVERY`` sends the progress message is edited; the
    final report replaces it (legacy sent the final counts as a fresh
    message, bot.py:26010 — edit-in-place is strictly tidier and falls
    back to a fresh send on edit failure).

    The report is never unconditionally the success line. Legacy answered
    an outer crash with its own message (bot.py:26011-26012, "❌ Ошибка
    рассылки"), and dropping that in the port meant a run that died at
    recipient 3 of 5000 still told the operator "finished" (#888).
    """
    sent = 0
    failed = 0
    total = len(user_ids)
    aborted = False

    async def _send_one(uid: int) -> None:
        if kind == "photo":
            await bot.send_photo(uid, file_id, caption=caption or None)
        else:
            await bot.send_message(uid, text)

    try:
        for done, uid in enumerate(user_ids, start=1):
            try:
                await _send_one(uid)
                sent += 1
            except TelegramRetryAfter as exc:
                # Telegram's own back-pressure. Counting this as a plain
                # failure and carrying on at 20 msg/s means every send
                # inside the wait window is refused too — the rest of the
                # audience silently goes unreached and the penalty grows.
                # Sleep the window out, then retry this one recipient; a
                # second refusal is an ordinary failure and the run moves on.
                wait = min(exc.retry_after, _MAX_RETRY_AFTER_SECONDS)
                log.bind(uid=uid, retry_after=exc.retry_after, wait=wait).warning(
                    "/broadcast flood wait — pausing the fan-out"
                )
                await asyncio.sleep(wait)
                try:
                    await _send_one(uid)
                    sent += 1
                except TelegramAPIError:
                    failed += 1
            except TelegramAPIError:
                # Forbidden (blocked), bad chat, flood-wait, network —
                # count and continue; one bad recipient must not stop
                # the run (legacy parity, bot.py:26008-26009).
                failed += 1
            await asyncio.sleep(_SEND_PAUSE_SECONDS)
            if done % _PROGRESS_EVERY == 0:
                await _report_progress(
                    bot,
                    chat_id=progress_chat_id,
                    message_id=progress_message_id,
                    body=t("h_broadcast_progress", lang, done=done, total=total, failed=failed),
                )
    except asyncio.CancelledError:
        # Process shutdown mid-broadcast: report what we managed, then
        # re-raise so cancellation semantics stay intact. The tail of the
        # audience was never attempted, so this must NOT be the success
        # key — the operator has to know a re-run is owed (#888).
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(progress_chat_id, _aborted_report(lang, sent, failed, total))
        raise
    except Exception:  # noqa: BLE001 — detached task: an escape would be silently dropped
        # No ``return`` here on purpose: the operator still gets a report.
        # But the flag is what keeps it honest — before #888 control fell
        # through to the success line and a crashed run read as a clean one.
        aborted = True
        log.exception("broadcast loop crashed after {n} sends", n=sent)

    final = (
        _aborted_report(lang, sent, failed, total)
        if aborted
        else t("h_broadcast_done", lang, sent=sent, failed=failed)
    )
    await _report_progress(
        bot, chat_id=progress_chat_id, message_id=progress_message_id, body=final
    )
    log.bind(sent=sent, failed=failed, total=total).info(
        "/broadcast aborted" if aborted else "/broadcast finished"
    )


# ── Shutdown drain ───────────────────────────────────────────────────────────


async def cancel_inflight() -> None:
    """Cancel every in-flight fan-out and let it report (#1815).

    ``Application.close`` drains ``_background_tasks`` — the sweepers it
    spawned itself. The fan-out is not in that list: it lives in the
    module-global set above, which doubles as the single-flight marker
    (#1496) and therefore cannot be handed to the application object.
    So nothing cancelled it, and ``_run_broadcast``'s
    ``except asyncio.CancelledError`` branch — written and tested for
    exactly this moment (#888) — never fired. What happened instead was
    worse than no handler: the loop kept sending until
    ``bot.session.close()`` pulled the aiohttp session out from under
    it, and the resulting ``RuntimeError`` is not a ``TelegramAPIError``
    — so it escaped the per-recipient handler AND the report send's own
    ``suppress``. The operator's last signal was a progress edit at some
    multiple of 100.

    Which is what this is for. Cancellation cannot save the broadcast:
    the set is process state, so the restart forgets a run ever
    happened, and a re-run sends twice to everyone the first pass
    reached. A resume cursor would be the real fix and is a product
    decision, not one to invent here. Telling the operator where it
    stopped is what lets them make that call.

    Bounded, and deliberately not shielded: on timeout we WANT the
    report send interrupted, because the steps ``close()`` still has to
    run — FSM storage, bot session, the aiosqlite pools — are lost
    outright if systemd's ``TimeoutStopSec`` arrives first.

    Scoped to broadcast on purpose. The other two detached-task sets in
    ``handlers/`` are not worth the same treatment: ``game_cards``
    sweeps a cosmetic card deletion that is indistinguishable from never
    having been scheduled, and ``group_events``' captcha timers need
    durable state rather than a tidier shutdown (#1823).
    """
    tasks = list(_BACKGROUND_TASKS)
    if not tasks:
        return
    log.bind(n=len(tasks)).info("shutdown: cancelling in-flight /broadcast fan-out")
    for task in tasks:
        task.cancel()
    for task in tasks:
        # ``TimeoutError`` is an ``Exception``, so the bound is covered
        # by the same suppression as the cancellation itself. Nothing a
        # dying fan-out can raise gets to abort the rest of teardown —
        # the lesson of #1445, one layer up.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, _SHUTDOWN_DRAIN_SECONDS)
    # Belt and braces. The done-callback discards each task as well, and
    # in practice it has already run by the time ``wait_for`` returns —
    # it was registered before the await's own callback, and ``call_soon``
    # is FIFO. This line does not depend on that ordering holding.
    _BACKGROUND_TASKS.difference_update(tasks)


# ── Router factory ───────────────────────────────────────────────────────────


def build_router(settings: Settings) -> Router:
    """Build the /broadcast router (private-chat, developer-gated inside)."""
    router = Router(name="broadcast")
    _private = F.chat.type == ChatType.PRIVATE

    # Callbacks need their OWN scope filter, and it cannot be
    # ``_private``: a CallbackQuery has no ``chat`` field at all
    # (only chat_instance / data / from_user / game_short_name /
    # id / inline_message_id / message), so ``F.chat.type`` would
    # never match and both buttons would go dead. The chat lives
    # one level down, on the message the button is attached to —
    # the same idiom eight other routers already use, each in its
    # own ``build_router``: p2p, main_menu, checks, ai,
    # transfer_rights, currency, admin/withdrawals and
    # admin/panel.
    #
    # Defence in depth, not a hole being closed: both bodies gate
    # on ``settings.bot.is_developer`` as their first statement,
    # and only the private, developer-gated /broadcast flow ever
    # produces this keyboard. The filter keeps a group-forwarded
    # copy of that keyboard from reaching the handler at all
    # (#1588).
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def _cmd(
        message: Message,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_broadcast_command(message, state, settings, lang)

    router.message.register(
        _cmd,
        Command("broadcast", ignore_case=True),
        _private,
        F.from_user,
    )

    async def _content(
        message: Message,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_broadcast_content(message, state, settings, lang)

    router.message.register(
        _content,
        StateFilter(BroadcastStates.awaiting_content),
        _private,
        F.from_user,
        # No ``F.text`` here on purpose: a broadcast draft may be a photo
        # (#1592: it may NOT be a video — ``handle_broadcast_content``
        # branches on ``message.photo`` and otherwise falls through to
        # ``message.text``, so a video reaches the empty-content refusal.
        # ``_F_KIND`` is typed "text" | "photo" and ``_send_one`` calls
        # only ``send_photo`` / ``send_message``.) The command carve-out
        # still applies to the text case.
        NOT_A_COMMAND,
    )

    async def _confirm(
        callback: CallbackQuery,
        state: FSMContext,
        bot: Bot,
        users_repo: UsersRepo,
        lang: str,
    ) -> None:
        await handle_broadcast_confirm(callback, state, bot, users_repo, settings, lang)

    router.callback_query.register(
        _confirm,
        F.data == _CB_CONFIRM,
        F.from_user,
    )

    async def _cancel(
        callback: CallbackQuery,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_broadcast_cancel(callback, state, settings, lang)

    router.callback_query.register(
        _cancel,
        F.data == _CB_CANCEL,
        F.from_user,
    )

    return router
