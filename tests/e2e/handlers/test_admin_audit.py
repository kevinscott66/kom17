"""End-to-end ``/admin_audit``.

Pins:

* Non-developer → silent drop.
* Live card renders + reports the probe as live.
* Healthy snapshot has NO ⚠ (cry-wolf prevention — the absence
  of other audit events is normal for an idle bot, not a fault).
* Broken-pipeline snapshot (probe_fired=False) DOES emit ⚠ —
  that's the one signal this card exists for.
* Recent-events list deduplicates noisy repeats.
* Group invocation → router-level private filter rejects.
* The probe hook is installed ONCE per process, however many
  times the card is opened (#1458). PEP 578 hooks cannot be
  removed, so this is the difference between one permanent
  callback and one per invocation.

The unit tests use synthetic ``_AuditSnapshot`` objects so we
don't need a live interpreter to provoke the broken-pipeline
case (which would be a CPython bug we can't reproduce).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.audit import (
    _STATE,
    _AuditSnapshot,
    _capture,
    _pipeline_broken,
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
        bot, make_message_update("/admin_audit", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_audit", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "sys.audit" in text
    # Healthy interpreter always dispatches the probe — assert
    # the "live" wording rather than the negation.
    assert "live" in text


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
        make_message_update("/admin_audit", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_live_capture_dispatches_probe() -> None:
    """The whole point of this card. ``sys.audit`` MUST dispatch
    to our installed hook in any healthy CPython — if this test
    fails on CI, the interpreter itself is broken."""
    snap = _capture()
    assert snap.probe_fired
    assert snap.probe_call_count == 1


def test_healthy_snapshot_has_no_warning() -> None:
    """Cry-wolf prevention: a working audit pipeline must not
    emit ⚠. The footer disclaimer mentions ⚠ legend; partition
    there and assert the body is clean."""
    snap = _AuditSnapshot(
        probe_fired=True,
        probe_call_count=1,
        other_events=(),
        python_version="3.12.0",
    )
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" not in head


def test_broken_pipeline_emits_warning() -> None:
    """The one failure mode this card detects: probe fired but
    hook didn't see it. ⚠ must appear in the body so the
    operator sees the signal."""
    snap = _AuditSnapshot(
        probe_fired=False,
        probe_call_count=0,
        other_events=(),
        python_version="3.12.0",
    )
    rendered = _render(snap)
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert "⚠" in head
    assert _pipeline_broken(snap)


def test_pipeline_broken_predicate() -> None:
    """``_pipeline_broken`` is False when the probe fired, True
    when it didn't. Pinned because this is the single
    operator-facing health predicate of the card."""
    fired = _AuditSnapshot(
        probe_fired=True,
        probe_call_count=1,
        other_events=(),
        python_version="3.12.0",
    )
    not_fired = _AuditSnapshot(
        probe_fired=False,
        probe_call_count=0,
        other_events=(),
        python_version="3.12.0",
    )
    assert not _pipeline_broken(fired)
    assert _pipeline_broken(not_fired)


def test_events_deduplicate() -> None:
    """The render must dedupe repeated events — the same event
    firing 5× during the tiny probe window is noisy and
    uninformative. First-seen order preserved."""
    snap = _AuditSnapshot(
        probe_fired=True,
        probe_call_count=1,
        other_events=("open", "open", "compile", "open", "compile"),
        python_version="3.12.0",
    )
    rendered = _render(snap)
    assert rendered.count("<code>open</code>") == 1
    assert rendered.count("<code>compile</code>") == 1


def test_no_other_events_renders_explanatory() -> None:
    """If no real events fired, render the explanatory note —
    otherwise an operator might think the audit framework is
    broken when it's just an idle bot."""
    snap = _AuditSnapshot(
        probe_fired=True,
        probe_call_count=1,
        other_events=(),
        python_version="3.12.0",
    )
    rendered = _render(snap)
    assert "no other audit events" in rendered


def test_capture_installs_the_probe_hook_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1458 — opening the card twice must not cost two hooks.

    ``sys.addaudithook`` is irreversible: whatever this function
    installs is called for every ``open`` / ``exec`` /
    ``socket.connect`` / ``import`` for the rest of the process's
    life. The first version installed one per ``/admin_audit``,
    so the only observable that matters is the number of installs,
    not the snapshot. ``addaudithook`` is replaced here precisely
    so the assertion costs the test process nothing, which also
    means the probe may not fire — do not assert on that.
    """
    installs: list[object] = []
    monkeypatch.setattr(sys, "addaudithook", installs.append)
    # The real hook may already be installed by an earlier test in
    # this process; re-arm the flag so the install path is taken.
    monkeypatch.setattr(_STATE, "installed", False)

    _capture()
    _capture()
    _capture()

    assert len(installs) == 1
    assert _STATE.installed
    # The window is closed again, so the hook is inert between probes.
    assert not _STATE.active


def test_python_version_surfaced() -> None:
    """The python_version field is rendered so the operator
    doesn't have to cross-reference /admin_python — pinned
    because the card explicitly advertises this in its docstring."""
    snap = _AuditSnapshot(
        probe_fired=True,
        probe_call_count=1,
        other_events=(),
        python_version="3.12.7",
    )
    assert "3.12.7" in _render(snap)
