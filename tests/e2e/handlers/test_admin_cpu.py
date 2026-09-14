"""End-to-end ``/admin_cpu``.

Pins:

* Non-developer → silent drop.
* Card renders cpu_count + affinity + load-avg rows.
* Affinity narrower than cpu_count → ⚠ marker (cgroup / CPUAffinity
  pinning catch).
* 5-min load average above cpu_count → ⚠ marker (host CPU-saturated;
  bot latency budget at risk).
* Healthy state (affinity == cpu_count, load < cpu_count) → bare
  render. Cry-wolf prevention: the ⚠ glyph must keep triage value.
* Affinity unavailable (non-Linux) → "unavailable" hint, no ⚠.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.cpu import (
    _CPUSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(
    *,
    cpu_count: int | None = 8,
    affinity: tuple[int, ...] | None = (0, 1, 2, 3, 4, 5, 6, 7),
    loadavg: tuple[float, float, float] | None = (1.0, 1.0, 1.0),
    loadavg_available: bool = True,
) -> _CPUSnapshot:
    return _CPUSnapshot(
        cpu_count=cpu_count,
        affinity=affinity,
        loadavg=loadavg,
        loadavg_available=loadavg_available,
    )


def _row_warn_count(rendered: str) -> int:
    """Count per-row ⚠ markers, excluding the footer legend.

    The footer line begins with ``<i>⚠`` and is a fixed prose key;
    per-row markers appear before that on bullet lines. Partition
    on the legend prefix and count ⚠ in the head — same helper
    pattern as test_admin_flags."""
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_cpu", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_structure(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_cpu", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "CPU capacity" in text
    assert "logical cores" in text
    assert "load average" in text


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
            "/admin_cpu",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_narrowed_affinity_surfaces_warning() -> None:
    """A process pinned to a subset of cores (CPUAffinity= in
    systemd, --cpuset-cpus= on a container) is exactly the case
    where the operator sees the bot maxing out CPU while ``top``
    on the host shows idle cores. The ⚠ on the affinity row is
    the visual cue."""
    rendered = _render(_snap(cpu_count=8, affinity=(0,)))
    assert _row_warn_count(rendered) == 1


def test_render_saturated_loadavg_surfaces_warning() -> None:
    """5-min load > cpu_count means processes are queued waiting
    for CPU. Sustained saturation is the standard "host is the
    bottleneck, not the bot" signal — operators triaging latency
    spikes need to see the ⚠ to know to look at the host before
    blaming the bot."""
    rendered = _render(_snap(cpu_count=4, loadavg=(10.0, 10.0, 10.0)))
    assert _row_warn_count(rendered) == 1


def test_render_healthy_state_no_warnings() -> None:
    """Affinity == cpu_count and load << cpu_count. If the renderer
    marked this, the ⚠ glyph would burn out across the admin
    surface (cry-wolf prevention, mirrored from warnings_view /
    flags / locale)."""
    rendered = _render(
        _snap(cpu_count=8, affinity=(0, 1, 2, 3, 4, 5, 6, 7), loadavg=(0.5, 0.5, 0.5)),
    )
    assert _row_warn_count(rendered) == 0


def test_render_affinity_unavailable_no_warning() -> None:
    """Non-Linux hosts (macOS dev box, Windows CI) have no
    sched_getaffinity. The card must render "unavailable" rather
    than ⚠ — an absent number isn't a misconfiguration."""
    rendered = _render(_snap(affinity=None))
    assert "unavailable" in rendered
    assert _row_warn_count(rendered) == 0


def test_render_loadavg_unavailable_branch() -> None:
    """Some sandboxed environments (Docker without --cap, certain
    minimal containers) raise OSError on getloadavg. The renderer
    must surface "unavailable" rather than crash or mark — same
    posture as the affinity-absent branch."""
    rendered = _render(_snap(loadavg=None, loadavg_available=False))
    assert "load average" in rendered
    assert "unavailable" in rendered


def test_render_large_affinity_set_collapses() -> None:
    """On a 64-core box the full affinity list would push the card
    past Telegram's 4096-char limit. The renderer collapses sets
    larger than 8 to a count — the operator can still spot
    narrowing via the count itself, which is the load-bearing
    number."""
    affinity = tuple(range(64))
    rendered = _render(_snap(cpu_count=64, affinity=affinity))
    # Full list (0, 1, 2, ..., 63) must NOT appear verbatim — we
    # collapsed it. The count must.
    assert "64 cpus" in rendered
    assert "0, 1, 2, 3, 4, 5, 6, 7, 8" not in rendered
