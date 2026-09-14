"""Effective payment-secret resolution (T-027).

The DI container builds singletons (``Bot``, engines, the resolved
``Settings``) at startup, so a value that only lived in ``.env`` cannot
change without a redeploy. To let a developer set ``CRYPTO_PAY_TOKEN``
from the in-bot admin panel and have it take effect immediately, the
payment code resolves the *effective* token at call time:

* the ``economy.runtime_secrets`` row (set via ``/set_crypto_token``)
  wins when present, else
* the ``.env``-loaded ``PaymentsConfig.crypto_api_secret`` is used.

Both the inbound webhook (signature verification) and the outbound
client (``transfer`` / ``createInvoice``) go through this single
resolver, so a runtime override feeds both halves consistently —
otherwise a dev who set the token via the panel would still see inbound
signature checks fail against an unset env var.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram_invite_bot.config.logging import register_runtime_secret
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.runtime_secrets_repo import RuntimeSecretsRepo

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

#: Runtime-secrets key for the Crypto Pay app token. Shared by the
#: resolver and the admin handler so the string lives in one place.
CRYPTO_PAY_TOKEN_KEY = "CRYPTO_PAY_TOKEN"  # noqa: S105 — a key NAME, not a secret


async def resolve_crypto_token(*, registry: EngineRegistry, settings: Settings) -> str | None:
    """Return the effective Crypto Pay token, or ``None`` if unconfigured.

    Runtime override (``economy.runtime_secrets``) first, then the
    ``.env``-loaded ``PaymentsConfig`` value. ``None`` means neither is
    set — callers degrade to "service not configured" (503 / a friendly
    user message), never crash.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        db_value = await RuntimeSecretsRepo(session).get(CRYPTO_PAY_TOKEN_KEY)
    if db_value:
        # The log redactor learns its secrets by reflecting over Settings
        # ONCE at startup, so a runtime override is invisible to it: on
        # prod the Crypto Pay token lives only here, in the DB, and was
        # the one payment secret with no redaction at all (#1367).
        # Registering on the read path covers every process that resolves
        # it, including one that started before the token was ever set.
        register_runtime_secret(db_value)
        return db_value
    secret = settings.payments.crypto_api_secret
    return secret.get_secret_value() if secret is not None else None
