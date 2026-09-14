"""Tiny aiogram glue helpers.

Lives here rather than under ``handlers/`` because it's a dependency-free
utility module — no Router, no DI — and other modules (services,
middlewares) may eventually want the same narrowing.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from loguru import logger

from telegram_invite_bot.utils.render import TELEGRAM_CAPTION_LIMIT, parsed_length

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import InlineKeyboardMarkup, Message, User


log = logger.bind(component="utils.aiogram")


#: Telegram rejects an edit for reasons that are ordinary facts about an
#: old card rather than bugs: nothing changed, the card is past the 48h
#: edit window, or the user deleted it. Matched as substrings because the
#: API prefixes them with ``Bad Request: `` and sometimes appends detail.
#: The one benign reject that must never earn a fallback *message*: the
#: card already renders what the caller wants. Named separately because
#: the other two markers describe a card that is gone or frozen, where
#: posting a fresh one is exactly right, and a helper that cannot tell
#: them apart answers a re-tap with a duplicate card (#722).
NOT_MODIFIED: str = "message is not modified"

BENIGN_EDIT_REJECTS: tuple[str, ...] = (
    NOT_MODIFIED,
    "message can't be edited",
    "message to edit not found",
)


#: Telegram's refusal when the card carries a caption instead of text —
#: ``editMessageText`` does not touch a media message's body. NOT in
#: :data:`BENIGN_EDIT_REJECTS`: the edit is still wanted and still
#: possible, just through a different method, so this marker earns a
#: fallback rather than a swallow. Prod hit it six times in a month
#: (five of them on a ``ratnav`` page tap) and every one of those taps
#: showed the user «⚠️ Произошла ошибка» instead of the next page.
NO_TEXT_TO_EDIT: str = "there is no text in the message to edit"

#: ``editMessageText`` parameters ``editMessageCaption`` does not accept.
#: Dropped on the fallback instead of forwarded — aiogram validates the
#: model, so passing one through would turn a rescued edit into a
#: ``TypeError`` and lose the card for real. Only the preview switch is
#: in reach today (three call sites pass it); the link-preview pair is
#: listed because it is the same parameter under aiogram's newer name.
_TEXT_ONLY_EDIT_KWARGS: frozenset[str] = frozenset(
    {"disable_web_page_preview", "link_preview_options", "entities"}
)


async def edit_card(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> bool:
    """Re-render an inline card in place; ``False`` if Telegram refused benignly.

    Every callback that redraws its own card needs the same three-marker
    swallow (see :data:`BENIGN_EDIT_REJECTS`) — a tap on a card the user
    kept open for two days must not raise into the error middleware and
    spend a Sentry event on "the card is old". Anything outside that
    tuple still propagates: a malformed-HTML edit is a real bug and must
    stay loud.

    The one exception is :data:`NO_TEXT_TO_EDIT`, which says the card is
    a media message: same card, same intent, different Bot API method.
    That falls through to ``editMessageCaption`` instead of raising, so
    the tap does what the button promised. A callback factory outlives
    the surface that minted it — ``RatingNav`` alone is reachable from
    four keyboards — so "this card happens to be a photo" is a fact
    about one old message, never a reason to show an error.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as exc:
        detail = str(exc)
        if NO_TEXT_TO_EDIT in detail:
            return await _edit_card_caption(message, text, reply_markup=reply_markup, **kwargs)
        if not any(marker in detail for marker in BENIGN_EDIT_REJECTS):
            raise
        return False
    return True


async def _edit_card_caption(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> bool:
    """Redraw a media card's caption; ``False`` if that is refused too.

    Captions get a quarter of a message (:data:`TELEGRAM_CAPTION_LIMIT`),
    so a body sized for a text card can simply not fit. Checking up front
    rather than letting Telegram answer "message caption is too long"
    keeps that case out of the error middleware as well — it is still
    just an old card in the wrong shape, not a bug in the caller.
    """
    if parsed_length(text) > TELEGRAM_CAPTION_LIMIT:
        log.bind(chat_id=message.chat.id, message_id=message.message_id).warning(
            "card body ({n} chars) does not fit a caption; leaving the media card as is",
            n=parsed_length(text),
        )
        return False
    caption_kwargs = {k: v for k, v in kwargs.items() if k not in _TEXT_ONLY_EDIT_KWARGS}
    try:
        await message.edit_caption(caption=text, reply_markup=reply_markup, **caption_kwargs)
    except TelegramBadRequest as exc:
        if not any(marker in str(exc) for marker in BENIGN_EDIT_REJECTS):
            raise
        return False
    return True


#: Telegram refuses the *whole* ``sendMessage`` when ``reply_to_message_id``
#: points at a message that no longer exists — deleted by the user, by a
#: group's cleaner bot, or aged out. Says nothing about the text itself.
REPLY_TARGET_LOST: tuple[str, ...] = (
    "message to be replied not found",
    "replied message not found",
    "message to reply not found",
)


class UndeliverableResultError(RuntimeError):
    """A result the user already paid for could not be shown at all.

    Raised by handlers that have already written to the database. The
    point is the *escape*, not the message: ``middlewares.base`` rolls
    the session back on any exception, so letting this out undoes the
    charge along with everything else it bought. A charge nobody could
    see is otherwise left for an operator to refund by hand.
    """


async def reply_or_send(message: Message, text: str, **kwargs: Any) -> bool:
    """Deliver ``text`` under ``message``; ``False`` if the chat is gone.

    For an outcome the user has already paid for — a spent activity, a
    settled game — the result has to reach them even when the card they
    tapped no longer exists. A reply keeps the result attached to the
    action in a busy group, so we try that first; when only the reply
    *target* is missing (see :data:`REPLY_TARGET_LOST`) a plain send
    into the same chat is strictly better than the silence a bare
    ``suppress`` would leave behind.

    Any other rejection propagates — a malformed body is our bug, and
    re-sending the same broken text would just fail twice while hiding
    the cause. ``False`` means the chat itself is unreachable (kicked,
    blocked, deleted); the caller is expected to log that, because by
    then the money is spent and nobody saw the result.
    """
    try:
        await message.reply(text, **kwargs)
    except TelegramBadRequest as exc:
        if not any(marker in str(exc) for marker in REPLY_TARGET_LOST):
            raise
        try:
            await message.answer(text, **kwargs)
        except TelegramAPIError:
            return False
    return True


def mention_html(user_id: int, name: str | None) -> str:
    """Build an HTML inline-mention link for a Telegram user.

    The visible label is the user's display name when known, falling
    back to the numeric id so a card never renders a blank or a bare
    ``None``. The name is HTML-escaped (cards render with parse_mode
    HTML), so admin- or user-supplied names can't inject markup —
    centralising that escape here is why the same one-liner shouldn't
    be re-pasted at each call site (``/send``, ``/pvp_*``, ``/duel``).
    """
    label = html.escape(str(name)) if name else str(user_id)
    return f"<a href='tg://user?id={user_id}'>{label}</a>"


def command_args(command: CommandObject) -> str:
    """Return ``command.args`` coerced to a stripped string.

    aiogram types :attr:`CommandObject.args` as ``str | None``: bare
    ``/cmd`` parses to ``None``, ``/cmd  hello  `` parses to
    ``"hello  "`` (the dispatcher keeps the user's spacing). Six
    handlers (``ai``, ``support``, ``weather``, ``timezone``, ``top``
    × 2) opened with the same ``(command.args or "").strip()`` guard
    to collapse both axes — None-as-empty and trailing-whitespace —
    into a single ``str`` they could ``split`` / compare / forward.

    Centralising the coercion has two real wins:

    * The fragile chain (``or`` + ``strip``) lives in one named
      function; a handler that calls ``command_args(command)`` cannot
      forget the ``.strip()`` half by accident.
    * Tests and call sites read the intent ("get the user-supplied
      argument string") rather than the mechanic.

    Does NOT split — handlers that need tokens call ``.split()``
    themselves so the splitting strategy (whitespace, comma, single
    arg) stays at the call site.
    """
    return (command.args or "").strip()


def command_body(message: Message) -> str:
    """Return the text a command was typed into — body **or** photo caption.

    aiogram's ``Command`` filter matches on ``message.text or
    message.caption`` (``aiogram/filters/command.py``), so a photo whose
    caption reads ``/ban 7d спам`` really does reach ``handle_ban``. A
    handler that then splits ``message.text`` sees ``None``, parses zero
    arguments, and takes its no-argument branch — which for ``/ban`` is
    a PERMANENT ban rather than the seven days the admin asked for, with
    an empty ``reason`` in the audit log. Attaching a screenshot of the
    offence is exactly what an admin does when banning someone, so this
    is the common path, not a corner case.

    Same failure, milder shape, at two dozen other call sites: ``/mute
    10m`` silently falls back to the group default, ``/clear 5`` deletes
    ``DEFAULT_CLEAR`` messages, and the ``games`` routing predicates
    (``_is_stake_roll`` and friends) stop recognising their own form so
    the message routes to a different handler entirely.

    Handlers that receive an injected ``CommandObject`` should prefer
    :func:`command_args` — aiogram fills it from the same
    text-or-caption source. This helper is for the majority that parse
    the raw body themselves, and it returns ``""`` (never ``None``) so
    ``.split()`` at the call site stays safe.

    NOT for text *content* handlers: a wordfilter or an alias trigger
    wants the real text of a real message, and those already do their
    own explicit ``text or caption`` narrowing where they want both.
    """
    return message.text or message.caption or ""


def require_from_user(message: Message) -> User:
    """Return ``message.from_user``, asserting it is set.

    Eleven handlers used to open with the same dogma block::

        assert message.from_user is not None  # F.from_user filter guarantees
        # …followed by ``message.from_user.id`` / ``.first_name`` / …

    The assert is required because aiogram types ``Message.from_user``
    as ``User | None`` (channel posts and anonymous messages legitimately
    have no author), and mypy ``--strict`` can't read the
    ``F.from_user`` filter on the handler decorator to narrow that out.
    Centralising the assert behind a named function buys us:

    * One place to evolve the failure mode — today an ``AssertionError``;
      a future audit may want a custom exception that the error-router
      can render politely. Eleven inline asserts would need eleven edits.
    * The "why is this safe" justification lives in this docstring rather
      than as a comment repeated verbatim eleven times.
    * Call sites read ``tg_user = require_from_user(message)`` and then
      use ``tg_user.id`` / ``tg_user.first_name`` directly — removing
      the ``message.from_user.X`` repetition and the temptation to
      pepper more asserts later.

    The function intentionally returns the ``User`` rather than just
    the id: most handlers also need the display name or premium flag,
    and returning the full object lets callers narrow once.
    """
    assert message.from_user is not None, (
        "require_from_user called without an F.from_user filter guard"
    )
    return message.from_user
