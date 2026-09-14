"""Last-resort fallback when Telegram refuses to parse our markup.

Unlike everything else in this package this is a **session request**
middleware, not a dispatcher middleware: it wraps outgoing Bot API
calls rather than incoming updates. It lives here because "middleware"
is where a reader looks for it, and the module name carries the
``api_`` prefix to keep the distinction visible at the import site.

Why it exists
-------------
The bot runs with ``parse_mode=HTML`` as a bot-wide default. Telegram
parses a message whole or not at all: one unrecognised tag anywhere in
the body — ``<unset>`` from a redaction sentinel, ``<locals>`` from a
qualname, ``<any>`` from a warnings filter, an angle bracket inside a
thread name or an env-var preview — and the entire API call fails with
``400 Bad Request: can't parse entities``. The user (or the developer
staring at an ``/admin_*`` card) then gets **nothing at all**, which is
strictly the worst outcome: no content, and no hint about why.

Every such case found so far has been fixed at the source by escaping
the interpolated value, and that remains the correct fix — this
middleware is a net, not a substitute. What it changes is the failure
mode: instead of silence, the message goes out once more with parse
mode disabled, so the recipient sees the text with literal tags in it,
and the journal carries an ERROR naming the method and Telegram's own
complaint so the underlying renderer bug still gets found and fixed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from loguru import logger

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.client.session.middlewares.base import NextRequestMiddlewareType
    from aiogram.methods import Response, TelegramMethod
    from aiogram.methods.base import TelegramType

#: Substrings Telegram uses when it is the *markup* it could not read.
#: Matched case-insensitively against the error text. Anything else —
#: a blocked user, a stale message id, a bad chat id — is a different
#: failure and must keep propagating untouched.
_PARSE_FAILURE_MARKERS = (
    "can't parse entities",
    "can't parse message text",
    "unsupported start tag",
    "unclosed start tag",
)


def _is_parse_failure(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PARSE_FAILURE_MARKERS)


class ParseModeFallbackMiddleware(BaseRequestMiddleware):
    """Resend once with parse mode off when the markup is rejected."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if not _is_parse_failure(exc):
                raise
            if "parse_mode" not in type(method).model_fields:
                # Nothing to turn off — the complaint came from a method
                # that carries entities but no parse mode, and resending
                # it unchanged would only fail identically.
                raise
            logger.bind(
                component="telegram_api",
                method=type(method).__name__,
            ).error(
                "Telegram rejected the markup ({detail}); resending as plain text. "
                "This is a renderer bug — the offending value needs escaping at "
                "its source.",
                detail=str(exc),
            )
            # ``model_copy`` rather than mutation: methods are pydantic
            # models the caller may still hold a reference to, and a
            # silently de-formatted object handed back to caller code
            # would be a much nastier surprise than the retry itself.
            update: dict[str, Any] = {"parse_mode": None}
            plain = method.model_copy(update=update)
            return await make_request(bot, plain)
