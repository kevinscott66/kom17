"""End-to-end ``/payment_keys`` admin panel (T-027).

Pins:

* Non-developer → silent drop on every command.
* ``/payment_keys`` with nothing configured → "not set".
* ``/set_crypto_token <token>`` → stores a runtime override, echoes
  only the masked fingerprint (never the full secret), and the message
  carrying the secret is deleted (best-effort).
* ``/payment_keys`` after set → "runtime override" + masked fingerprint.
* ``/set_crypto_token`` with no args → usage hint, nothing stored.
* ``/set_crypto_token`` with a malformed value → rejected, nothing stored.
* ``/clear_crypto_token`` → drops the override; a second clear is a no-op.
* Group invocation → router-level private filter rejects (UNHANDLED).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.runtime_secrets_repo import RuntimeSecretsRepo
from telegram_invite_bot.services.payments.secret_resolver import CRYPTO_PAY_TOKEN_KEY
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_DEV = 555
_TOKEN = "12345:AAaBBbCCtoken"


async def _stored(registry: EngineRegistry) -> str | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        return await RuntimeSecretsRepo(session).get(CRYPTO_PAY_TOKEN_KEY)


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    for cmd in ("/payment_keys", "/set_crypto_token 12345:x", "/clear_crypto_token"):
        await dispatcher.feed_update(bot, make_message_update(cmd, user_id=42, chat_type="private"))
    assert sent == []


@pytest.mark.asyncio
async def test_keys_not_set(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/payment_keys", user_id=_DEV, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Payment keys" in text
    assert "not set" in text


@pytest.mark.asyncio
async def test_set_then_status(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(f"/set_crypto_token {_TOKEN}", user_id=_DEV, chat_type="private"),
    )
    # Stored in the runtime-secrets table.
    assert await _stored(registry) == _TOKEN
    # Confirmation echoes only the masked fingerprint, never the secret.
    confirm = sent[-1]["text"]
    assert "set" in confirm
    assert "12345:…oken" in confirm
    assert "AAaBBbCCtoken" not in confirm

    # Status card now reports the runtime override + mask, not the secret.
    await dispatcher.feed_update(
        bot, make_message_update("/payment_keys", user_id=_DEV, chat_type="private")
    )
    card = sent[-1]["text"]
    assert "runtime override" in card
    assert "12345:…oken" in card
    assert "AAaBBbCCtoken" not in card


@pytest.mark.asyncio
async def test_set_no_args(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/set_crypto_token", user_id=_DEV, chat_type="private")
    )
    assert "Usage" in sent[-1]["text"]
    assert await _stored(registry) is None


@pytest.mark.asyncio
async def test_set_malformed_rejected(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    # No colon separator → not a Crypto Pay token shape.
    await dispatcher.feed_update(
        bot,
        make_message_update("/set_crypto_token notatoken", user_id=_DEV, chat_type="private"),
    )
    assert "doesn't look like" in sent[-1]["text"]
    assert await _stored(registry) is None


@pytest.mark.asyncio
async def test_clear(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(f"/set_crypto_token {_TOKEN}", user_id=_DEV, chat_type="private"),
    )
    assert await _stored(registry) == _TOKEN

    await dispatcher.feed_update(
        bot, make_message_update("/clear_crypto_token", user_id=_DEV, chat_type="private")
    )
    assert "cleared" in sent[-1]["text"]
    assert await _stored(registry) is None

    # Second clear is a no-op (nothing to revert).
    await dispatcher.feed_update(
        bot, make_message_update("/clear_crypto_token", user_id=_DEV, chat_type="private")
    )
    assert "No runtime" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, assert_no_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    assert_no_outgoing(bot, "group /payment_keys must fall through")
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/payment_keys", user_id=_DEV, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
