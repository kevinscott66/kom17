"""Name the renderer when an outgoing message is too long to deliver.

A session **request** middleware, like
:mod:`~telegram_invite_bot.middlewares.api_parse_mode_fallback` — see
that module for why the ``api_`` prefix is on the name.

Why it exists
-------------
Telegram caps message text at 4096 characters *after* entity parsing,
measured in UTF-16 code units, and rejects anything longer with a 400.
The recipient gets nothing, and what lands in the journal is a generic
``TelegramBadRequest`` that says the message is too long without saying
which card produced it or by how much.

That is not a hypothetical failure mode. ``/admin_help`` (8 659
characters) and ``/admin_routes`` (11 817) had both been over the
ceiling for the entire life of the deployment: every invocation was a
silent 400, and neither was noticed, because nothing in the logs
connected "400" to "this renderer". Both are paginated now and pinned
by tests, but tests can only measure the cards a test can drive — a
list that grows with user data (filter words, warnings, orders) crosses
the ceiling on someone's account, not in CI.

4096 is only the message ceiling, and this guard's first version
applied it to every body it inspected. Telegram is stricter than that
on three of the four surfaces: a media caption is capped at 1024, a
poll question at 300, and a callback toast at 200. Measuring those
against 4096 left them unguarded in exactly the range where they
actually break — a 2 000-character caption read as fine here and was
still rejected by Telegram. The limit is now looked up per field, and
per method where the field name is ambiguous: ``AnswerCallbackQuery``
calls its body ``text`` too, and gets a twentieth of a message.

So this measures every outgoing body before it leaves, and when it is
over that body's own limit it logs an ERROR carrying the method, the
field, the measured length, the limit it broke and the opening
characters of the body. The call still fails:
truncating here would turn a loud failure into a message that looks
complete and is not, and the honest fix is always pagination at the
renderer (:func:`telegram_invite_bot.utils.render.paginate_lines`).
What changes is that the journal now names the culprit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from loguru import logger

from telegram_invite_bot.utils.render import (
    TELEGRAM_CALLBACK_ANSWER_LIMIT,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_POLL_QUESTION_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    parsed_length,
    utf16_length,
)

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
    from aiogram.methods import Response, TelegramMethod
    from aiogram.methods.base import TelegramType

logger = logger.bind(component="middlewares.api_length_guard")

#: Fields that carry user-visible body text on the Bot API methods we
#: send, each with the ceiling Telegram enforces for it. Only ``text``
#: on a message gets the famous 4096 — a media caption gets a quarter
#: of that and a poll question a fourteenth, so measuring them all
#: against 4096 (as this guard first did) leaves the two tightest
#: surfaces effectively unguarded.
_FIELD_LIMITS = {
    "text": TELEGRAM_TEXT_LIMIT,
    "caption": TELEGRAM_CAPTION_LIMIT,
    "question": TELEGRAM_POLL_QUESTION_LIMIT,
}

#: Where the field name alone is not enough. ``AnswerCallbackQuery``
#: also calls its body ``text``, but a toast is capped at 200 — twenty
#: times tighter than a message, and the surface most likely to drift
#: past its cap unnoticed because nothing about it looks like a "card".
_METHOD_FIELD_LIMITS = {
    ("AnswerCallbackQuery", "text"): TELEGRAM_CALLBACK_ANSWER_LIMIT,
}

#: How much of the offending body to quote in the log line. Enough to
#: recognise the card at a glance, short enough not to replay the whole
#: over-long message into the journal.
_EXCERPT = 120


def _measured_length(method: TelegramMethod[TelegramType], value: str) -> int:
    """Length as Telegram will count it for this particular call.

    Under the bot-wide HTML default the tags are markup and do not
    count, so the parsed length is the real one. A call that opts out
    with an explicit ``parse_mode=None`` sends those same characters
    literally, and then every one of them counts. A method that has no
    ``parse_mode`` field at all — the callback toast, whose body
    Telegram never parses — reads as ``None`` here too, and lands on
    the same branch for the same reason.
    """
    if getattr(method, "parse_mode", None) is None:
        return utf16_length(value)
    return parsed_length(value)


class LengthGuardMiddleware(BaseRequestMiddleware):
    """Log an ERROR naming the renderer behind an over-length body."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        method_name = type(method).__name__
        for field, default_limit in _FIELD_LIMITS.items():
            value = getattr(method, field, None)
            if not isinstance(value, str):
                continue
            limit = _METHOD_FIELD_LIMITS.get((method_name, field), default_limit)
            length = _measured_length(method, value)
            if length <= limit:
                continue
            logger.bind(
                component="telegram_api",
                method=method_name,
                field=field,
                length=length,
                limit=limit,
            ).error(
                "Outgoing {field} on {method} is {length} characters, over "
                "Telegram's {limit}; the call will be rejected and the "
                "recipient will get nothing. This is a renderer bug — the "
                "body needs paginate_lines() or a tighter cap. Body starts: "
                "{excerpt!r}",
                field=field,
                method=method_name,
                length=length,
                limit=limit,
                excerpt=value[:_EXCERPT],
            )
        return await make_request(bot, method)
