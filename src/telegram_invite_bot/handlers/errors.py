"""Global aiogram error handler.

Catches any exception raised inside a registered handler so that:

1. ``HANDLER_ERRORS`` Prometheus counter is bumped with the exception
   class label — alerting can scope to e.g. ``OperationalError`` spikes
   without firing on every cosmetic ``ValueError``.
2. The traceback lands in loguru via ``logger.exception``, picking up
   the surrounding ``contextualize`` block (``request_id``,
   ``update_id``) that the webhook server set up.
3. The user gets a brief, *non-leaking* "что-то пошло не так" reply —
   never the raw exception message (which can contain SQL fragments,
   API tokens echoed back from third-party errors, internal paths).
   Benign Telegram rejections (:func:`_is_benign`) are the exception:
   they are counted and logged but never reported to the user, because
   an unchanged card or a blocked bot is not something the user did
   wrong or can retry.
4. The handler returns ``True`` so aiogram does NOT log a redundant
   "Cause exception while process update" warning on top of our line.

Without this, an unhandled exception bubbles up to aiogram's default
which logs to stdlib ``logging`` (we route stdlib → loguru, but the
default log line is much less informative than what we emit here)
and never tells the user anything happened — they see a hung command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from aiogram import Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from loguru import logger

from telegram_invite_bot.utils.aiogram import BENIGN_EDIT_REJECTS, NO_TEXT_TO_EDIT
from telegram_invite_bot.utils.language import resolve_lang
from telegram_invite_bot.webhook.metrics import HANDLER_ERRORS

log = logger.bind(component="handlers.errors")

# M-I-4: bound the cardinality of the ``exc_type`` label on
# ``tib_handler_errors_total``. A flaky upstream (aiohttp, OpenAI SDK,
# SQLAlchemy) can raise dozens of distinct exception class names that
# would otherwise grow the Prometheus label set unbounded — past the
# ~10⁴ ceiling Prometheus recommends. Only class names in this
# allowlist pass through verbatim; anything else collapses to
# ``"Other"`` so the metric's series count is fixed.
_ALLOWED_EXC_TYPE_LABELS: frozenset[str] = frozenset(
    {
        "TelegramBadRequest",
        "TelegramRetryAfter",
        "TelegramNotFound",
        "TelegramForbiddenError",
        "TelegramAPIError",
        "RuntimeError",
        "DBError",
        "OperationalError",
        "IntegrityError",
        "TimeoutError",
        "ValueError",
        # #1692: the sibling of ValueError that a ``try: int(x)``
        # / ``except ValueError`` does NOT catch. Named here so the
        # sites that still lack a magnitude bound are a distinct
        # signal instead of another anonymous "Other".
        "OverflowError",
        "KeyError",
        "TypeError",
        "ValidationError",
        "BenignReject",
        "Other",
    }
)

#: Telegram rejections that describe an ordinary fact about the chat, not
#: a bug in us: the card the user tapped is unchanged / gone / past its
#: edit window, the callback query aged out (a tap on a card left open,
#: or a handler that took longer than Telegram's ~15 s answer window), or
#: a message we tried to sweep was already deleted. Matched as substrings
#: because the API prefixes them with ``Bad Request: `` and appends
#: detail. :data:`~utils.aiogram.BENIGN_EDIT_REJECTS` is reused rather
#: than re-listed so the helper and the safety net cannot drift.
#:
#: :data:`~utils.aiogram.NO_TEXT_TO_EDIT` joins them here but deliberately
#: NOT there: inside :func:`~utils.aiogram.edit_card` that rejection earns
#: a caption retry, because the edit is still wanted and still possible.
#: By the time it reaches this router the retry either never existed (a
#: raw ``edit_text`` on what turned out to be a media card) or already
#: failed, and either way the user is owed silence, not an apology for a
#: card that is simply the wrong shape.
_BENIGN_REJECT_MARKERS: tuple[str, ...] = (
    *BENIGN_EDIT_REJECTS,
    NO_TEXT_TO_EDIT,
    "query is too old",
    "message to delete not found",
    "message can't be deleted",
)


def _is_benign(exc: BaseException) -> bool:
    """True for failures the user must not be shown an error card for.

    Two families. :class:`TelegramForbiddenError` means the user blocked
    the bot or we were kicked — there is nobody to apologise to, and the
    reply attempt would fail anyway. The marker list covers stale cards
    and aged-out callback queries: normal outcomes of a chat that moved
    on, indistinguishable from a bug only if you read the exception
    class instead of the message.

    Benign does not mean invisible: the caller still bumps
    ``HANDLER_ERRORS`` (under its own ``BenignReject`` label, so alerts
    can scope to real ones) and still logs a line. It only drops the
    traceback and the user-facing "⚠️ Произошла ошибка".
    """
    if isinstance(exc, TelegramForbiddenError):
        return True
    if not isinstance(exc, TelegramAPIError):
        return False
    text = str(exc)
    return any(marker in text for marker in _BENIGN_REJECT_MARKERS)


def _bounded_exc_label(exc: BaseException) -> str:
    """Map an exception class name to a member of the bounded label set.

    Pulled out of the handler so unit tests can exercise the mapping
    without a full ErrorEvent fixture.
    """
    name = type(exc).__name__
    if name in _ALLOWED_EXC_TYPE_LABELS:
        return name
    return "Other"


if TYPE_CHECKING:
    from aiogram.types import ErrorEvent, Message
    from aiogram.types import User as TelegramUser


_USER_REPLY_RU = "⚠️ Произошла ошибка. Мы уже знаем. Попробуй ещё раз через минуту."
_USER_REPLY_EN = "⚠️ Something went wrong. We're on it. Try again in a minute."

# Toast shown *on the button itself* — a separate, shorter pair: the
# popup is capped at 200 chars by Telegram and is read in a glance,
# while the chat reply above can afford the full sentence.
_TOAST_RU = "⚠️ Ошибка. Мы уже знаем, попробуй ещё раз."
_TOAST_EN = "⚠️ Something went wrong. Try again."


def build_errors_router() -> Router:
    """Return a router whose only handler is the global error catcher.

    Included LAST into the main router (the final ``include_router``
    call in ``build_main_router``), not into the dispatcher — the only
    ``dispatcher.include_router`` call is the one in
    ``AppProvider.dispatcher`` and it takes ``build_main_router``
    alone. Last means feature routers get first dibs on each event; the
    error path only fires when one of them raised.
    """
    router = Router(name="errors")

    @router.error()
    async def _on_error(event: ErrorEvent, **data: Any) -> bool:
        exc = event.exception
        if _is_benign(exc):
            # A stale card or a blocked bot is not an incident. Counted
            # and logged so a call site that forgot ``edit_card`` still
            # shows up, but no traceback and — crucially — no reply: the
            # user tapped a button that had nothing left to do, and
            # telling them "произошла ошибка" is itself the bug.
            HANDLER_ERRORS.labels(exc_type="BenignReject").inc()
            log.warning("benign telegram reject, no user reply: {exc}", exc=exc)
            # "No reply" means no *text* — the spinner still has to stop,
            # otherwise the button the user tapped keeps loading for the
            # full ~15 s timeout on an outcome we deliberately call fine.
            await _stop_spinner(event, data, silent=True)
            return True
        # ``exc_type`` is the class name only — full module path would
        # blow up the Prometheus cardinality (every refactor that moves
        # a class becomes a new label value).
        HANDLER_ERRORS.labels(exc_type=_bounded_exc_label(exc)).inc()
        log.exception("handler raised: {exc}", exc=exc)

        # Try to reply to the originating user. ``event.update`` carries
        # whichever update triggered the failed handler; we look at
        # ``message`` (commands, text) and ``callback_query`` (buttons)
        # which cover ~all reachable failures. Anything else (channel
        # post, my_chat_member) silently drops the user-facing reply —
        # logging + metric is still emitted. The callback path gets the
        # toast first so the button stops spinning even if the reply
        # below cannot be delivered at all.
        await _stop_spinner(event, data, silent=False)
        await _try_reply_to_user(event, data)

        # Returning ``True`` tells aiogram "I handled this, don't log
        # again." Returning ``False`` would double-log via aiogram's
        # default exception logger; we don't want both lines.
        return True

    return router


async def _stop_spinner(event: ErrorEvent, data: dict[str, Any], *, silent: bool) -> None:
    """Answer the callback query so the tapped button stops spinning.

    ``Message.answer`` in :func:`_try_reply_to_user` sends a *new*
    message; it does nothing for the loading indicator Telegram spins
    on the inline button until ``answerCallbackQuery`` arrives or the
    query ages out (~15 s). Legacy ``bot.py:1305`` answered the query
    *and* sent the reply; the port kept only the second half, so every
    exception raised inside a callback handler left the button loading
    long after the user had already been told something went wrong.

    ``silent=True`` answers with no text at all. That is the benign
    branch: a visible "произошла ошибка" popup on a stale card is
    precisely the bug that branch exists to prevent — but the spinner
    must stop regardless, and an empty answer does exactly that.

    Best-effort by construction. An aged-out query cannot be answered
    (``query is too old`` is itself one of :data:`_BENIGN_REJECT_MARKERS`),
    and nothing raised here may displace the original exception.
    """
    callback = event.update.callback_query
    if callback is None:
        return
    text = None if silent else _pick_toast_language(data, callback.from_user)
    try:
        await callback.answer(text)
    except Exception:  # noqa: BLE001 — must never displace the original failure
        log.debug("could not stop the callback spinner")


async def _try_reply_to_user(event: ErrorEvent, data: dict[str, Any]) -> None:
    """Best-effort user reply. NEVER raises — a failure here would
    create an infinite error-handling loop (error handler erroring is
    NOT routed back through the error handler, but we still don't want
    a stray log line on every handler failure).
    """
    try:
        update = event.update
        target: Message | None = None
        text = _USER_REPLY_RU
        if update.message is not None:
            target = update.message
            text = _pick_reply_language(data, update.message.from_user)
        elif update.callback_query is not None and update.callback_query.message is not None:
            # callback_query.message is the original message with the
            # button; reply there so the failure shows up under the
            # action the user just took.
            msg = update.callback_query.message
            # In aiogram 3 callback_query.message is MaybeInaccessibleMessage —
            # the inaccessible variant lacks ``.answer``. Narrow to the
            # accessible subtype by attribute presence.
            if hasattr(msg, "answer"):
                target = cast("Message", msg)
                # #186: the language comes from the *tapping* user, not
                # from ``msg``. The card the button hangs on was sent by
                # the bot, so ``msg.from_user`` is the bot account — its
                # ``language_code`` is ``None`` and every EN user used to
                # get the Russian sentence. ``callback_query.from_user``
                # is the person who tapped, which is the same source
                # :func:`_stop_spinner` already uses for the toast.
                text = _pick_reply_language(data, update.callback_query.from_user)
        if target is not None:
            await target.answer(text)
    except TelegramAPIError:
        # User-side problem (blocked the bot, deleted chat, etc.) —
        # log at debug and move on; this is not our bug.
        log.debug("could not deliver error reply (telegram side)")
    except Exception:  # noqa: BLE001 — last-resort guard, see docstring
        log.exception("failed to deliver error reply")


def _pick_toast_language(data: dict[str, Any], user: TelegramUser | None) -> str:
    """RU/EN for the button toast — same rule as :func:`_pick_reply_language`,
    but keyed off the callback's ``from_user`` because the message the
    button hangs on may be the inaccessible variant (no ``from_user``
    of the tapping user, and possibly no message object we can trust).
    """
    if resolve_lang(data, user) == "en":
        return _TOAST_EN
    return _TOAST_RU


def _pick_reply_language(data: dict[str, Any], user: TelegramUser | None) -> str:
    """Choose RU/EN reply via the effective language.

    The error router runs outside the message/callback pipeline, so the
    root :class:`LanguageMiddleware` may not have stamped ``data["lang"]``
    — we use :func:`resolve_lang`, which prefers the stamped value and
    otherwise falls back to the originating user's Telegram locale. We
    keep the literal RU/EN pair (rather than the i18n layer) so this
    module stays decoupled and never loads the YAML for a single string.

    Takes the *user* rather than the message for the same reason
    :func:`_pick_toast_language` does: on the callback branch the only
    message we hold is the bot's own card, whose ``from_user`` is the
    bot. Callers pass the originating human — ``message.from_user`` on
    the message branch, ``callback_query.from_user`` on the callback
    branch.
    """
    if resolve_lang(data, user) == "en":
        return _USER_REPLY_EN
    return _USER_REPLY_RU
