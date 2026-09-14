"""End-to-end ``/mydonates`` (Stage 24).

Pins:

* Private-DM with donations → lifetime total + history rows, top
  (most recent) first.
* Lifetime total is the SUM across the user's WHOLE ledger, not the
  rendered page — a user who donated > display_cap times still sees
  the correct grand total.
* Private-DM with no donations → "history empty" localised text
  (still renders the zero-total line above it — distinct from a
  silent reply).
* Per-user scoping: another user's donations don't surface.
* Group call falls through to legacy (router-level private filter).
* Orphan donations (group_id not in ``groups_donations``) still
  render — LEFT JOIN, not INNER.
* Group names with HTML get escaped.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import (
    Donation,
    GroupDonationsAggregate,
)
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import mydonates as mydonates_module
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import assert_chat_scope_refusal, make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_USER_ID = 1111


async def _seed_user(registry: EngineRegistry, *, lang: str = "ru") -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=_USER_ID, first_name="Me", language_code=lang))
        await session.commit()


async def _seed_donations(
    registry: EngineRegistry,
    rows: list[tuple[int, int, int, datetime]],  # (user_id, group_id, amount, ts)
    groups: dict[int, str | None] | None = None,
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for uid, gid, amount, ts in rows:
            session.add(Donation(user_id=uid, group_id=gid, amount=amount, created_at=ts))
        for gid, name in (groups or {}).items():
            session.add(GroupDonationsAggregate(group_id=gid, group_name=name))
        await session.commit()


def _dm(text: str = "/mydonates", *, user_id: int = _USER_ID) -> Any:
    return make_message_update(text, user_id=user_id, chat_type="private")


@pytest.mark.asyncio
async def test_mydonates_renders_total_and_history(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    t0 = datetime(2026, 1, 1, 12, 0)
    await _seed_donations(
        registry,
        rows=[
            (_USER_ID, -100, 100, t0),
            (_USER_ID, -200, 250, t0 + timedelta(days=1)),
        ],
        groups={-100: "Альфа", -200: "Бета"},
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _dm())

    assert result is not UNHANDLED
    body = sent[-1]["text"]
    # Lifetime total — 350 across both donations.
    assert "350" in body
    # Both group names appear, with the most recent first.
    assert body.index("Бета") < body.index("Альфа")
    # Date format is YYYY-MM-DD HH:MM (no microseconds, no TZ).
    assert "2026-01-02 12:00" in body


@pytest.mark.asyncio
async def test_mydonates_total_spans_full_ledger_beyond_page_cap(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A user with 30 donations still sees the correct grand total
    even though the rendered history caps at 25. A regression here
    would silently understate the lifetime sum on power-donor cards.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    base = datetime(2026, 1, 1)
    rows = [(_USER_ID, -100, 10, base + timedelta(hours=i)) for i in range(30)]
    await _seed_donations(registry, rows=rows, groups={-100: "G"})
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _dm())
    body = sent[-1]["text"]
    # 30 × 10 = 300, not 25 × 10 = 250.
    assert "300" in body
    assert "250" not in body


@pytest.mark.asyncio
async def test_mydonates_history_empty(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _dm())
    body = sent[-1]["text"]
    assert "0" in body  # zero-total line is rendered, not omitted
    assert t("mydonates_history_empty", "ru") in body


@pytest.mark.asyncio
async def test_mydonates_scoped_to_caller(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Critical: another user's donations must NOT surface. The handler
    filters on ``user_id`` — a regression would expose private donation
    history across users.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    base = datetime(2026, 1, 1)
    await _seed_donations(
        registry,
        rows=[
            (_USER_ID, -100, 50, base),
            (9999, -100, 999_999, base),  # someone else's huge donation
        ],
        groups={-100: "G"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _dm())
    body = sent[-1]["text"]
    assert "50" in body
    assert "999999" not in body
    assert "999,999" not in body  # also not in any thousand-formatted shape


@pytest.mark.asyncio
async def test_mydonates_in_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A group ``/mydonates`` is answered, not ignored (#123).

    The command is DM-only because it lists what this user paid; the
    refusal twin says so (and offers the deep link) instead of leaving
    the group with silence.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/mydonates",
            user_id=_USER_ID,
            chat_id=-100123,
            chat_type="supergroup",
        ),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="mydonates")


@pytest.mark.asyncio
async def test_mydonates_renders_orphan_groups(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Donation to a group that no longer has a row in
    ``groups_donations``. LEFT JOIN means it must still appear — the
    user's own history isn't truncated by a missing aggregate.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    await _seed_donations(
        registry,
        rows=[(_USER_ID, -100, 42, datetime(2026, 1, 1))],
        groups={},  # NO aggregate row for -100
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _dm())
    body = sent[-1]["text"]
    # Fallback name is "Group <id>"; the line still shows up.
    assert "Group -100" in body
    assert "42" in body


@pytest.mark.asyncio
async def test_mydonates_sends_every_page_not_just_the_first(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A history too wide for one message goes out as several.

    25 donations is the display cap, but the group name is the chat
    title and Telegram allows 128 characters of it — that history is
    ~4100 characters, i.e. a 400 the user never sees. The renderer
    splits it; this pins that the handler actually SENDS the tail
    instead of dropping every page after the first.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    base = datetime(2026, 1, 1)
    titles = [f"{'я' * 126}{i:02d}" for i in range(25)]
    groups: dict[int, str | None] = {-100 - i: title for i, title in enumerate(titles)}
    await _seed_donations(
        registry,
        rows=[(_USER_ID, -100 - i, 999_999, base + timedelta(hours=i)) for i in range(25)],
        groups=groups,
    )
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _dm())

    assert len(sent) > 1
    body = "\n".join(item["text"] for item in sent)
    for title in titles:
        assert title in body


@pytest.mark.asyncio
async def test_mydonates_escapes_html_in_group_name(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    await _seed_donations(
        registry,
        rows=[(_USER_ID, -100, 10, datetime(2026, 1, 1))],
        groups={-100: "<b>spoof</b>"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _dm())
    body = sent[-1]["text"]
    assert "<b>spoof</b>" not in body
    assert "&lt;b&gt;spoof&lt;/b&gt;" in body


@pytest.mark.asyncio
async def test_mydonates_does_not_hold_the_write_lock_across_the_render(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/mydonates`` answers page by page; users.db must stay writable.

    ``user_service.touch`` opens the update's write transaction, and
    ``BEGIN IMMEDIATE`` means one writer per DB until the middleware
    commits — which, without a checkpoint, is after the last page has
    been sent. Every other update in the process would spend
    ``busy_timeout`` waiting and then fail with ``database is locked``.
    The probe below runs where the first Telegram call is about to be.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_user(registry)
    await _seed_donations(
        registry,
        rows=[(_USER_ID, -100, 10, datetime(2026, 1, 1))],
        groups={-100: "G"},
    )
    sent = capture_outgoing(bot)
    other_updates_could_write: list[bool] = []
    original = mydonates_module._fetch_total

    async def fetch_while_probing(reg: Any, user_id: int) -> Any:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(
                sql_update(User).where(User.user_id == _USER_ID).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return await original(reg, user_id)

    monkeypatch.setattr(mydonates_module, "_fetch_total", fetch_while_probing)

    await dispatcher.feed_update(bot, _dm())

    assert other_updates_could_write == [True]
    assert "10" in sent[-1]["text"]
