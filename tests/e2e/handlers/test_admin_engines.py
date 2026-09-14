"""End-to-end ``/admin_engines``.

Pins:

* Non-developer → silent drop.
* Card lists every DB from :data:`ALL_DBS` with its pool counters.
* Healthy footer when nothing is at the ceiling.
* Group invocation → router-level private filter rejects.
* Unit pin on :func:`_render` for the at-ceiling warning branch —
  the whole point of the card is the ⚠ glyph firing when the pool
  is exhausted, so the warning path is load-bearing.
* Unit pin on :class:`_PoolSnapshot.at_ceiling` — both the
  ``max_overflow``-aware branch and the fallback branch (used for
  NullPool-style pools that don't expose ``_max_overflow``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.handlers.admin.engines import (
    _PoolSnapshot,
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
        bot,
        make_message_update("/admin_engines", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_lists_every_db_under_healthy_pool(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """In the test wiring nothing is checked out — the card should
    list every engine and render the all-clean footer.

    The happy path matters: if the card warned for an idle pool the
    operator would learn to ignore the ⚠ glyph that's the whole
    point of the diagnostic."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_engines", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Engine connection-pool snapshot" in text
    for db in ALL_DBS:
        # Header per DB — assert no ⚠ next to the header on idle pool.
        assert f"<b>{db.value}</b>\n" in text
    assert "All engines within configured pool limits" in text
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
            "/admin_engines",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_at_ceiling_uses_max_overflow_when_present() -> None:
    """Ceiling = ``pool_size + max_overflow``. Saturating *just*
    pool_size while max_overflow > 0 is NOT the ceiling — burst
    capacity is still available."""
    saturating_only_base = _PoolSnapshot(
        db=DBName.USERS,
        pool_class="AsyncAdaptedQueuePool",
        pool_size=5,
        checked_in=0,
        checked_out=5,
        overflow=0,
        max_overflow=10,
    )
    assert not saturating_only_base.at_ceiling
    fully_saturated = _PoolSnapshot(
        db=DBName.USERS,
        pool_class="AsyncAdaptedQueuePool",
        pool_size=5,
        checked_in=0,
        checked_out=15,  # 5 + 10
        overflow=10,
        max_overflow=10,
    )
    assert fully_saturated.at_ceiling


def test_at_ceiling_falls_back_when_max_overflow_unknown() -> None:
    """NullPool and friends don't expose ``_max_overflow``. The card
    still has to give an operator-meaningful "are we wedged?" signal
    — fall back to ``checked_out >= pool_size``."""
    snap = _PoolSnapshot(
        db=DBName.USERS,
        pool_class="NullPool",
        pool_size=1,
        checked_in=0,
        checked_out=1,
        overflow=0,
        max_overflow=None,
    )
    assert snap.at_ceiling


def test_render_flags_at_ceiling_engine_with_warning_footer() -> None:
    """The ⚠ glyph and the explanatory footer are the load-bearing
    surface — without them the card would be a wall of numbers."""
    snaps = [
        _PoolSnapshot(
            db=DBName.USERS,
            pool_class="AsyncAdaptedQueuePool",
            pool_size=5,
            checked_in=0,
            checked_out=15,
            overflow=10,
            max_overflow=10,
        ),
    ]
    rendered = _render(snaps)
    # Header gets the ⚠ glyph.
    assert "<b>users</b> ⚠" in rendered
    # Footer flips to the warning copy explaining what to grep for.
    assert "at its connection ceiling" in rendered
    assert "async with" in rendered.lower() or "async with" in rendered
    # Idle-pool footer must NOT appear when any engine is at the ceiling.
    assert "All engines within configured pool limits" not in rendered
