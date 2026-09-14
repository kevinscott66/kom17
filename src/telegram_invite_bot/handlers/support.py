"""Support commands — Stage 14 (feedback/faq/check) + T-022 full lifecycle.

T-022 extends the Stage-14 partial port into the full ticket lifecycle:

  /support  (alias /ticket, no args)   — FSM flow: prompt → user types → save
  /support  <text>                     — one-shot: save immediately (legacy parity)
  /my_tickets                          — user sees own last 10 tickets
  /admin_tickets                       — admin: list 20 open tickets
  /ticket_reply <id> <text>            — admin: reply + DM the user
  /ticket_close <id>                   — admin: mark closed

The Stage-14 handlers are preserved verbatim:

  /feedback (aliases /отзыв)           — one-shot feedback (kept for legacy parity)
  /faq                                 — FAQ part 1 + inline "continue" button
  /check                               — static check blurb

Parse mode: HTML throughout. Every user-controlled field (username,
first_name, ticket text, answer) is passed through ``html.escape``
before interpolation. The i18n values that contain entity references
(``&lt;``, ``&gt;``) are rendered as-is by Telegram HTML.

Admin gate: ``settings.bot.is_developer(user_id)``. Uses the same
helper as ``handlers/moderation.py`` — not group-admin status, because
ticket management is a bot-operator concern, not a chat-admin concern.

FSM note: the global ``/cancel`` handler (``handlers/cancel.py``) calls
``state.clear()`` so we do NOT need our own /cancel branch here. The
user running /cancel while in ``SupportStates.awaiting_text`` is handled
transparently; we only need to register the awaiting-text message handler
with the correct ``StateFilter``.
"""

from __future__ import annotations

import contextlib
import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message as MessageType  # runtime — isinstance guard
from loguru import logger

from telegram_invite_bot.fsm.support import SupportStates
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.faq import build_continue_markup as build_faq_continue_markup
from telegram_invite_bot.handlers.faq import build_part2_markup as build_faq_part2_markup
from telegram_invite_bot.handlers.faq import render_part1 as render_faq_part1
from telegram_invite_bot.handlers.faq import render_part2 as render_faq_part2
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import FaqContinue
from telegram_invite_bot.middlewares.language import best_effort_language_for_user
from telegram_invite_bot.repositories.support_tickets_repo import ReplyOutcome
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.render import paginate_lines

log = logger.bind(component="handlers.support")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.filters import CommandObject
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import HelpConfig, Settings
    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.repositories.support_tickets_repo import (
        SupportTicketsRepo,
    )
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo


async def on_expire_support(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """FSM-sweeper timeout callback for ``SupportStates.awaiting_text``
    (GAP-1).

    A lingering support session has no money/lockout side-effect (it is
    self-recovering — the next message the user sends is saved as the
    ticket), but registering a sweeper rule honours the "every stateful
    flow has a timeout" contract and keeps the FSM tidy. The sweeper
    clears the state after this returns; we DM the user so a forgotten
    session doesn't sit open indefinitely.
    """
    lang_raw = data.get("lang")
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.user_id, t("h_support_timeout", lang))
    log.bind(uid=key.user_id).info("/support session expired by sweeper")


# ── Constants ────────────────────────────────────────────────────────────────

# NOT a legacy cap, whatever the comment here used to say (#1500). It
# cited ``bot/handlers/support.py:14`` — a path that has never existed in
# this repository, since legacy is one file at the root — and legacy
# caps the text nowhere: ``_start_support_flow``'s text step stores
# ``message.text`` unchecked and forwards it into the admin card.
#
# So this number is this port's own, and the reason is forward-
# looking rather than parity: Telegram itself accepts longer, but a
# multi-page rant in /feedback is a worse read for the operator than
# a truncated one, and the admin preview is clipped again at
# :data:`_ADMIN_PREVIEW_MAX` regardless.
_FEEDBACK_MAX_LEN = 2000
# T-022: /support allows slightly longer free-form messages than /feedback.
# Truncate (not reject) to keep the "describe in one message" promise.
_SUPPORT_MAX_LEN = 4000
# Trim the admin preview at 1500 to keep one message under the 4096 cap
# even with framing + HTML escaping overhead. Legacy used 1500 too.
_ADMIN_PREVIEW_MAX = 1500
# User-facing ticket listing shows first 80 chars per ticket.
_TICKET_PREVIEW_LEN = 80


# NOTE: the six-bullet ``_FAQ`` blurb that used to live here was
# replaced by the real paged card in RR-6 #70 — see ``render_faq_part1``
# and the ``h_faq_*`` keys. The stub was RU-only and named commands the
# new pipeline no longer has (``/referral``), so an English user asking
# /faq got Russian text pointing at a 404.
#
# NOTE: the ``/feedback`` literals that used to live here are now the
# ``h_feedback_*`` keys, so they answer in the caller's language like the
# rest of the T-022 flow.
# NOTE: the static ``/donate`` blurb (``handle_donate`` +
# ``h_donate_blurb``) was removed when #2007 restored the real command in
# ``handlers/donate.py``. It was registered PRIVATE-only and pointed at a
# donation link no setting in this package holds, while the catalog
# advertised ``/donate`` as «поддержать группу или автора монетами» —
# so the blurb was the dead end, not the fallback. ``/donate`` is now
# claimed by that router, group-only.
# NOTE: the static ``/check`` stub (``_CHECK`` + ``handle_check``) was
# removed when #26 landed the real coin-code voucher flow in
# ``handlers/checks.py``. ``/check`` is now claimed by that router.


# ── Helper: admin notification body ─────────────────────────────────────────


def _format_admin_notification(*, uid: int, first_name: str, username: str, text: str) -> str:
    """Build the admin DM. Every interpolated string is HTML-escaped —
    a user-supplied first_name like ``<script>`` must not break HTML
    parse_mode.

    Deliberately NOT routed through ``t()``: this card goes to
    ``admin_chat_id``, not to the reporting user, and the only locale
    we have on this code path is the *reporter's*. Translating it would
    mean an English-speaking user's ticket arrives at a Russian-speaking
    operator in English — the wrong direction. The operator's own
    locale isn't modelled anywhere yet; when it is, this becomes a
    ``t(..., operator_lang)`` call.
    """
    preview = text[:_ADMIN_PREVIEW_MAX]
    if len(text) > _ADMIN_PREVIEW_MAX:
        preview += "…"
    handle = f"@{username}" if username else "—"
    return (
        "📩 <b>Новый отзыв/обращение</b>\n\n"
        f"От: {html.escape(first_name or '—')} (ID: <code>{uid}</code>)\n"
        f"{html.escape(handle)}\n\n"
        f"Текст:\n{html.escape(preview)}"
    )


# ── Stage-14: /feedback ──────────────────────────────────────────────────────


async def handle_feedback(
    message: Message,
    command: CommandObject,
    bot: Bot,
    support_tickets_repo: SupportTicketsRepo,
    admin_chat_id: int,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """One-shot ``/feedback <text>`` — kept for Stage-14 parity.

    Rejects empty or overlong bodies so the row is always meaningful.
    Admin notification is best-effort: failure to DM the admin must NOT
    roll back the ticket (same contract as legacy).
    """
    text = command_args(command)
    if not text or len(text) > _FEEDBACK_MAX_LEN:
        await message.reply(t("h_feedback_usage", lang, max_len=_FEEDBACK_MAX_LEN))
        return

    user = require_from_user(message)
    bound = log.bind(uid=user.id, text_len=len(text))

    try:
        ticket_id = await support_tickets_repo.create_open_ticket(
            user_id=user.id,
            username=(user.username or "")[:128],
            first_name=(user.first_name or "")[:256],
            text=text[:_FEEDBACK_MAX_LEN],
        )
    except Exception:  # noqa: BLE001
        # We log here, then swallow so the user sees the friendly
        # message rather than a 500 — same UX contract as legacy. Safe
        # to keep using the session afterwards because the repo takes
        # a SAVEPOINT around the insert (R15); without it the failed
        # flush would leave the per-update transaction unusable and the
        # middleware's commit would raise on the way out.
        bound.exception("failed to save support ticket")
        await message.reply(t("h_feedback_save_failed", lang))
        return

    # #1877: the INSERT holds ``BEGIN IMMEDIATE`` on the tickets DB,
    # and two Telegram round-trips follow — the acknowledgement and the
    # admin DM. The admin DM is already documented as best-effort, but
    # "best-effort" only holds if the row is durable by then: without
    # this commit the session middleware would still be free to roll the
    # ticket back, so a user told their report was saved could have had
    # it discarded by a failure in the notification.
    if checkpoint is not None:
        await checkpoint()

    await message.reply(t("h_feedback_saved", lang))
    bound.info("support ticket saved: id={tid}", tid=ticket_id)

    if not admin_chat_id:
        return
    try:
        await bot.send_message(
            admin_chat_id,
            _format_admin_notification(
                uid=user.id,
                first_name=user.first_name or "",
                username=user.username or "",
                text=text,
            ),
        )
    except TelegramAPIError as exc:
        # Best-effort: failure to notify the admin must NOT roll back
        # the ticket the user already saw saved. Log and move on.
        bound.warning("admin notify failed for ticket {tid}: {e!r}", tid=ticket_id, e=exc)


# ── T-022: /support + /ticket — FSM flow ────────────────────────────────────


async def handle_support_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    state: FSMContext,
    support_tickets_repo: SupportTicketsRepo,
    admin_chat_id: int,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/support`` or ``/ticket`` entry point.

    Two branches depending on whether the user supplied inline text:

    * **With args**: behave like ``/feedback`` — save immediately, no
      FSM. Preserves legacy parity (the monolith's ``cmd_support`` also
      accepts ``/support <text>``).

    * **No args**: enter FSM ``awaiting_text``, prompt the user to type
      their message. The next free-text message from that user (matched
      by :func:`handle_support_text`) saves the ticket and clears state.

    Text truncated at ``_SUPPORT_MAX_LEN`` (4000 chars); not rejected.
    The limit keeps rows readable and prevents Telegram-length errors in
    the admin notification.
    """
    user = require_from_user(message)
    text = command_args(command)

    if text:
        # Inline-arg form — save immediately, identical flow to /feedback
        # but with the larger 4000-char cap and a different ack message.
        truncated = text[:_SUPPORT_MAX_LEN]
        bound = log.bind(uid=user.id, text_len=len(truncated))
        try:
            ticket_id = await support_tickets_repo.create_open_ticket(
                user_id=user.id,
                username=(user.username or "")[:128],
                first_name=(user.first_name or "")[:256],
                text=truncated,
            )
        except Exception:  # noqa: BLE001
            bound.exception("failed to save support ticket (inline-arg form)")
            await message.reply(t("h_feedback_save_failed", lang))
            return

        # #1877: same as ``handle_feedback`` — the row is written and
        # the lock is held; the ack and the admin DM are both network.
        if checkpoint is not None:
            await checkpoint()

        await message.reply(t("h_support_saved", lang, ticket_id=ticket_id))
        bound.info("support ticket saved (inline): id={tid}", tid=ticket_id)
        await _notify_admin(
            bot, admin_chat_id, user=user, text=truncated, bound=bound, ticket_id=ticket_id
        )
        return

    # No args — start FSM
    prior = await state.get_state()
    if prior is not None and prior != SupportStates.awaiting_text.state:
        # In some OTHER flow (withdraw, p2p, check-create, …). Don't
        # hijack it — the user's next message belongs to that flow, so
        # promising "describe your problem" here would send their text
        # somewhere else entirely. Say which way out exists instead.
        await message.reply(t("h_support_busy", lang))
        return
    if prior is not None:
        # Already waiting for the ticket text — guard against double-entry.
        # Re-send the prompt so the user knows they're already waiting, and
        # push the sweeper stamp forward: the user just re-stated intent, so
        # expiring them on the *first* /support's clock would reap a session
        # they are visibly still in. ``update_data`` (not ``set_data``) so
        # nothing else in the bag is dropped.
        await state.update_data({STATE_ENTERED_AT_FIELD: utc_now_iso(), "lang": lang})
        await message.reply(t("h_support_prompt", lang))
        return

    await state.set_state(SupportStates.awaiting_text)
    # Stamp state_entered_at so the global FSM sweeper can reclaim
    # orphaned sessions (same pattern as handlers/rps.py). ``lang`` is
    # stamped too so the sweeper's on_expire DM renders in the user's
    # language without a second users-table read (GAP-1).
    await state.set_data({STATE_ENTERED_AT_FIELD: utc_now_iso(), "lang": lang})
    await message.reply(t("h_support_prompt", lang))
    log.bind(uid=user.id).info("/support FSM started")


async def handle_support_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    support_tickets_repo: SupportTicketsRepo,
    admin_chat_id: int,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Receive the user's free-text message when in ``awaiting_text`` state.

    Saves the ticket, clears FSM, and fires an admin notification (best-
    effort — a failed DM must not roll back the saved row).
    """
    user = require_from_user(message)
    raw = (message.text or "").strip()
    text = raw[:_SUPPORT_MAX_LEN]
    bound = log.bind(uid=user.id, text_len=len(text))

    try:
        ticket_id = await support_tickets_repo.create_open_ticket(
            user_id=user.id,
            username=(user.username or "")[:128],
            first_name=(user.first_name or "")[:256],
            text=text,
        )
    except Exception:  # noqa: BLE001
        bound.exception("failed to save support ticket (FSM form)")
        await state.clear()
        await message.reply(t("h_feedback_save_failed", lang))
        return

    await state.clear()
    # #1877: commit AFTER the state clear so the two agree. A rollback
    # between them would leave the user out of ``awaiting_text`` with no
    # ticket to show for it — their text gone and no way to tell.
    if checkpoint is not None:
        await checkpoint()

    await message.reply(t("h_support_saved", lang, ticket_id=ticket_id))
    bound.info("support ticket saved (FSM): id={tid}", tid=ticket_id)
    await _notify_admin(bot, admin_chat_id, user=user, text=text, bound=bound, ticket_id=ticket_id)


async def _notify_admin(
    bot: Bot,
    admin_chat_id: int,
    *,
    user: object,  # aiogram User — typed loosely to avoid import cycle
    text: str,
    bound: object,  # loguru BoundLogger
    ticket_id: int,
) -> None:
    """Send an admin-notification DM. Swallows API errors (best-effort).

    Centralised here because both the inline-arg and FSM branches share
    the same admin notification contract.
    """
    if not admin_chat_id:
        return
    # ``user`` is always a TG User with .id / .first_name / .username attrs.
    uid: int = getattr(user, "id", 0)
    first_name: str = getattr(user, "first_name", "") or ""
    username: str = getattr(user, "username", "") or ""
    try:
        await bot.send_message(
            admin_chat_id,
            _format_admin_notification(
                uid=uid,
                first_name=first_name,
                username=username,
                text=text,
            ),
        )
    except TelegramAPIError as exc:
        # mypy would complain about ``bound.warning`` on ``object``; the
        # bound logger is always loguru.Logger here, so type-ignore.
        log.warning(  # noqa: G004
            "admin notify failed for ticket {tid}: {e!r}",
            tid=ticket_id,
            e=exc,
        )


# ── T-022: /my_tickets ───────────────────────────────────────────────────────


async def handle_my_tickets(
    message: Message,
    support_tickets_repo: SupportTicketsRepo,
    lang: str,
) -> None:
    """List the calling user's most-recent 10 tickets.

    Each line shows: id, status, creation date (YYYY-MM-DD), and the
    first 80 chars of the ticket text. The ``html.escape`` calls below
    are the ONLY defence, not a belt-and-braces second one: all three
    write sites store the text RAW (``handle_feedback``,
    ``handle_support_command`` and ``handle_support_text`` pass the
    user's bytes through unchanged), and every ``html.escape`` in this
    module is a render-time call. Removing either escape here on the
    theory that the stored value is already safe opens HTML injection
    into a ``parse_mode=HTML`` body.

    Paginated (#120). Ten rows do not overflow today — the worst case
    measures about 1.2k of Telegram's 4096 — but the listing had no
    ceiling of its own: row count, preview width and the row copy are
    all constants an edit can raise, and the failure mode is silent in
    the worst way. Telegram does not truncate an over-long message, it
    refuses it with a 400, so the user sees no answer at all rather
    than a shortened list.
    """
    user = require_from_user(message)

    tickets = await support_tickets_repo.list_by_user(user.id, limit=10)
    if not tickets:
        await message.reply(t("h_my_tickets_empty", lang))
        return

    # Trailing blank line stays in code, not in the copy: paginate_lines
    # joins header and body with a single "\n" and the gap is layout, not
    # something a translator should have to preserve.
    header = t("h_my_tickets_header", lang) + "\n"
    lines: list[str] = []
    for ticket in tickets:
        date_str = ticket.created_at.strftime("%Y-%m-%d") if ticket.created_at else "—"
        raw_preview = (ticket.text or "")[:_TICKET_PREVIEW_LEN]
        if len(ticket.text or "") > _TICKET_PREVIEW_LEN:
            raw_preview += "…"
        preview = html.escape(raw_preview)
        lines.append(
            t(
                "h_my_tickets_line",
                lang,
                id=ticket.id,
                status=html.escape(ticket.status or "open"),
                created=date_str,
                preview=preview,
            )
        )

    pages = paginate_lines(
        header,
        lines,
        more_line=lambda left: t("h_my_tickets_more", lang, count=left),
    )
    await message.reply(pages[0])
    for page in pages[1:]:
        await message.answer(page)
    log.bind(uid=user.id, count=len(tickets)).info("/my_tickets")


# ── T-022: /admin_tickets ────────────────────────────────────────────────────


async def handle_admin_tickets(
    message: Message,
    support_tickets_repo: SupportTicketsRepo,
    settings: Settings,
    lang: str,
) -> None:
    """List the 20 most-recent open tickets (admin only).

    Gated on ``settings.bot.is_developer`` — ticket management is a
    bot-operator concern, not a chat-admin concern (chat admins can /ban,
    but they should not see user DM support requests).

    Paginated for the reason spelled out in :func:`handle_my_tickets`
    (#120), and considerably closer to the wall: twenty rows, each
    carrying a 32-character username on top of the preview, measure
    about 3.1k of the 4096 in the worst case — a quarter of the budget
    away from the message being refused outright.
    """
    user = require_from_user(message)

    if not settings.bot.is_developer(user.id):
        await message.reply(t("h_admin_tickets_only", lang))
        return

    tickets = await support_tickets_repo.list_open(limit=20)
    if not tickets:
        await message.reply(t("h_admin_tickets_empty", lang))
        return

    # Trailing blank line stays in code, not in the copy: paginate_lines
    # re-prints the header on every page, and a copy that carried its own
    # "\n\n" would drift out of sync with the second page's spacing.
    header = t("h_admin_tickets_header", lang) + "\n"
    lines: list[str] = []
    for ticket in tickets:
        date_str = ticket.created_at.strftime("%Y-%m-%d") if ticket.created_at else "—"
        raw_preview = (ticket.text or "")[:_TICKET_PREVIEW_LEN]
        if len(ticket.text or "") > _TICKET_PREVIEW_LEN:
            raw_preview += "…"
        lines.append(
            t(
                "h_admin_tickets_line",
                lang,
                id=ticket.id,
                user_id=ticket.user_id,
                username=html.escape(ticket.username or "—"),
                created=date_str,
                preview=html.escape(raw_preview),
            )
        )

    pages = paginate_lines(
        header,
        lines,
        more_line=lambda left: t("h_admin_tickets_more", lang, count=left),
    )
    await message.reply(pages[0])
    for page in pages[1:]:
        await message.answer(page)
    log.bind(admin=user.id, count=len(tickets)).info("/admin_tickets")


# ── T-022: /ticket_reply ─────────────────────────────────────────────────────


async def handle_ticket_reply(
    message: Message,
    command: CommandObject,
    bot: Bot,
    support_tickets_repo: SupportTicketsRepo,
    users_repo: UsersRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Admin posts a reply to a ticket (developer-only).

    Syntax: ``/ticket_reply <id> <text>``.

    Calls ``add_admin_reply``, then DMs the user. The DM goes out in the
    RECIPIENT's language, not the admin's — ``lang`` here is the
    developer's, and every string in this handler except the DM is
    addressed to them. Legacy hard-coded Russian for this DM
    (bot.py:35852-35854, no ``t()`` at all), so localising it is a
    deliberate improvement rather than a parity break.

    A failed DM (user blocked the bot, etc.) is logged and reported to
    the admin but does NOT undo the stored answer: both Telegram errors
    are caught, so the session middleware still reaches its commit and
    the reply lives in the DB. Note the write is NOT yet committed at
    the point the DM is attempted — the middleware commits only after
    this function returns (middlewares/base.py:132, the ``else`` arm of
    the try/except/else/finally at :111-137) — so an UNCAUGHT exception
    here would roll the answer back.

    #1872 closes that hole for the one call that was still outside
    both wrappers: the ``h_ticket_reply_ok`` reply to the admin, which
    runs AFTER the user's DM has already gone out. Its failure used to
    roll the stored answer back with the DM irreversibly delivered —
    the ticket returned to ``open``, ``/my_tickets`` showed no answer,
    and since :meth:`SupportTicketsRepo.add_admin_reply` accepts only
    ``open`` tickets a second ``/ticket_reply`` was allowed to
    overwrite it. The checkpoint below commits the moment the write is
    known to have landed, before the language lookup and the DM.

    #1941 moved that checkpoint ahead of the outcome branches. The
    guard is now a conditional ``UPDATE`` rather than a ``SELECT``, so
    the session holds the users.db writer lock on EVERY path out of the
    repo call — including the two refusals, where the statement matched
    no row and wrote nothing. Both of those answer the admin over the
    network before returning, and holding the file's write lock across
    a Telegram round trip would queue every other writer behind it
    (``db/engines.py``'s ``_promote_to_write_txn`` says why that
    matters). Committing first costs nothing on a refusal — there is
    nothing to commit — and is exactly #1872's durability point on the
    success path.
    """
    user = require_from_user(message)

    if not settings.bot.is_developer(user.id):
        await message.reply(t("h_admin_tickets_only", lang))
        return

    raw = command_args(command)
    parts = raw.split(maxsplit=1) if raw else []
    if len(parts) < 2 or not is_int_token(parts[0]):
        await message.reply(t("h_ticket_reply_usage", lang))
        return

    ticket_id = int(parts[0])
    answer_text = parts[1].strip()
    if not answer_text:
        await message.reply(t("h_ticket_reply_usage", lang))
        return

    outcome, ticket = await support_tickets_repo.add_admin_reply(
        ticket_id,
        admin_id=user.id,
        answer=answer_text,
    )
    # #1872/#1941: end the transaction before anything touches the
    # network — durability on a win, and handing the writer lock back
    # on a refusal. Nothing below this line writes.
    if checkpoint is not None:
        await checkpoint()

    if outcome is ReplyOutcome.NOT_FOUND:
        await message.reply(t("h_ticket_reply_not_found", lang, id=ticket_id))
        return
    if outcome is ReplyOutcome.NOT_OPEN:
        # Refusing beats overwriting: the row holds a single ``answer``
        # column and nothing archives the previous one.
        assert ticket is not None  # NOT_OPEN always carries the row
        await message.reply(t("h_ticket_reply_not_open", lang, id=ticket_id, status=ticket.status))
        return
    assert ticket is not None  # OK always carries the row

    # The admin has saved the reply. Now DM the user. Best-effort:
    # failure to deliver the DM does not reverse the stored answer —
    # and neither does a failure to work out which language to write it
    # in. Hence the never-raising wrapper rather than
    # ``language_for_user`` itself: per the docstring above the reply
    # is written but NOT yet committed here, so a ``database is
    # locked`` hiccup on this line would reach the session middleware,
    # roll the saved answer back, and tell the admin the reply failed.
    target_lang = await best_effort_language_for_user(
        ticket.user_id,
        users_repo=users_repo,
        settings_repo=user_settings_repo,
        fallback=lang,
    )
    try:
        await bot.send_message(
            ticket.user_id,
            t(
                "h_ticket_user_reply_received",
                target_lang,
                id=ticket_id,
                answer=html.escape(answer_text),
            ),
        )
    except (TelegramForbiddenError, TelegramAPIError) as exc:
        log.warning(
            "ticket DM delivery failed for ticket {tid} to user {uid}: {exc!r}",
            tid=ticket_id,
            uid=ticket.user_id,
            exc=exc,
        )
        await message.reply(t("h_ticket_reply_dm_failed", lang, id=ticket_id))
        return

    await message.reply(t("h_ticket_reply_ok", lang, id=ticket_id))
    log.bind(admin=user.id, ticket_id=ticket_id).info("/ticket_reply sent")


# ── T-022: /ticket_close ─────────────────────────────────────────────────────


async def handle_ticket_close(
    message: Message,
    command: CommandObject,
    support_tickets_repo: SupportTicketsRepo,
    settings: Settings,
    lang: str,
) -> None:
    """Admin marks a ticket closed (developer-only).

    Syntax: ``/ticket_close <id>``.

    Idempotent at the repo level: closing an already-closed ticket is a
    no-op that still returns ``True``.
    """
    user = require_from_user(message)

    if not settings.bot.is_developer(user.id):
        await message.reply(t("h_admin_tickets_only", lang))
        return

    raw = command_args(command)
    if not raw or not is_int_token(raw.strip()):
        await message.reply(t("h_ticket_reply_usage", lang))
        return

    ticket_id = int(raw.strip())
    closed = await support_tickets_repo.mark_closed(ticket_id)
    if not closed:
        await message.reply(t("h_ticket_close_not_found", lang, id=ticket_id))
        return

    await message.reply(t("h_ticket_close_ok", lang, id=ticket_id))
    log.bind(admin=user.id, ticket_id=ticket_id).info("/ticket_close")


# ── Stage-14: /faq static handlers ──────────────────────────────────────────


async def handle_faq(message: Message, lang: str, help_config: HelpConfig | None = None) -> None:
    """Render FAQ part 1 + "Continue" and the command-list link.

    Rendering lives in :mod:`handlers.faq` so ``/faq``, the callback and
    ``/faq2`` cannot drift into three different answers. ``lang`` is the
    effective language stamped by the root ``LanguageMiddleware`` —
    stored preference first, Telegram's ``language_code`` second, so a
    Russian user with an English client still gets Russian.

    ``help_config`` is optional so the older two-argument call sites keep
    working; they simply render without the guide button.
    """
    await message.reply(
        render_faq_part1(lang),
        reply_markup=build_faq_continue_markup(lang, help_config),
    )
    log.bind(chat_id=message.chat.id, lang=lang).info("/faq part 1 rendered")


async def handle_faq_continue_callback(
    callback: CallbackQuery,
    lang: str,
    help_config: HelpConfig | None = None,
) -> None:
    """Edit the FAQ message into the part-2 body.

    Stateless: the body is fully determined by the clicker's effective
    language. No user_id field on the callback wire (see
    ``keyboards/builders/faq.py``) means a click from a *different*
    user on someone else's FAQ message would render their language
    over the original asker's button — that's identical to legacy
    posture (``bot.py:35460`` reads ``call.from_user.id`` with no
    "is this your message" check), so we don't gate it either. The
    user who first asked /faq sees the part-2 body change in-place;
    that's expected within the legacy contract.
    """
    assert callback.from_user is not None  # filter guarantees
    body = render_faq_part2(lang)
    markup = build_faq_part2_markup(lang, help_config)
    # answer_callback first — the small grey toast dismisses the
    # spinner even if edit_text races a Telegram hiccup below.
    await callback.answer()
    # ``callback.message`` is ``Message | InaccessibleMessage | None``
    # (96h+ old post / deleted). Only ``Message`` has ``edit_text``.
    if isinstance(callback.message, MessageType):
        try:
            await callback.message.edit_text(body, reply_markup=markup)
        except TelegramBadRequest:
            # "message is not modified" / "message to edit not found"
            # — drop a fresh send so the user still sees part 2.
            await callback.message.answer(body, reply_markup=markup)
    log.bind(uid=callback.from_user.id, lang=lang).info("/faq continue clicked")


# ── Router factory ───────────────────────────────────────────────────────────


def build_router(admin_chat_id: int, settings: Settings | None = None) -> Router:
    """Build the support router.

    Parameters
    ----------
    admin_chat_id:
        Target for admin-notification DMs. ``0`` means "no admin
        configured" — notifications are silently skipped.
    settings:
        The full :class:`Settings` object.  Needed by T-022 admin
        commands (``is_developer`` check, same as moderation.py). If
        ``None`` (legacy call sites that haven't been updated yet),
        admin commands reject everyone — a safe fallback rather than an
        AttributeError.
    """
    # Optional guide URL for the button under FAQ part 2. ``None`` when
    # no Settings was passed — the button is simply omitted, never a
    # dead link (see ``handlers.faq.build_part2_markup``).
    help_config = settings.help if settings is not None else None
    router = Router(name="support")
    # Most support message handlers are private-only (the ticket FSM,
    # /feedback, the admin commands), but ``/faq`` is the one exception —
    # it worked in GROUPS in legacy (``bot.py:35400``). aiogram checks
    # router-root filters in ``_propagate_event`` BEFORE both own
    # handlers and sub-routers, so a root PRIVATE filter on ``router``
    # would gate ``/faq`` too.
    #
    # So the private family lives in its own child router carrying a
    # single root filter, and ``/faq`` stays on the parent. The split
    # exists for #123: ``with_chat_type_refusal`` reads the command words
    # off the router it wraps, so wrapping the whole module would have
    # made the bot answer "``/faq`` only works in a DM" — which is false.
    # With the split, only the private family gets a refusal twin and
    # ``/faq`` keeps answering everywhere.
    #
    # The FaqContinue callback registration carries no chat filter so a
    # stray click on a forwarded button still resolves through the new
    # pipeline rather than 404ing.
    _PRIVATE_ONLY = F.chat.type == ChatType.PRIVATE
    private = Router(name="support:private")
    private.message.filter(_PRIVATE_ONLY)

    # ── Stage-14: /feedback (one-shot, kept for legacy parity) ──

    async def _handle_feedback(
        message: Message,
        command: CommandObject,
        bot: Bot,
        support_tickets_repo: SupportTicketsRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_feedback(
            message, command, bot, support_tickets_repo, admin_chat_id, lang, checkpoint
        )

    private.message.register(
        _handle_feedback,
        Command("feedback", "отзыв", "review", "kom_feedback", ignore_case=True),
        F.from_user,
    )

    # ── T-022: /support + /ticket — FSM flow ──

    async def _handle_support_command(
        message: Message,
        command: CommandObject,
        bot: Bot,
        state: FSMContext,
        support_tickets_repo: SupportTicketsRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        # ``settings`` is captured from the factory closure.  It is always
        # provided by the production wiring in main_router.py; ``None`` only
        # arises in legacy call sites that predate T-022 and those paths
        # don't reach this closure (they call the old signature).
        if settings is None:
            await message.reply(t("h_admin_tickets_only", lang))
            return
        await handle_support_command(
            message,
            command,
            bot,
            state,
            support_tickets_repo,
            admin_chat_id,
            settings,
            lang,
            checkpoint,
        )

    # NOT StateFilter(None)-gated (mirrors /ad in handlers/ads.py): the
    # escape hatch has to work from *inside* a stuck interview, which is
    # exactly when a user reaches for it. Gated at the router, /support
    # mid-withdraw fell through to the unknown_form hint and told the
    # user they had used the wrong *argument form* of a command they had
    # typed perfectly. Other-flow states are rejected inside the handler,
    # and the inline-arg form (/support <текст>) never touches the FSM.
    private.message.register(
        _handle_support_command,
        Command("support", "ticket", "мои_обращения_help", ignore_case=True),
        F.from_user,
    )

    # Free-text message handler when user is in awaiting_text state.
    async def _handle_support_text(
        message: Message,
        state: FSMContext,
        bot: Bot,
        support_tickets_repo: SupportTicketsRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        _s = settings
        if _s is None:
            return
        await handle_support_text(
            message, state, bot, support_tickets_repo, admin_chat_id, _s, lang, checkpoint
        )

    private.message.register(
        _handle_support_text,
        StateFilter(SupportStates.awaiting_text),
        F.from_user,
        F.text,
        NOT_A_COMMAND,
    )
    # A screenshot of the problem is the obvious thing to send here, and
    # the ticket store holds text only — so say that instead of eating it.
    register_text_expected(private, SupportStates.awaiting_text)

    # ── T-022: /my_tickets ──

    async def _handle_my_tickets(
        message: Message,
        support_tickets_repo: SupportTicketsRepo,
        lang: str,
    ) -> None:
        await handle_my_tickets(message, support_tickets_repo, lang)

    private.message.register(
        _handle_my_tickets,
        Command("my_tickets", "tickets", "мои_обращения", "мои_тикеты", ignore_case=True),
        F.from_user,
    )

    # ── T-022: /admin_tickets ──

    async def _handle_admin_tickets(
        message: Message,
        support_tickets_repo: SupportTicketsRepo,
        lang: str,
    ) -> None:
        _s = settings
        if _s is None:
            await message.reply(t("h_admin_tickets_only", lang))
            return
        await handle_admin_tickets(message, support_tickets_repo, _s, lang)

    private.message.register(
        _handle_admin_tickets,
        Command("admin_tickets", ignore_case=True),
        F.from_user,
    )

    # ── T-022: /ticket_reply ──

    async def _handle_ticket_reply(
        message: Message,
        command: CommandObject,
        bot: Bot,
        support_tickets_repo: SupportTicketsRepo,
        users_repo: UsersRepo,
        user_settings_repo: UserSettingsRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        _s = settings
        if _s is None:
            await message.reply(t("h_admin_tickets_only", lang))
            return
        await handle_ticket_reply(
            message,
            command,
            bot,
            support_tickets_repo,
            users_repo,
            user_settings_repo,
            _s,
            lang,
            checkpoint,
        )

    private.message.register(
        _handle_ticket_reply,
        Command("ticket_reply", ignore_case=True),
        F.from_user,
    )

    # ── T-022: /ticket_close ──

    async def _handle_ticket_close(
        message: Message,
        command: CommandObject,
        support_tickets_repo: SupportTicketsRepo,
        lang: str,
    ) -> None:
        _s = settings
        if _s is None:
            await message.reply(t("h_admin_tickets_only", lang))
            return
        await handle_ticket_close(message, command, support_tickets_repo, _s, lang)

    private.message.register(
        _handle_ticket_close,
        Command("ticket_close", ignore_case=True),
        F.from_user,
    )

    # ── Stage-14: /faq, /check ──

    # /faq is read-only and worked in GROUPS in legacy (``bot.py:35415``
    # — "Один ответ в группе"). It is the one support command that must
    # NOT be private-gated, so it carries no chat filter (every other
    # message handler in this router gets ``_PRIVATE_ONLY`` per-handler;
    # see the note above). The FaqContinue callback is already
    # filter-exempt below — a deliberate #1608 exemption, because a
    # private-only callback filter would strand the in-group reader
    # halfway through a paginated answer.
    async def _handle_faq(message: Message, lang: str) -> None:
        await handle_faq(message, lang, help_config)

    router.message.register(
        _handle_faq,
        Command("faq", "вопросы", "questions", "kom_faq", ignore_case=True),
    )
    # /check + /чек are owned by handlers/checks.py (#26) — the static
    # stub that used to live here was removed when the real flow landed.

    async def _handle_faq_continue(callback: CallbackQuery, lang: str) -> None:
        await handle_faq_continue_callback(callback, lang, help_config)

    router.callback_query.register(
        _handle_faq_continue,
        FaqContinue.filter(),
        F.from_user,
    )
    # Included last so ``/faq`` — an own handler of ``router`` — is
    # matched before the private child, and the refusal twin inside the
    # wrapper is the very last thing consulted.
    router.include_router(with_chat_type_refusal(private, scope="private"))
    return router
