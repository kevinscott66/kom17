"""End-to-end ``/admin_rusage``.

Pins:

* Non-developer → silent drop.
* Card renders CPU time + RSS + page faults + context switches +
  block I/O rows.
* Major page faults above threshold → ⚠.
* Involuntary context switches above threshold → ⚠.
* Healthy cumulative counts → bare. Cry-wolf prevention.
* Unavailable (Windows / resource import failed) → "unavailable"
  message, no fake zeros.
* maxrss unit varies by platform — render must surface the source
  unit in italics so an operator running both Linux + macOS
  captures can tell them apart.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.rusage import (
    _MAJFLT_CONCERNING,
    _NIVCSW_CONCERNING,
    _render,
    _RUsageSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _RUsageSnapshot:
    defaults: dict[str, Any] = {
        "available": True,
        "utime": 1.5,
        "stime": 0.3,
        "maxrss": 102400,
        "maxrss_unit": "kB",
        "minflt": 1234,
        "majflt": 0,
        "inblock": 0,
        "oublock": 0,
        "nvcsw": 100,
        "nivcsw": 50,
    }
    defaults.update(overrides)
    return _RUsageSnapshot(**defaults)


def _row_warn_count(rendered: str) -> int:
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
        make_message_update("/admin_rusage", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_rusage", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "getrusage" in text


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
            "/admin_rusage",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_includes_all_rows() -> None:
    """Every operationally-relevant row must render. Shape stability
    is what lets the operator diff two samples and spot rates."""
    rendered = _render(_snap())
    assert "CPU time" in rendered
    assert "peak RSS" in rendered
    assert "page faults" in rendered
    assert "context switches" in rendered
    assert "block I/O" in rendered


def test_render_majflt_above_threshold_surfaces_warning() -> None:
    """Major page faults > threshold means the working set is
    spilling to swap/disk. ⚠ on the row is the cue to cross-check
    /admin_memory's VmSwap."""
    rendered = _render(_snap(majflt=_MAJFLT_CONCERNING + 1))
    assert _row_warn_count(rendered) == 1


def test_render_nivcsw_above_threshold_surfaces_warning() -> None:
    """Involuntary context switches > threshold means the kernel
    is preempting us — host CPU contention. ⚠ pairs with
    /admin_cpu's load average for stereoscopic diagnosis."""
    rendered = _render(_snap(nivcsw=_NIVCSW_CONCERNING + 1))
    assert _row_warn_count(rendered) == 1


def test_render_healthy_counters_no_warnings() -> None:
    """Low major faults, low involuntary switches, low I/O —
    every cumulative number well below threshold. If any healthy
    row marked ⚠ the glyph would burn out across the diagnostic
    surface (cry-wolf prevention, mirrored from warnings_view /
    flags / locale / cpu / runtime / memory)."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_unavailable_branch() -> None:
    """Windows / resource-module-missing → "unavailable" message.
    Renderer must NOT emit fake zero rows — every counter is
    cumulative since process start and a 0 would be confusable
    with a freshly-started healthy bot."""
    rendered = _render(_RUsageSnapshot(available=False))
    assert "unavailable" in rendered
    assert "page faults" not in rendered
    assert "context switches" not in rendered


def test_render_maxrss_unit_surfaced() -> None:
    """Linux reports maxrss in kB; macOS in bytes. Same field name,
    1024× factor difference. Renderer must surface the source unit
    so an operator running both can spot which capture used which
    conversion — a silent unit mismatch is a three-orders-of-
    magnitude misread."""
    rendered_linux = _render(_snap(maxrss=102400, maxrss_unit="kB"))
    rendered_macos = _render(_snap(maxrss=102400 * 1024, maxrss_unit="bytes"))
    assert "from kB" in rendered_linux
    assert "from bytes" in rendered_macos
    # And the converted value should be the same MiB on both
    # (verifying the unit-aware formatter actually divides
    # correctly).
    assert "100.0 MiB" in rendered_linux
    assert "100.0 MiB" in rendered_macos


def test_render_majflt_exactly_at_threshold_not_concerning() -> None:
    """Threshold is strict ``>``: an operator-tuned threshold needs
    to behave as documented. Pin the boundary so a future change
    that swaps ``>`` for ``>=`` doesn't silently shift the trigger."""
    rendered = _render(_snap(majflt=_MAJFLT_CONCERNING))
    assert _row_warn_count(rendered) == 0
