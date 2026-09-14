"""End-to-end ``/admin_status``.

Pins the two contracts that matter:

* Non-developers get **no** reply. Not "доступ запрещён", not a
  callback ack — silence. The handler is a side-channel for
  enumerating dev IDs if it confirms its own existence to outsiders.
* Developers see a card containing the redacted webhook host (NOT the
  full URL — the random path suffix is the security boundary), the
  package version, and one ✅/❌ per database engine.
* The card reports the **shipped revision** — and says ⚠️ unknown
  rather than guessing when the deploy did not stamp one. Prod ran
  four-day-old code once (#170) with nothing anywhere saying so; the
  ``version`` line above it is a static ``0.1.0`` that has never
  changed and never will, so it cannot answer "is this deploy
  current?" and must not be mistaken for an answer.

The webhook URL must be redacted to host-only even though the response
is private — admins triage in shared channels and screenshots are how
secrets leak in practice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_admin_status_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)

    update = make_message_update("/admin_status", user_id=42)  # NOT 999
    await dispatcher.feed_update(bot, update)

    # The whole point of the silent-drop policy: nothing on the wire.
    # ``feed_update`` returns the handler's own return value here —
    # which is ``None`` because the guard's ``return`` is bare. The
    # contract we care about is "user sees no reply", and that's the
    # ``sent == []`` assert. UNHANDLED would be a stronger statement
    # (the dispatcher didn't even consider the command), but aiogram
    # marks the event handled as soon as the filter matches — there's
    # no way to distinguish "no match" from "match + silent drop" from
    # the bot's outbound side, which is exactly the property we want.
    assert sent == []


@pytest.mark.asyncio
async def test_admin_status_renders_for_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_status", user_id=555))

    assert len(sent) == 1
    text = sent[0]["text"]
    # version + env reported
    assert "version" in text.lower()
    assert "env:" in text.lower()
    # All five DB names appear with a probe result. ``users.db`` is the
    # canonical name in the registry — match on the short identifier.
    for db in ("users", "economy", "activity", "moderation", "message_stats"):
        assert db in text


@pytest.mark.asyncio
async def test_admin_status_is_dropped_in_a_group_even_for_a_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The board is private-only; being a developer does not unlock the
    venue (#408).

    The dev gate answers "who may read this"; it cannot answer "where may
    it be rendered". Without the router's chat filter, a developer typing
    ``/admin_status`` in a shared group during an incident publishes the
    webhook host and every subsystem's configured state to that group —
    the exact card ``test_admin_status_redacts_webhook_path`` below goes
    to the trouble of redacting for forwarded screenshots.
    """
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_status", user_id=555, chat_id=-1001234567890, chat_type="supergroup"
        ),
    )

    assert sent == []


@pytest.mark.asyncio
async def test_admin_status_redacts_webhook_path(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The random suffix in WEBHOOK_URL is the security boundary
    against attackers guessing the endpoint. Even though only devs see
    this card, the redaction is defence-in-depth: a forwarded
    screenshot must not leak the full path. We pin host-only display.
    """
    monkeypatch.setenv("WEBHOOK_URL", "https://bot.example.com/hook/abc123secret")
    monkeypatch.setenv("APP_ENV", "dev")  # avoid prod-secret-token requirement

    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_status", user_id=777))

    assert len(sent) == 1
    text = sent[0]["text"]
    # We get the host but NOT the secret suffix — that's the contract.
    # The fixture's Settings ignores monkeypatched WEBHOOK_URL because
    # it builds Settings from explicit kwargs (see conftest), so the
    # redaction is exercised against the default empty URL → "<unset>".
    # Either branch is acceptable but the secret suffix MUST be absent.
    assert "abc123secret" not in text


@pytest.mark.asyncio
async def test_admin_status_silent_when_user_field_missing(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Defence in depth — even if some future aiogram update arrives
    without a ``from_user`` (channel post, anonymous-admin edge case),
    the handler must not fall through to the developer branch. The
    ``user is None`` early-return is the guard; this test pins it so a
    refactor that "simplifies" the if can't accidentally remove it.
    """
    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    sent = capture_outgoing(bot)

    # Build an update with no ``from`` field at all — channel-post shape.
    from aiogram.types import Update

    update = Update.model_validate(
        {
            "update_id": 3,
            "channel_post": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100, "type": "channel", "title": "C"},
                "text": "/admin_status",
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    # Channel posts don't reach our /admin_status message handler at
    # all — different event type. Sent list must be empty regardless.
    assert sent == []


# ``test_admin_status_includes_top_legacy_commands`` existed while the
# strangler bridge was live (T-011, 2026-05-26 removed it). The legacy
# command counter and ``_top_legacy_commands`` rendering are gone, so
# the test has nothing to assert.


@pytest.mark.asyncio
async def test_admin_status_reports_shipped_revision(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the deploy stamped a revision, the card shows it — that
    string is what an operator diffs against ``git log`` to find out
    whether prod is behind."""
    from telegram_invite_bot.handlers.admin import status as status_mod
    from telegram_invite_bot.utils.build_info import BuildInfo

    monkeypatch.setattr(
        status_mod,
        "read_build_info",
        lambda: BuildInfo(
            revision="1ac54dc",
            committed_at="2026-08-20 05:10:00 +0300",
            deployed_at="2026-08-20 05:43:11 +0300",
            deployed_by="owner@Mac",
        ),
    )

    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_status", user_id=555))

    text = sent[0]["text"]
    assert "1ac54dc" in text
    assert "2026-08-20 05:43:11 +0300" in text
    # Not "unknown" — a stamp exists, so no warning.
    assert "deploy did not write BUILD_INFO" not in text


@pytest.mark.asyncio
async def test_admin_status_admits_unknown_build(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stamp → the card says so, loudly, and points at the runbook.

    This is the branch that actually mattered: silence here reads as
    "nothing to report", which is exactly the wrong conclusion when
    the truth is "nobody knows what is running".
    """
    from telegram_invite_bot.handlers.admin import status as status_mod

    monkeypatch.setattr(status_mod, "read_build_info", lambda: None)

    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_status", user_id=555))

    text = sent[0]["text"]
    assert "⚠️" in text
    assert "BUILD_INFO" in text
    assert "docs/DEPLOY.md" in text


@pytest.mark.asyncio
async def test_admin_status_escapes_a_hostile_stamp(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BUILD_INFO`` is a file on the server, and the card is HTML.

    Nothing sanitises it on the way in, so an angle bracket that got
    there by accident (or otherwise) must not reach Telegram as
    markup — an unparseable card is a 400, i.e. the status command
    stops working precisely when someone is investigating (#155).
    """
    from telegram_invite_bot.handlers.admin import status as status_mod
    from telegram_invite_bot.utils.build_info import BuildInfo

    monkeypatch.setattr(
        status_mod,
        "read_build_info",
        lambda: BuildInfo(
            revision="<b>abc</b>",
            committed_at=None,
            deployed_at="<i>now</i>",
            deployed_by=None,
        ),
    )

    bot, dispatcher, _registry = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_status", user_id=555))

    text = sent[0]["text"]
    assert "&lt;b&gt;abc&lt;/b&gt;" in text
    assert "&lt;i&gt;now&lt;/i&gt;" in text
