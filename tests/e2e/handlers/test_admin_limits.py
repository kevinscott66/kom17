"""End-to-end ``/admin_limits``.

Pins:

* Non-developer → silent drop.
* Live card renders + lists multiple rlimits.
* Card has NO ⚠ markers at all — pure information by design.
* RLIM_INFINITY renders as "unlimited" (not raw sentinel).
* Unavailable rlimit (platform-missing) renders as "n/a" inline,
  not skipped.
* _capture returns at least the rlimits the test platform
  actually exposes (NOFILE is universal).
* References /admin_fdlimit so the operator knows about the
  curated subset.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

import resource
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.limits import (
    _capture,
    _fmt_value,
    _LimitRow,
    _render,
)
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
        bot, make_message_update("/admin_limits", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_limits", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Resource limits" in text
    # NOFILE is universal — must appear on any host the tests
    # could plausibly run on.
    assert "NOFILE" in text


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
        make_message_update("/admin_limits", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_card_has_no_warnings() -> None:
    """No ⚠ on this card by design — a configured limit is
    operator intent, not a problem. This is the cry-wolf
    prevention story for /admin_limits: pinned explicitly so a
    future tightening doesn't accidentally add a marker."""
    rendered = _render(_capture())
    # The phrase "No ⚠ markers" in the footer disclaimer is
    # the only place ⚠ may appear; partition there and assert
    # the body has none.
    head, _, _ = rendered.partition("No ⚠")
    assert "⚠" not in head


def test_references_fdlimit() -> None:
    """The card must point at /admin_fdlimit — the operator
    needs to know about the curated subset so they don't
    over-rely on this exhaustive one for capacity planning."""
    rendered = _render(_capture())
    assert "/admin_fdlimit" in rendered


def test_fmt_unlimited() -> None:
    """RLIM_INFINITY → "unlimited". The sentinel value is
    user-hostile (a giant 64-bit number); render the intent."""
    assert _fmt_value(resource.RLIM_INFINITY) == "unlimited"


def test_fmt_n_a_for_none() -> None:
    """None → "n/a". Platform doesn't expose the rlimit at all
    (RLIMIT_MSGQUEUE on macOS, etc.); operator should see the
    absence rather than wonder if the read failed silently."""
    assert _fmt_value(None) == "n/a"


def test_fmt_real_value_with_thousands_separator() -> None:
    """Real values use the , separator so big NOFILE caps
    (1,048,576) stay readable."""
    assert _fmt_value(1_048_576) == "1,048,576"


def test_capture_yields_nofile() -> None:
    """NOFILE is universal across Linux + macOS + BSD. _capture
    must produce a row for it on any test host."""
    rows = _capture()
    nofile = [r for r in rows if "NOFILE" in r.label]
    assert len(nofile) == 1
    assert nofile[0].available


def test_render_handles_unavailable_row() -> None:
    """A row with available=False must render "n/a on this
    platform" — and must NOT show soft/hard fields (would be
    misleading without real data)."""
    row = _LimitRow(
        label="Fake (FAKE)",
        note="this is a test",
        soft=None,
        hard=None,
        available=False,
    )
    rendered = _render((row,))
    assert "n/a on this platform" in rendered
    # No soft= or hard= for unavailable rows.
    assert "soft=" not in rendered.split("n/a on this platform")[0].split("Fake (FAKE)")[1]


def test_render_handles_available_row() -> None:
    """A normal row shows soft= and hard= explicitly. The
    operator's eye wants the two numbers labelled, not space-
    separated."""
    row = _LimitRow(
        label="Test (TEST)",
        note="test note",
        soft=1024,
        hard=2048,
        available=True,
    )
    rendered = _render((row,))
    assert "soft=" in rendered
    assert "1,024" in rendered
    assert "hard=" in rendered
    assert "2,048" in rendered
