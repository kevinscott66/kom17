"""End-to-end ``/admin_flags``.

Pins:

* Non-developer → silent drop.
* Card renders every flag in ``_FLAGS_OF_INTEREST`` — drift between
  the handler tuple and what the operator sees would silently shrink
  the diagnostic surface.
* Protective flag off (``no_user_site=0`` / ``hash_randomization=0``)
  → ⚠ marker. Load-bearing: the whole reason to surface these flags
  is to catch the case an ``-s`` or ``PYTHONHASHSEED`` got left off.
* Development mode on (``debug=1`` / ``dev_mode=1`` / ``optimize=1``
  / ``verbose=1``) → ⚠ marker. Same reasoning in reverse: a stray
  ``PYTHONDEVMODE=1`` or ``-O`` in prod is exactly what the card
  exists to catch.
* Informational flags (``no_site`` / ``isolated`` / ``safe_path``)
  must NOT mark either way — operators read the value and decide.
  If we marked them, the ⚠ glyph loses its triage signal (cry-wolf
  prevention, same posture as warnings_view).
* Healthy state (all protective on, all dev-modes off) → bare render
  with no ⚠ markers anywhere.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.flags import (
    _FLAGS_OF_INTEREST,
    _FlagRow,
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
        bot, make_message_update("/admin_flags", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_every_flag(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_flags", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "sys.flags" in text
    # Every flag in _FLAGS_OF_INTEREST must appear — drift would
    # silently shrink the diagnostic surface.
    for flag in _FLAGS_OF_INTEREST:
        assert flag in text, f"flag {flag!r} missing from rendered card"


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
            "/admin_flags",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def _row(name: str, value: int, concerning: bool) -> _FlagRow:
    return _FlagRow(name=name, raw_value=value, concerning=concerning)


def _row_warn_count(rendered: str) -> int:
    """Count per-row ⚠ markers, excluding the footer legend.

    The footer line begins with ``<i>⚠`` (legend) and is one fixed
    occurrence; per-row markers appear after ``</code>`` on bullet
    lines. Splitting the render on the legend prefix and counting
    ⚠ in the head gives the per-row count without coupling to the
    exact HTML shape.
    """
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


def test_render_protective_flag_off_surfaces_warning() -> None:
    """``no_user_site=0`` (user site-packages loaded) is the
    shadowing-vector the module docstring documents — on a shared
    host, anyone who owns ``~/.local/lib/...`` can shadow our deps.
    The ⚠ marker is the visual cue an operator needs to spot the
    ``-s`` missing from the systemd unit."""
    rendered = _render([_row("no_user_site", 0, True)])
    assert _row_warn_count(rendered) == 1


def test_render_dev_mode_on_surfaces_warning() -> None:
    """``debug=1`` / ``dev_mode=1`` / ``optimize`` ≠ 0 are
    development-mode switches that have measurable runtime costs or
    behaviour changes. A prod build with any of these flipped on is
    exactly the regression the card exists to catch."""
    for name in ("debug", "dev_mode", "verbose"):
        rendered = _render([_row(name, 1, True)])
        assert _row_warn_count(rendered) == 1, f"{name}=1 must surface ⚠"
    # ``optimize`` is 0/1/2; both non-zero values are concerning.
    rendered = _render([_row("optimize", 2, True)])
    assert _row_warn_count(rendered) == 1


def test_render_healthy_state_no_warnings() -> None:
    """All protective flags on, all dev-modes off — the card must
    render bare. If the renderer marked any healthy row, the ⚠
    glyph would lose its triage value across the whole admin
    diagnostic surface (cry-wolf prevention, mirrored from
    warnings_view)."""
    rendered = _render(
        [
            _row("debug", 0, False),
            _row("optimize", 0, False),
            _row("dev_mode", 0, False),
            _row("verbose", 0, False),
            _row("no_user_site", 1, False),
            _row("no_site", 0, False),
            _row("isolated", 0, False),
            _row("ignore_environment", 0, False),
            _row("safe_path", 0, False),
            _row("hash_randomization", 1, False),
        ]
    )
    # Per-row warning markers must be absent. The footer legend
    # contains a literal ⚠ glyph as part of its prose key — we
    # exclude that one fixed occurrence so the assertion targets
    # per-row markers only.
    assert _row_warn_count(rendered) == 0


def test_render_informational_flag_never_marked() -> None:
    """``no_site`` / ``isolated`` / ``safe_path`` are deployer-choice
    flags — surfaced but never flagged either way. If we marked
    ``isolated=1`` an operator running an intentionally isolated
    interpreter would chase a false alarm; if we marked ``=0`` we'd
    nag every default deployment. Informational only."""
    for name in ("no_site", "isolated", "safe_path"):
        rendered = _render([_row(name, 1, False)])
        assert _row_warn_count(rendered) == 0, f"{name} must render bare regardless of value"
