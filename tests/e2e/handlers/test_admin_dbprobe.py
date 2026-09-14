"""End-to-end ``/admin_dbprobe``.

Pins:

* Non-developer → silent drop.
* Healthy state: every engine returns ``ok`` with a latency
  reading; footer says "All engines responsive". No ⚠.
* Render-side: failed probe → ⚠ + error-class hint.
* Render-side: slow probe (above threshold) → ⚠ on the row.
* Render-side: healthy state with sub-threshold latency → bare.
  Cry-wolf prevention.
* Render-side: failed probe does NOT also get a latency-⚠
  (single marker per failing row).
* Real probe on the test fixture: every DB answers without an
  error. The latency threshold is monkeypatched out of the way
  there — a fresh temp file is fast on an idle host and slow on a
  loaded one, and that is a property of the host, not of the
  handler.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.handlers.admin import dbprobe
from telegram_invite_bot.handlers.admin.dbprobe import (
    _LATENCY_CONCERNING_MS,
    _ProbeResult,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _healthy_results() -> list[_ProbeResult]:
    return [_ProbeResult(db=db, latency_ms=3.2, error=None) for db in ALL_DBS]


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_dbprobe", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_healthy_state_renders_ok_per_db(
    make_wired: WiredFactory, capture_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real probe against the test-fixture engines. Every engine
    must report ``ok`` and the footer must say "All engines
    responsive". Cry-wolf invariant: a clean fixture must render
    without a single ⚠.

    The latency threshold is lifted out of the way first. What this
    test owns is that a *real* probe over the real engines comes back
    without an error — and the wall clock is not part of that claim:
    the same fixture takes 3 ms on an idle laptop and blows past 100 ms
    on a host running the rest of this suite in parallel, which used to
    fail here for a reason that has nothing to do with the handler. The
    threshold's own behaviour is pinned deterministically by
    :func:`test_render_slow_probe_warns` and
    :func:`test_render_healthy_state_no_warnings`, on hand-built
    results.
    """
    monkeypatch.setattr(dbprobe, "_LATENCY_CONCERNING_MS", 1_000_000.0)
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_dbprobe", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Engine liveness probe" in text
    for db in ALL_DBS:
        assert db.value in text
    assert "All engines responsive" in text
    # With the threshold out of the picture, a ⚠ here can only mean an
    # engine actually failed to answer — which is the thing worth
    # failing the build over.
    assert "⚠" not in text


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
            "/admin_dbprobe",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def _row_warn_count(rendered: str) -> int:
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


def test_render_failed_probe_surfaces_error_class() -> None:
    """Failure → row ⚠ + error class. Routing-hint posture mirrors
    /admin_dns / /admin_tempdir."""
    pick_db = next(iter(ALL_DBS))
    results = [
        _ProbeResult(db=db, latency_ms=4.0, error=None) for db in ALL_DBS if db is not pick_db
    ]
    results.insert(0, _ProbeResult(db=pick_db, latency_ms=1500.0, error="TimeoutError"))
    rendered = _render(results)
    assert "TimeoutError" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_failed_probe_no_double_latency_warn() -> None:
    """A failed probe's latency reflects the timeout path, NOT
    "slow query". The error ⚠ is the warning; double-marking the
    same row as both error and slow would dilute the triage signal."""
    results = [_ProbeResult(db=db, latency_ms=3.0, error=None) for db in ALL_DBS]
    pick = next(iter(ALL_DBS))
    # Replace one entry with a failed probe whose latency would
    # otherwise also trigger the slow-⚠.
    for i, r in enumerate(results):
        if r.db is pick:
            results[i] = _ProbeResult(
                db=pick,
                latency_ms=_LATENCY_CONCERNING_MS * 10,
                error="OperationalError",
            )
            break
    rendered = _render(results)
    # Exactly one row-level ⚠ — the error row.
    assert _row_warn_count(rendered) == 1


def test_render_slow_probe_warns() -> None:
    """A successful probe whose latency exceeds the threshold gets
    its own ⚠ — catches the wedged-writer / busy_timeout pattern
    the module docstring documents."""
    results = [_ProbeResult(db=db, latency_ms=3.0, error=None) for db in ALL_DBS]
    results[0] = _ProbeResult(
        db=results[0].db,
        latency_ms=_LATENCY_CONCERNING_MS + 50,
        error=None,
    )
    rendered = _render(results)
    assert _row_warn_count(rendered) >= 1


def test_render_healthy_state_no_warnings() -> None:
    """Every probe ok and under-threshold → bare. Cry-wolf
    prevention; mirrors integrity / pragmas / engines."""
    rendered = _render(_healthy_results())
    assert _row_warn_count(rendered) == 0
    assert "All engines responsive" in rendered


def test_render_includes_every_db() -> None:
    """The render must mention every DB the operator expects to see,
    even when all are healthy. Missing a DB silently would be a
    regression we'd never catch in a CI green-state run otherwise."""
    rendered = _render(_healthy_results())
    for db in ALL_DBS:
        assert db.value in rendered


def test_probe_result_constructable_with_each_dbname() -> None:
    """Trivial structural pin: ``_ProbeResult`` accepts every
    :class:`DBName` value. Guards against an enum extension that
    breaks the snapshot constructor at runtime."""
    for db in ALL_DBS:
        r = _ProbeResult(db=db, latency_ms=1.0, error=None)
        assert isinstance(r.db, DBName)
