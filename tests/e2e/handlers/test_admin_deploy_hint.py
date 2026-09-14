"""End-to-end ``/admin_deploy``.

Pins:

* Non-developer → silent drop (existence is not a side-channel for
  enumerating dev IDs).
* Developer in private → static reminder card renders.
* Card explicitly says "not a Telegram command" — the whole point of
  this handler is that a deploy MUST NOT happen here, and that
  promise is operator-visible content, not buried in code comments.
* Card names the live deploy path and names the dead ones as dead.
  This card is the only in-bot documentation of how to ship, so its
  being out of date is indistinguishable, to its reader, from its
  being right.
* Group invocation → router-level private filter rejects (UNHANDLED).
* The bare ``/deploy`` a developer actually types answers too (#2003).
  This module's reason for existing is that the deploy command must
  produce a card rather than silence: "without this handler they see
  'unknown command' and then guess (wrong) that the deploy is wedged".
  The port renamed it to ``/admin_deploy`` and left the short name to
  the strangler bridge, which T-011 then removed — so the name an
  operator reaches for first stopped matching anything at all, and the
  module's documented failure mode became its actual behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_deploy", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_for_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_deploy", user_id=555, chat_type="private")
    )
    assert len(sent) == 1
    text = sent[0]["text"]
    # Card must surface the safety promise — the WHOLE point of this
    # command is that it does NOT initiate a deploy. If a future
    # refactor drops that line, an operator could reasonably guess
    # the bot is running the deploy and just being quiet about it.
    assert "not a Telegram command" in text
    # Shell hint must be present — operator opens this card to find
    # out HOW to deploy. Without the hint the card is just a refusal.
    assert "./scripts/deploy.sh" in text
    assert "docs/DEPLOY.md" in text
    # The card must not resurrect a route that goes nowhere. Each of
    # these named a path that does not exist any more (#170): there is
    # no Makefile, the repo-root scripts target a server that is gone,
    # and the blue/green window was removed in #146. A stale
    # instruction here is read by whoever is least able to notice it
    # is stale — someone who opened this card because they were unsure.
    for dead in ("make deploy", "blue/green"):
        assert dead not in text
    # The legacy scripts DO get named — but only inside the warning,
    # because they are one tab-completion away in the repo root and
    # silence about them is not the same as a fence around them.
    for legacy in ("deploy_to_vps.sh", "deploy_to_server.py", "CLI_DEPLOY.txt"):
        assert legacy in text
    assert "Do not run them" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_deploy",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


@pytest.mark.asyncio
async def test_the_legacy_short_name_still_answers(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """``/deploy`` — the name the operator's fingers already know.

    Registered on the same handler behind the same developer gate, so
    it adds no surface: a non-developer typing either spelling gets
    the same silence. What it restores is the one case this module was
    written for — a developer who types the obvious name and must not
    be met with nothing.
    """
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
        schemas=[UsersBase],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/deploy", user_id=555, chat_type="private")
    )
    assert len(sent) == 1, "/deploy matched no handler — the update was dropped in silence"
    assert "not a Telegram command" in sent[0]["text"]


@pytest.mark.asyncio
async def test_the_legacy_short_name_is_no_wider_than_the_new_one(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The alias must not become an enumeration channel of its own."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
        schemas=[UsersBase],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/deploy", user_id=42, chat_type="private")
    )
    assert sent == []
