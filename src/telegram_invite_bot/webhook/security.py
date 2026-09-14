"""Telegram webhook secret-token verification.

Telegram includes ``X-Telegram-Bot-Api-Secret-Token`` on every update if a
secret was supplied to ``setWebhook``. Comparing it with the configured
value closes the MITM hole that the legacy ``main.py`` left open until
``WEBHOOK_SECRET_TOKEN`` was added — see plan, Stage 3.

If no secret is configured (``settings.webhook.secret_token is None``)
the check is a no-op. Production deployments MUST set it.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, Request, status

from telegram_invite_bot.config.settings import Settings

logger = logging.getLogger(__name__)

_HEADER = "X-Telegram-Bot-Api-Secret-Token"

#: One reply for every rejection. The caller is unauthenticated by
#: definition at this point, so the response must not tell them WHICH
#: guard tripped: "misconfigured" would reveal that the deployment's
#: secret is empty — i.e. that this layer is currently authenticating
#: nobody. The operator learns the difference from the logs below.
_DENIED = "forbidden"


def verify_secret_token(request: Request, settings: Settings) -> None:
    expected = settings.webhook.secret_token
    if expected is None:
        return
    expected_value = expected.get_secret_value()
    # Defence in depth (SEC audit): an empty configured secret must NEVER
    # authenticate a caller. ``Settings`` already normalises an empty
    # ``WEBHOOK_SECRET_TOKEN`` to ``None`` (→ the no-op path above), but if
    # an empty SecretStr ever reaches here, ``compare_digest("", "")``
    # would return True for a request with NO header — a silent forge/MITM
    # bypass. Reject instead of comparing against the empty string.
    if not expected_value:
        logger.error("webhook secret token is configured but empty; denying all")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)
    # Symmetric to the header check below, and for a worse failure: a
    # CONFIGURED secret that is not ASCII makes ``compare_digest`` raise
    # ``TypeError`` on EVERY update, not only on a hostile one. The bot
    # would answer 500 to Telegram forever while looking configured.
    # ``Settings._webhook_secret_token_charset`` now refuses such a value
    # at settings load, so reaching here means something bypassed Settings
    # — same defence in depth as the empty check above.
    if not expected_value.isascii():
        logger.error(
            "webhook secret token contains non-ASCII characters; denying all "
            "(compare_digest would raise TypeError on every update)"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)
    provided = request.headers.get(_HEADER, "")
    # Reject non-ASCII BEFORE comparing. Starlette decodes header values
    # with latin-1, so any byte in 0x80-0xFF arrives here as a non-ASCII
    # ``str`` — and ``compare_digest`` raises ``TypeError`` on those
    # rather than returning False. Nothing registers an ``Exception``
    # handler on this app, so that TypeError escaped the route as a 500
    # with a full uvicorn traceback: an anonymous caller could turn one
    # byte into unbounded journald writes on a small host that has no
    # rate limit, and do it INVISIBLY — on that path neither
    # ``UPDATES_TOTAL`` nor the rejection log below is ever reached.
    # Telegram's secret is restricted to ``A-Za-z0-9_-``, so a non-ASCII
    # header can only ever be a mismatch; saying so up front costs one
    # scan of attacker-supplied bytes and leaks nothing about the secret.
    # ``compare_digest`` stays constant-time for the real comparison.
    if not provided.isascii() or not secrets.compare_digest(provided, expected_value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)
