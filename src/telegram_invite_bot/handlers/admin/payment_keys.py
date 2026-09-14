"""``/payment_keys`` admin panel — set the Crypto Pay token at runtime (T-027).

The DI container builds the ``Bot``, the engines, and the resolved
``Settings`` as startup singletons, so a ``CRYPTO_PAY_TOKEN`` that only
lives in ``.env`` cannot change without a redeploy. This panel lets a
developer set the token from inside the bot and have it take effect on
the *next* payment call — the resolver
(``services/payments/secret_resolver.resolve_crypto_token``) reads the
``economy.runtime_secrets`` row at call time, preferring it over the
``.env`` value.

Three commands, all dev-only and private-only (same posture as every
other ``/admin_*`` handler — silent-drop for non-devs so existence
never enumerates dev IDs):

* ``/payment_keys`` — status card: is the token set, from which source
  (runtime override vs ``.env``), and a masked last-4 fingerprint so the
  operator can confirm *which* token is live without revealing it.
* ``/set_crypto_token <token>`` — upsert the runtime override. The
  message carrying the secret is deleted immediately so the token does
  not linger in chat history (and never appears in a group — the router
  is private-only). Light shape validation rejects the obvious paste
  mistakes (empty / no ``:`` separator).
* ``/clear_crypto_token`` — drop the runtime override, reverting to the
  ``.env`` fallback.

The token value itself is NEVER echoed back — not in the status card,
not in the set/clear confirmations, not in logs (only the masked last-4
and the colon-prefix app-id, both non-secret).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from loguru import logger

from telegram_invite_bot.config.logging import register_runtime_secret
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.runtime_secrets_repo import RuntimeSecretsRepo
from telegram_invite_bot.services.payments.secret_resolver import CRYPTO_PAY_TOKEN_KEY

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="handlers.admin.payment_keys")


def _mask(token: str) -> str:
    """Non-secret fingerprint of a Crypto Pay token.

    A Crypto Pay token is ``<app_id>:<secret>``. The numeric app-id
    prefix is not sensitive (it identifies the app, not authorises it),
    so we surface it whole; the secret half is reduced to its last 4
    chars. Tokens shorter than 4 chars (only seen via a bad paste that
    slipped validation) collapse to ``****`` so we never reveal a short
    secret in full.
    """
    app_id, sep, secret = token.partition(":")
    last4 = secret[-4:] if len(secret) >= 4 else ""
    tail = f"…{last4}" if last4 else "****"
    return f"{app_id}{sep}{tail}" if sep else tail


def _looks_like_token(token: str) -> bool:
    """Reject the obvious paste mistakes before we store the value.

    A Crypto Pay app token is ``<digits>:<secret>``. We require a colon
    with a non-empty numeric-ish prefix and a non-empty secret. This is
    a guardrail against storing a blank/half-copied value — NOT a
    cryptographic check; the real verdict is the API's 401 on first use.
    """
    app_id, sep, secret = token.partition(":")
    return bool(sep) and bool(app_id.strip()) and bool(secret.strip())


async def _get_runtime_token(registry: EngineRegistry) -> str | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        return await RuntimeSecretsRepo(session).get(CRYPTO_PAY_TOKEN_KEY)


async def handle_payment_keys(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /payment_keys; silently dropped"
        )
        return

    runtime_token = await _get_runtime_token(registry)
    env_secret = settings.payments.crypto_api_secret
    env_token = env_secret.get_secret_value() if env_secret is not None else None

    lines = ["🔑 <b>Payment keys</b>", ""]
    if runtime_token:
        lines.append("• <code>CRYPTO_PAY_TOKEN</code>: <b>set</b> (runtime override)")
        lines.append(f"  ↳ <code>{_mask(runtime_token)}</code>")
        if env_token:
            lines.append("  ↳ <i>.env fallback present but overridden</i>")
    elif env_token:
        lines.append("• <code>CRYPTO_PAY_TOKEN</code>: <b>set</b> (.env)")
        lines.append(f"  ↳ <code>{_mask(env_token)}</code>")
    else:
        lines.append("• <code>CRYPTO_PAY_TOKEN</code>: <b>not set</b>")
        lines.append("  ↳ set it with <code>/set_crypto_token &lt;token&gt;</code>")

    lines.append("")
    lines.append(
        "Override with <code>/set_crypto_token &lt;token&gt;</code>, "
        "revert with <code>/clear_crypto_token</code>."
    )
    await message.answer("\n".join(lines))
    log.bind(
        user_id=user.id,
        runtime_set=bool(runtime_token),
        env_set=bool(env_token),
    ).info("/payment_keys rendered")


async def handle_set_crypto_token(
    message: Message,
    command: CommandObject,
    settings: Settings,
    registry: EngineRegistry,
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /set_crypto_token; silently dropped"
        )
        return

    # Delete the message carrying the secret FIRST — before any await
    # that could fail — so the token does not linger in chat history
    # even if the upsert below errors. best-effort: a failed delete
    # (e.g. message already gone) must not abort the set.
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 — delete is best-effort cleanup
        log.bind(user_id=user.id).warning("could not delete secret-bearing message: {}", exc)

    token = (command.args or "").strip()
    if not token:
        await message.answer(
            "Usage: <code>/set_crypto_token &lt;token&gt;</code>\n"
            "Get the token from @CryptoBot → Crypto Pay → My Apps."
        )
        return
    if not _looks_like_token(token):
        await message.answer(
            "❌ That doesn't look like a Crypto Pay token "
            "(expected <code>&lt;app_id&gt;:&lt;secret&gt;</code>). Not stored."
        )
        log.bind(user_id=user.id).warning("rejected malformed CRYPTO_PAY_TOKEN")
        return

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        await RuntimeSecretsRepo(session).upsert(CRYPTO_PAY_TOKEN_KEY, token, updated_by=user.id)
        await session.commit()
    # Redact it from this process's logs immediately rather than waiting
    # for the first payment call to resolve it (#1367) — the window in
    # between is exactly when an operator is most likely to be tailing
    # the journal to see whether the new token works.
    register_runtime_secret(token)

    await message.answer(
        "✅ <code>CRYPTO_PAY_TOKEN</code> set (runtime override).\n"
        f"↳ <code>{_mask(token)}</code>\n"
        "Takes effect on the next payment call. "
        "<code>/clear_crypto_token</code> reverts to .env."
    )
    log.bind(user_id=user.id, fingerprint=_mask(token)).info(
        "CRYPTO_PAY_TOKEN runtime override set"
    )


async def handle_clear_crypto_token(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /clear_crypto_token; silently dropped"
        )
        return

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        existed = await RuntimeSecretsRepo(session).clear(CRYPTO_PAY_TOKEN_KEY)
        await session.commit()

    env_secret = settings.payments.crypto_api_secret
    if existed:
        fallback = (
            "Reverted to the <code>.env</code> value."
            if env_secret is not None
            else "No <code>.env</code> fallback — Crypto Pay is now unconfigured."
        )
        await message.answer(f"✅ Runtime <code>CRYPTO_PAY_TOKEN</code> cleared. {fallback}")
    else:
        await message.answer("ℹ️ No runtime <code>CRYPTO_PAY_TOKEN</code> override was set.")
    log.bind(user_id=user.id, existed=existed).info("CRYPTO_PAY_TOKEN runtime override cleared")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only at the router level — these commands carry/echo
    payment-credential material; they must never run in a group.
    """
    router = Router(name="admin.payment_keys")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _keys(message: Message) -> None:
        await handle_payment_keys(message, settings, registry)

    async def _set(message: Message, command: CommandObject) -> None:
        await handle_set_crypto_token(message, command, settings, registry)

    async def _clear(message: Message) -> None:
        await handle_clear_crypto_token(message, settings, registry)

    router.message.register(_keys, Command("payment_keys", ignore_case=True))
    router.message.register(_set, Command("set_crypto_token", ignore_case=True))
    router.message.register(_clear, Command("clear_crypto_token", ignore_case=True))
    return router
