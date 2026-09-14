"""End-to-end ``/referrals`` (Stage 29).

Pins:

* Wallets in ``economy.users`` with ``referred_by=caller_id`` show up
  as the invitee list; balance comes from each wallet's own row.
* Earnings line is ``SUM(amount)`` over ``transactions`` filtered by
  ``to_id=caller_id AND type='referral'`` — other transaction kinds
  don't leak in.
* Empty list → header still renders (zeros), body is the localised
  ``referrals_empty`` line.
* Cross-engine join: display names come from ``users.users``,
  invitees come from ``economy.users``. A wallet without a matching
  profile row falls back to ``ID<uid>`` rather than blanking.
* Display cap of 20 — wallet 21+ collapses into the overflow tail.
* HTML in first_name is escaped (defence-in-depth — the helper
  escapes for us, but the test pins the boundary).
* All three command aliases route.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CALLER = 5000


async def _seed_caller(registry: EngineRegistry, *, lang: str = "ru") -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=_CALLER, first_name="Me", language_code=lang))
        await session.commit()


async def _seed_invitees(
    registry: EngineRegistry,
    rows: list[tuple[int, int, int | None]],  # (user_id, balance, referred_by)
    *,
    names: dict[int, str] | None = None,
) -> None:
    """Seed economy wallets and (optionally) matching first_names.

    Splitting names from wallets mirrors prod: a wallet row in
    economy.db can outlive (or arrive before) the profile row in
    users.db. The test that exercises the missing-name fallback
    relies on this split — see ``test_missing_name_falls_back``.
    """
    economy = registry.engine(DBName.ECONOMY)
    async with AsyncSession(economy) as session:
        for uid, balance, referred_by in rows:
            session.add(EconomyUser(user_id=uid, balance=balance, referred_by=referred_by))
        await session.commit()
    if names:
        users = registry.engine(DBName.USERS)
        async with AsyncSession(users) as session:
            for uid, first_name in names.items():
                session.add(User(user_id=uid, first_name=first_name))
            await session.commit()


async def _seed_transactions(
    registry: EngineRegistry,
    rows: list[tuple[int, int, str]],  # (to_id, amount, type)
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for to_id, amount, tx_type in rows:
            session.add(
                Transaction(
                    to_id=to_id,
                    amount=amount,
                    type=tx_type,
                    date=datetime(2026, 1, 1),
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_referrals_renders_invitees_and_earnings(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    await _seed_invitees(
        registry,
        rows=[(101, 250, _CALLER), (102, 50, _CALLER)],
        names={101: "Alice", 102: "Bob"},
    )
    # Mix of referral and non-referral transactions — only the
    # referral rows count toward the displayed earnings total.
    await _seed_transactions(
        registry,
        rows=[
            (_CALLER, 25, "referral"),
            (_CALLER, 15, "referral"),
            (_CALLER, 999, "transfer"),  # not a referral commission
        ],
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update("/referrals", user_id=_CALLER))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Всего приглашено: <b>2</b>" in body
    assert "Заработано с рефералов: <b>40</b>" in body  # 25 + 15, not + 999
    assert "Alice" in body and "250" in body
    assert "Bob" in body and "50" in body


@pytest.mark.asyncio
async def test_referrals_empty_renders_localised_line(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry, lang="en")
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/referrals", user_id=_CALLER, language_code="en")
    )
    body = sent[-1]["text"]
    assert "Total invited: <b>0</b>" in body
    assert "Earned from referrals: <b>0</b>" in body
    assert "No one yet" in body


@pytest.mark.asyncio
async def test_referrals_missing_profile_falls_back_to_id(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A wallet without a matching users.users row still renders;
    legacy used the literal ``ID<uid>`` fallback so an invitee who
    blocked the bot (clearing their profile row) is still visible to
    the referrer who's owed commission on them.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    # No names dict → invitee 777 has no first_name in users.db.
    await _seed_invitees(registry, rows=[(777, 10, _CALLER)])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/referrals", user_id=_CALLER))
    body = sent[-1]["text"]
    assert "ID777" in body


@pytest.mark.asyncio
async def test_referrals_caps_at_twenty_and_shows_overflow(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """22 invitees → 20 rendered + ``+2`` overflow suffix. The header
    "total invited" still reports the full 22 — the cap is rendering-
    only.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    rows = [(200 + i, i, _CALLER) for i in range(22)]
    await _seed_invitees(registry, rows=rows)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/referrals", user_id=_CALLER))
    body = sent[-1]["text"]
    assert "Всего приглашено: <b>22</b>" in body
    # The 21st invitee (index 20, user_id 220) must NOT appear in
    # the rendered list — the cap drops them into the overflow tail.
    assert "ID220" not in body
    assert "+2" in body


@pytest.mark.asyncio
async def test_referrals_escapes_html_in_first_name(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    await _seed_invitees(
        registry,
        rows=[(900, 0, _CALLER)],
        names={900: "<i>nope</i>"},
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/referrals", user_id=_CALLER))
    body = sent[-1]["text"]
    # Raw markup must not survive into the rendered card.
    assert "<i>nope</i>" not in body
    assert "&lt;i&gt;nope&lt;/i&gt;" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/referrals", "/рефералы", "/мои_рефералы"])
async def test_referrals_aliases_route(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    cmd: str,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_caller(registry)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, make_message_update(cmd, user_id=_CALLER))
    assert result is not UNHANDLED
    assert sent, f"alias {cmd} did not produce a reply"
