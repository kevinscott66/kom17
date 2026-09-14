"""``/report`` — tell the group's admins about a message.

Port of legacy ``_handle_moderation_report`` (bot.py:32030-32086) —
with the transport turned around, because a literal port would ship a
feature that almost never fires.

Legacy's only entry point was a **DM forward**: the user forwarded the
offending message to the bot in private, and the bot read
``message.forward_from_chat`` to learn which group it came from. Bot
API 7.0 removed that field. Its replacement, ``forward_origin``, only
carries a chat when the original author was a channel
(:class:`MessageOriginChannel`) or an anonymous / on-behalf-of-a-chat
sender (:class:`MessageOriginChat`). An ordinary member's message
forwards as :class:`MessageOriginUser` — **no chat information at
all** — so the source group is unresolvable, and that is precisely the
case a report is about. The legacy path was already mostly dead on the
current API; porting it verbatim would have reproduced the silence.

So the feature is a documented superset of legacy:

1. **``/report`` in the group, as a reply** to the offending message.
   This is the path that actually works, and the one the guide
   advertises. The source chat is ``message.chat`` — nothing to
   resolve, nothing to spoof.
2. **``/report [comment]`` in a DM, as a reply to a forward** whose
   origin *is* resolvable (channel post, anonymous admin, on-behalf-of
   a chat). Legacy parity, kept because it still works for that slice.
3. A DM ``/report`` on an unresolvable forward answers with a hint
   pointing at (1) instead of legacy's silent ``return False``.

Deliberate deviations from legacy, each closing something legacy left
open:

* **Membership check on the DM path.** Legacy DM-blasted the admins of
  any group whose id it could read off a forward — the reporter never
  had to be a member. Anyone who obtained a forwarded channel post
  could ping that chat's whole admin bench, repeatedly. The DM path
  now verifies the reporter is actually in the source chat
  (``get_chat_member``, status not LEFT/KICKED) and fails **closed** on
  an API error.
* **Per-user cooldown.** Legacy had none: the fan-out is one DM per
  admin per report, so an unthrottled ``/report`` is an amplifier
  aimed at the staff of a chat the reporter may not even be in.
* **Capped fan-out, paced and flood-aware.** Same shape as the VIP
  expiry notifier (``scheduler/economy_cleanup``): a pause between
  recipients, ``TelegramRetryAfter`` slept through and retried once,
  per-recipient failures logged and skipped. Legacy sent in a tight
  loop and swallowed every error, so one flood wait silently dropped
  every remaining admin in the batch.
* **Live Telegram admins only**, minus bots and minus the reporter.
  Legacy unioned the bot's own ``group_roles`` table with the live
  list; a stale row there is a DM to someone who is no longer staff.
  ``get_chat_administrators`` is the authority on who can act on a
  report right now.
* **HTML, escaped.** Legacy sent the body with ``parse_mode="Markdown"``
  and interpolated the chat title, the reporter's name and the free-text
  comment raw — a title containing ``*`` broke the message, and a
  crafted comment could re-style it. The new body is HTML with every
  interpolation through :func:`html.escape` and every field length-capped
  so the 4096 ceiling cannot be reached.
* **The body and the forward are separate deliveries.** Legacy wrapped
  both in one ``try``: in a group with protected content the forward
  fails, so the admin got *nothing* and the report counted as
  undelivered. Now the body always lands and only the copy of the
  offending message is best-effort.

Router placement (see ``routers/main_router``): included **after** the
AI router on purpose. The DM half only claims explicit ``/report``
commands — never a bare forward — so a user who forwarded something to
the model in a «Войти в Ком» session keeps getting the model.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import time
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import MessageOriginChannel, MessageOriginChat
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.report")

#: One report per user per minute, across every chat. The unit is the
#: *reporter*, not the chat: the cost being throttled is the DM fan-out,
#: and hopping groups must not reset it.
_COOLDOWN_SECONDS = 60.0

#: Bounded so a spam wave cannot grow the table without limit — same
#: reasoning as the rate-limit / language caches (tasks #72, #74, #79).
#: Entries are one float each; 4096 distinct reporters per minute is far
#: past anything this bot sees.
_COOLDOWN_CAPACITY = 4096

#: Telegram allows up to 50 admins in a supergroup. Notifying every one
#: of them is a 50-message burst for a single ``/report``; the people who
#: act on reports are the first few anyway.
_MAX_ADMINS = 20

#: Between recipients — the same pacing the VIP expiry fan-out uses.
_SEND_PAUSE_SECONDS = 0.05

#: A flood wait longer than this is not worth blocking the handler for;
#: the remaining admins are skipped and logged instead.
_MAX_RETRY_AFTER_SECONDS = 30

#: Length caps on the three interpolated fields (code points, applied
#: BEFORE escaping). Worst-case escaping expands each character sixfold
#: (``&`` → ``&amp;``), so 400 + 100 + 100 caps the body at ~3.6k UTF-16
#: units and the 4096 ceiling stays out of reach without a clamp that
#: could cut an HTML tag in half.
_COMMENT_MAX_LEN = 400
_TITLE_MAX_LEN = 100
_REPORTER_MAX_LEN = 100

#: A member who left or was banned is not "in the chat" for the purpose
#: of the DM path's membership check.
_ABSENT_STATUSES = frozenset({ChatMemberStatus.LEFT, ChatMemberStatus.KICKED})

_COOLDOWN: TTLLRUCache[int, float] = TTLLRUCache(_COOLDOWN_SECONDS, _COOLDOWN_CAPACITY)


def _reset_cooldown_for_tests() -> None:
    """Drop every recorded cooldown.

    The cache is module-level (one process, one bot) so tests would
    otherwise leak a cooldown from one case into the next. Named
    explicitly rather than reaching into ``_COOLDOWN`` from the test so
    the coupling is visible from this side too.
    """
    _COOLDOWN.clear()


def _cooldown_remaining(user_id: int, now: float) -> int:
    """Seconds left on ``user_id``'s cooldown; ``0`` when clear.

    Rounded **up** so a caller 0.2s early is told "1", never "0 seconds
    left, try again" — which reads as a bug.
    """
    deadline = _COOLDOWN.get(user_id, now)
    if deadline is None:
        return 0
    return max(1, int(deadline - now) + 1)


def _origin_chat_id(message: Message) -> int | None:
    """Source chat id of ``message``'s forward origin, if it has one.

    ``None`` for a message that was not forwarded, and — the case this
    whole module is shaped around — for a forward whose origin is a
    plain user (:class:`MessageOriginUser`) or a hidden one: Bot API 7.0
    carries no chat there, so the group is genuinely unknowable. Also
    ``None`` for a positive id, which is a private chat rather than a
    group (legacy's ``report_chat.id >= 0`` guard).
    """
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChat):
        chat_id = origin.sender_chat.id
    elif isinstance(origin, MessageOriginChannel):
        chat_id = origin.chat.id
    else:
        return None
    return chat_id if chat_id < 0 else None


def _reporter_name(message: Message) -> str:
    """``First Name (@username)`` — legacy's exact composition, capped.

    Falls back to the numeric id when a user has neither (deleted
    accounts and some bots), so the admins always get *something*
    actionable.
    """
    tg_user = require_from_user(message)
    name = tg_user.first_name or ""
    if tg_user.username:
        name = f"{name} (@{tg_user.username})"
    return (name.strip() or str(tg_user.id))[:_REPORTER_MAX_LEN]


async def _chat_title(bot: Bot, chat_id: int) -> str | None:
    """Title of ``chat_id``, or ``None`` when the bot cannot see it.

    ``None`` is the "bot is not in this group" signal — the caller
    answers ``report_bot_not_in_group`` and stops, exactly as legacy
    did. Falling back to the raw id (legacy does that too, but only on
    a *successful* ``get_chat`` with no title) keeps a title-less chat
    reportable.
    """
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:  # noqa: BLE001 — "not a member" is the common case
        log.bind(chat_id=chat_id).debug("report: get_chat failed: {}", exc)
        return None
    return (chat.title or str(chat_id))[:_TITLE_MAX_LEN]


async def _is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Is ``user_id`` currently in ``chat_id``? Fails **closed**.

    An API error here means we could not establish the reporter's
    standing, and the action being authorised is a DM burst at other
    people. Refusing is the cheap failure — the reporter can use
    ``/report`` inside the group, where no check is needed at all.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — see the fail-closed note above
        log.bind(chat_id=chat_id, uid=user_id).warning(
            "report: get_chat_member failed, refusing: {}", exc
        )
        return False
    return member.status not in _ABSENT_STATUSES


async def _admin_ids(bot: Bot, chat_id: int, exclude: int) -> list[int]:
    """Live, non-bot admins of ``chat_id`` minus ``exclude``, capped.

    Empty means "nobody to notify" — either the API refused, or the
    only admins are bots and the reporter themselves. The caller says so
    rather than reporting a delivery that did not happen.
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:  # noqa: BLE001 — no admins ⇒ localized refusal
        log.bind(chat_id=chat_id).warning("report: get_chat_administrators failed: {}", exc)
        return []
    ids = [admin.user.id for admin in admins if not admin.user.is_bot and admin.user.id != exclude]
    return ids[:_MAX_ADMINS]


def _notify_body(lang: str, *, chat_title: str, reporter: str, comment: str) -> str:
    """The HTML card each admin receives.

    Not ``report_notify_body`` (the legacy key carried over into the
    yamls): that template is Markdown (``**bold**``) and this bot runs a
    process-wide HTML ``parse_mode``, so reusing it would render the
    asterisks literally at best. ``h_report_notify`` is its HTML twin;
    the legacy key stays untouched for the legacy process.
    """
    return t(
        "h_report_notify",
        lang,
        chat_title=html.escape(chat_title),
        reporter=html.escape(reporter),
        comment=html.escape(comment or t("report_no_comment", lang)),
    )


async def _fan_out(
    bot: Bot,
    admin_ids: list[int],
    body: str,
    *,
    from_chat_id: int,
    message_id: int,
) -> int:
    """DM ``body`` to each admin and forward the reported message after it.

    Returns how many admins actually received the body. The forward is
    best-effort and deliberately does NOT gate the count: a group with
    protected content refuses forwards, and legacy's single ``try``
    around both then reported "0 admins notified" while every admin had
    in fact been told what happened and by whom.

    Paced and flood-aware for the reason spelled out in the module
    docstring; ``CancelledError`` is re-raised so a shutdown mid-fan-out
    is not swallowed as a delivery failure.
    """
    notified = 0
    for index, admin_id in enumerate(admin_ids):
        if index:
            await asyncio.sleep(_SEND_PAUSE_SECONDS)
        try:
            try:
                await bot.send_message(admin_id, body)
            except TelegramRetryAfter as exc:
                wait = min(exc.retry_after, _MAX_RETRY_AFTER_SECONDS)
                log.bind(admin_id=admin_id, retry_after=exc.retry_after, wait=wait).warning(
                    "report fan-out flood wait; sleeping then retrying once"
                )
                await asyncio.sleep(wait)
                # One retry only — a capped wait may bounce again, and
                # that admin is simply skipped.
                await bot.send_message(admin_id, body)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — blocked bot / never started it
            log.bind(admin_id=admin_id).debug("report DM failed: {}", exc)
            continue
        notified += 1
        # Best-effort copy of the offending message. Suppressed rather
        # than logged-and-counted: protected content and deleted
        # messages are ordinary facts, not incidents.
        with contextlib.suppress(Exception):
            await bot.forward_message(admin_id, from_chat_id, message_id)
    return notified


async def _dispatch(
    message: Message,
    bot: Bot,
    lang: str,
    *,
    source_chat_id: int,
    chat_title: str,
    comment: str,
    from_chat_id: int,
    reported_message_id: int,
) -> None:
    """Shared tail of both entry points: resolve admins, fan out, answer.

    The caller has already claimed the cooldown; this function only
    ever hands it *back*, on the outcome that costs the reporter
    nothing — "no admins to notify" must not lock them out for a minute
    over a report nobody got.
    """
    reporter_id = require_from_user(message).id
    admin_ids = await _admin_ids(bot, source_chat_id, exclude=reporter_id)
    if not admin_ids:
        _release_cooldown(reporter_id)
        await message.reply(t("h_report_no_admins", lang))
        log.bind(uid=reporter_id, chat_id=source_chat_id).info("/report: no admins to notify")
        return

    body = _notify_body(
        lang,
        chat_title=chat_title,
        reporter=_reporter_name(message),
        comment=comment,
    )
    notified = await _fan_out(
        bot,
        admin_ids,
        body,
        from_chat_id=from_chat_id,
        message_id=reported_message_id,
    )
    await message.reply(t("report_sent", lang, count=notified))
    log.bind(
        uid=reporter_id,
        chat_id=source_chat_id,
        admins=len(admin_ids),
        notified=notified,
    ).info("/report dispatched")


def _claim_cooldown(message: Message, lang: str) -> str | None:
    """Take the reporter's minute, or a localized refusal if it is gone.

    #2015: reading the cooldown and stamping it have to be one
    await-free step. The stamp used to land at the end of
    :func:`_dispatch`, past ``getChatAdministrators`` and a message plus
    a forward per admin — so every ``/report`` sent during that window
    read a clear cooldown and bought its own fan-out. The minute exists
    to bound exactly that DM burst.

    Claiming up front means the claim can be wrong, so the two outcomes
    that cost the reporter nothing hand it back through
    :func:`_release_cooldown`: no admins to notify, and (on the DM path)
    a source chat the bot cannot see or the reporter is not in.
    """
    reporter_id = require_from_user(message).id
    now = time.monotonic()
    remaining = _cooldown_remaining(reporter_id, now)
    if remaining:
        return t("h_report_cooldown", lang, seconds=remaining)
    # Value and expiry are the same instant: the cache evicts the entry
    # when it lapses, and the value is what ``_cooldown_remaining``
    # subtracts from to render "try again in N seconds".
    _COOLDOWN.put(reporter_id, now + _COOLDOWN_SECONDS, now)
    return None


def _release_cooldown(user_id: int) -> None:
    """Give back a claim from :func:`_claim_cooldown` that went unspent."""
    _COOLDOWN.discard(user_id)


async def handle_group_report(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/report [comment]`` in a group, as a reply to the offending message.

    The path that actually works on Bot API 7.0 and the one the guide
    documents. No membership check: sending a message in the chat *is*
    the proof of membership. No ``get_chat`` either — the title is right
    there on the update.
    """
    user = await user_service.touch(require_from_user(message))
    lang = user.language
    # #220: everything below this line talks to Telegram — up to
    # ``getChatAdministrators`` plus one forward and one message per
    # admin. The ``touch`` above is bookkeeping that stands either way,
    # so end its transaction here rather than hold ``users.db``'s single
    # writer slot for the whole fan-out. See :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()

    target = message.reply_to_message
    if target is None:
        await message.reply(t("h_report_reply_required", lang))
        return
    if target.from_user is not None and target.from_user.id == user.user_id:
        await message.reply(t("h_report_self", lang))
        return

    refusal = _claim_cooldown(message, lang)
    if refusal is not None:
        await message.reply(refusal)
        return

    await _dispatch(
        message,
        bot,
        lang,
        source_chat_id=message.chat.id,
        chat_title=(message.chat.title or str(message.chat.id))[:_TITLE_MAX_LEN],
        comment=command_args(command)[:_COMMENT_MAX_LEN],
        from_chat_id=message.chat.id,
        reported_message_id=target.message_id,
    )


async def handle_private_report(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/report [comment]`` in a DM, as a reply to a forwarded message.

    Legacy's transport, kept for the slice of forwards that still carry
    their source chat. Everything else gets the hint that points at the
    in-group form — the one case Bot API 7.0 made unanswerable is also
    the most common one, so saying so beats saying nothing.
    """
    user = await user_service.touch(require_from_user(message))
    lang = user.language
    # #220: this path asks Telegram even more — ``getChat``,
    # ``getChatMember`` and then the same fan-out. Same reasoning as the
    # group twin above.
    if checkpoint is not None:
        await checkpoint()

    target = message.reply_to_message
    if target is None or target.forward_origin is None:
        await message.reply(t("h_report_dm_usage", lang))
        return

    source_chat_id = _origin_chat_id(target)
    if source_chat_id is None:
        await message.reply(t("h_report_forward_hint", lang))
        return

    refusal = _claim_cooldown(message, lang)
    if refusal is not None:
        await message.reply(refusal)
        return

    chat_title = await _chat_title(bot, source_chat_id)
    if chat_title is None:
        _release_cooldown(user.user_id)
        await message.reply(t("report_bot_not_in_group", lang))
        return

    if not await _is_member(bot, source_chat_id, user.user_id):
        _release_cooldown(user.user_id)
        await message.reply(t("h_report_not_member", lang))
        log.bind(uid=user.user_id, chat_id=source_chat_id).info(
            "/report: reporter is not in the source chat"
        )
        return

    await _dispatch(
        message,
        bot,
        lang,
        source_chat_id=source_chat_id,
        chat_title=chat_title,
        comment=command_args(command)[:_COMMENT_MAX_LEN],
        # The copy the admins receive is the forward sitting in the
        # reporter's DM — legacy forwarded the same message object.
        from_chat_id=message.chat.id,
        reported_message_id=target.message_id,
    )


def build_router() -> Router:
    """Two chat-type-disjoint registrations of the same command word.

    No router-level chat filter: the group and private halves answer
    ``/report`` with different rules, so each registration carries its
    own ``F.chat.type`` guard and they can never both match one update.

    Takes no ``registry`` — the handler reads nothing from the bot's
    databases. Everything it needs (who is an admin, who is a member,
    what the chat is called) is live Telegram state, and asking Telegram
    is the whole point: a report acted on by someone the bot *used to*
    think was staff is worse than no report.
    """
    router = Router(name="report")

    router.message.register(
        handle_group_report,
        Command("report", "репорт", "kom_report", ignore_case=True),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
        F.from_user,
    )
    router.message.register(
        handle_private_report,
        Command("report", "репорт", "kom_report", ignore_case=True),
        F.chat.type == ChatType.PRIVATE,
        F.from_user,
    )
    return router
