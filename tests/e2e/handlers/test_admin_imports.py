"""End-to-end ``/admin_imports``.

Pins:

* Non-developer → silent drop.
* Card renders total + builtin + frozen + load-bearing + heaviest.
* sys.modules total above ``_MODULES_TOTAL_CONCERNING`` → ⚠.
* Healthy state (modest total, all probes ok) → bare. Cry-wolf
  prevention.
* Load-bearing package not imported → rendered as "not imported"
  (informational, not flagged).
* Heaviest-others list excludes load-bearing packages so the card
  doesn't double-count.
* Capture against the real sys.modules succeeds with reasonable
  structural invariants.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.imports import (
    _LOAD_BEARING_PACKAGES,
    _MODULES_TOTAL_CONCERNING,
    _capture,
    _heavy_packages_excluding_load_bearing,
    _ImportsSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _ImportsSnapshot:
    top: Counter[str] = Counter(
        {
            "aiogram": 40,
            "sqlalchemy": 80,
            "pydantic": 20,
            "loguru": 5,
            "telegram_invite_bot": 30,
            "json": 3,
            "asyncio": 12,
            "concurrent": 4,
            "encodings": 25,
        }
    )
    load_bearing = {pkg: top.get(pkg, 0) for pkg in _LOAD_BEARING_PACKAGES}
    defaults: dict[str, Any] = {
        "total": sum(top.values()),
        "top_level_counts": top,
        "load_bearing_counts": load_bearing,
        "builtin_count": 50,
        "frozen_count": 80,
    }
    defaults.update(overrides)
    return _ImportsSnapshot(**defaults)


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
        make_message_update("/admin_imports", user_id=42, chat_type="private"),
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
        make_message_update("/admin_imports", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "sys.modules" in text
    assert "total" in text
    assert "builtin" in text
    assert "frozen" in text
    assert "load-bearing" in text


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
            "/admin_imports",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_excessive_total_warns() -> None:
    """Above the threshold the card surfaces ⚠ on the total row.
    Catches the "we accidentally imported pandas/torch" scenario."""
    huge: Counter[str] = Counter({"pandas": _MODULES_TOTAL_CONCERNING + 100})
    rendered = _render(_snap(total=_MODULES_TOTAL_CONCERNING + 100, top_level_counts=huge))
    assert _row_warn_count(rendered) >= 1


def test_render_healthy_state_no_warnings() -> None:
    """Modest total, load-bearing imported → bare. Cry-wolf prevention."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_load_bearing_not_imported_informational() -> None:
    """A load-bearing package absent from sys.modules is informational
    (lazy import hasn't fired yet) — rendered as "not imported"
    without a ⚠ marker. Flagging it would dilute the triage signal."""
    rendered = _render(_snap(load_bearing_counts={pkg: 0 for pkg in _LOAD_BEARING_PACKAGES}))
    assert "not imported" in rendered
    assert _row_warn_count(rendered) == 0


def test_heavy_packages_excludes_load_bearing() -> None:
    """The "heaviest other" list must not re-list load-bearing packages
    — they have their own row above. Double-counting would push real
    surprises off the bottom of the card."""
    snap = _snap()
    heavy = _heavy_packages_excluding_load_bearing(snap)
    names = {n for n, _ in heavy}
    for pkg in _LOAD_BEARING_PACKAGES:
        assert pkg not in names


def test_capture_on_real_sys_modules() -> None:
    """End-to-end ``_capture`` against the real ``sys.modules``.
    We assert structural invariants only — the test host's exact
    module count and frozen-module count varies across CPython
    versions / -X frozen_modules settings."""
    snap = _capture()
    # sys.modules is never empty in any sane Python process.
    assert snap.total > 0
    # The load-bearing dict must have entries for every key in the
    # constant, even if the value is zero.
    for pkg in _LOAD_BEARING_PACKAGES:
        assert pkg in snap.load_bearing_counts
    # builtin + frozen counts must each be ≤ total.
    assert snap.builtin_count <= snap.total
    assert snap.frozen_count <= snap.total
    # The test module itself is part of sys.modules so the test
    # package's top-level count must be ≥ 1.
    assert snap.top_level_counts["tests"] >= 1
