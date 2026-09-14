"""End-to-end ``/admin_runtime``.

Pins:

* Non-developer → silent drop.
* Card surfaces all four tunables (recursionlimit, switchinterval,
  int_max_str_digits, maxsize).
* recursionlimit raised > 2× default → ⚠.
* switchinterval raised > 4× default → ⚠.
* int_max_str_digits = 0 (disabled) → ⚠ + "disabled" hint.
* int_max_str_digits unavailable (older interpreter) → no ⚠;
  bare "unavailable" row.
* maxsize below 64-bit expectation (32-bit build) → ⚠.
* Healthy state (all at defaults) → bare render. Cry-wolf
  prevention.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.runtime import (
    _EXPECTED_MAXSIZE_64BIT,
    _render,
    _RuntimeSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    recursion_limit: int = 1000,
    switch_interval: float = 0.005,
    int_max_str_digits: int = 4300,
    int_max_str_digits_available: bool = True,
    maxsize: int = _EXPECTED_MAXSIZE_64BIT,
) -> _RuntimeSnapshot:
    return _RuntimeSnapshot(
        recursion_limit=recursion_limit,
        switch_interval=switch_interval,
        int_max_str_digits=int_max_str_digits,
        int_max_str_digits_available=int_max_str_digits_available,
        maxsize=maxsize,
    )


def _row_warn_count(rendered: str) -> int:
    """Count per-row ⚠, excluding the footer legend prefix. Same
    partition trick as test_admin_flags / test_admin_cpu — keeps
    the assertion targeting per-row markers without coupling to
    the exact HTML shape."""
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_runtime", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_structure(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_runtime", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "recursionlimit" in text
    assert "switchinterval" in text
    assert "int_max_str_digits" in text
    assert "maxsize" in text


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
            "/admin_runtime",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_recursionlimit_bumped_surfaces_warning() -> None:
    """The 10000+ recursionlimit failure mode the module docstring
    documents: turns a tractable RecursionError into a C-stack
    segfault. ⚠ on the row is the visual cue."""
    rendered = _render(_snap(recursion_limit=10_000))
    assert _row_warn_count(rendered) == 1


def test_render_switchinterval_cranked_surfaces_warning() -> None:
    """Cranking switchinterval to 0.1s on an async bot is the
    "I read a blog post" failure mode — coroutines wait up to
    100ms for GIL handoffs and the bot's latency budget evaporates."""
    rendered = _render(_snap(switch_interval=0.1))
    assert _row_warn_count(rendered) == 1


def test_render_int_max_str_digits_disabled_surfaces_warning() -> None:
    """Setting int_max_str_digits to 0 disables the CVE-2020-10735
    mitigation. On a bot that takes user text this is a real DoS
    surface. Renderer must mark AND spell out "disabled" — the
    bare 0 is ambiguous ("no digits allowed?")."""
    rendered = _render(_snap(int_max_str_digits=0))
    assert _row_warn_count(rendered) == 1
    assert "disabled" in rendered


def test_render_int_max_str_digits_unavailable_no_warning() -> None:
    """An older interpreter without the limit can't be faulted —
    it's a feature absence, not a misconfiguration. Renderer must
    surface "unavailable" without ⚠."""
    rendered = _render(
        _snap(int_max_str_digits=-1, int_max_str_digits_available=False),
    )
    assert "unavailable" in rendered
    assert _row_warn_count(rendered) == 0


def test_render_32bit_maxsize_surfaces_warning() -> None:
    """A 32-bit build slipped into a 64-bit deploy is exactly the
    "how did this even happen" failure mode the maxsize row exists
    to catch."""
    rendered = _render(_snap(maxsize=2**31 - 1))
    assert _row_warn_count(rendered) == 1


def test_render_healthy_defaults_no_warnings() -> None:
    """All four tunables at CPython defaults → bare render. If the
    renderer marked any healthy row, the ⚠ glyph would burn out
    across the admin surface (cry-wolf prevention, same posture
    as warnings_view / flags / locale / cpu)."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_tolerates_small_recursionlimit_bump() -> None:
    """Test frameworks routinely bump recursionlimit to 1500 — the
    "C-stack overflow" failure mode only bites at ~10k. A 50%
    bump must NOT trigger ⚠ or the card cries wolf every CI run."""
    rendered = _render(_snap(recursion_limit=1500))
    assert _row_warn_count(rendered) == 0
