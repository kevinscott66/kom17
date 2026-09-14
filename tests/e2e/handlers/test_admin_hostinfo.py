"""End-to-end ``/admin_hostinfo``.

Pins:

* Non-developer → silent drop.
* Card renders the load-bearing identity rows: node, fqdn, system,
  release, version, machine. Any one of these going missing would
  silently degrade the blue/green deploy verification story in the
  module docstring.
* Group invocation → router-level private filter rejects.
* node/fqdn disagreement renders a soft ⚠ — the marker is the
  load-bearing signal for /etc/hostname vs resolver drift; a
  cleanup pass that drops the marker would hide a real
  configuration regression.
* ``.localdomain`` fqdn fallback does NOT trip the marker — the
  cry-wolf case is what makes operators learn to ignore warnings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.hostinfo import _HostSnapshot, _render
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
        bot,
        make_message_update("/admin_hostinfo", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_lists_identity_rows(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_hostinfo", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Host identity" in text
    for label in ("node:", "fqdn:", "system:", "release:", "version:", "machine:"):
        assert label in text, f"{label} missing from /admin_hostinfo output"


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
            "/admin_hostinfo",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_node_fqdn_disagreement_surfaces_warning() -> None:
    """A real prod disagreement (different hostname vs FQDN) is the
    config drift this card catches. Without the marker the operator
    would have to compare the two strings character-by-character on
    every snapshot."""
    snap = _HostSnapshot(
        node="bot01",
        fqdn="proxy.example.com",
        system="Linux",
        release="6.1.0",
        version="#1 SMP",
        machine="x86_64",
        processor="",
    )
    rendered = _render(snap)
    assert "⚠" in rendered
    assert "drift" in rendered


def test_render_localdomain_fallback_no_warning() -> None:
    """``getfqdn`` falling back to ``<node>.localdomain`` on a host
    with no domain is genuinely benign. The card MUST NOT warn on
    that case — cry-wolf trains operators to ignore the marker
    when a real drift later appears."""
    snap = _HostSnapshot(
        node="bot01",
        fqdn="bot01.localdomain",
        system="Linux",
        release="6.1.0",
        version="#1 SMP",
        machine="x86_64",
        processor="",
    )
    rendered = _render(snap)
    assert "⚠" not in rendered


def test_render_matching_node_fqdn_no_warning() -> None:
    """Healthy state: node == fqdn. Must not carry the marker."""
    snap = _HostSnapshot(
        node="bot01",
        fqdn="bot01",
        system="Linux",
        release="6.1.0",
        version="#1 SMP",
        machine="x86_64",
        processor="",
    )
    rendered = _render(snap)
    assert "⚠" not in rendered
