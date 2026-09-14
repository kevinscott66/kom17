"""#1946: the anti-abuse new-account lockout for ``/daily``, wired at last.

``utils.daily.new_user_lockout_remaining`` was ported in full — docstring,
edge cases, five unit tests — and then never called. Nothing in ``src/``
imported it, no settings field carried its ``lockout_hours``, and so the
legacy guard at ``bot.py:12024`` simply did not exist in the new pipeline:
a wallet minted seconds ago could claim the bonus, which is precisely the
throwaway-account farming the guard was written against.

This file pins the wiring rather than the arithmetic (that lives in
``tests/unit/utils/test_daily.py``): that the service consults the helper
at all, in legacy's position relative to the cooldown check, that the
developer whitelist and the ``0 = disabled`` default both work, that a
refused claim writes nothing, and that the handler says something other
than "come back tomorrow" — the cooldown card's promise is false for an
account whose wait does not shrink into a claimable day.

The guard ships DISABLED (``anti_abuse_new_user_no_daily_hours`` defaults
to ``0``, exactly as in legacy, and this deployment carries no
``settings.json`` that ever set it). Turning it on is an operator
decision; what changes here is that the switch now exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.handlers.daily import handle_daily
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.daily_service import (
    ClaimOutcome,
    DailyConfig,
    DailyEffects,
    DailyService,
)
from telegram_invite_bot.services.economy_service import EconomyService

_USER = 4242
_DEV = 999
_NOW = datetime(2024, 5, 1, 12, 0, 0)


class _FrozenClock:
    """``datetime`` stand-in whose ``now`` is fixed — same shape as
    ``tests/integration/services/test_daily_service.py`` uses."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz: object = None) -> datetime:  # noqa: ARG002
        return self._fixed


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _service(session: AsyncSession, config: DailyConfig) -> DailyService:
    economy_repo = EconomyRepo(session)
    return DailyService(
        economy_repo,
        EconomyService(economy_repo, TransactionsRepo(session)),
        config=config,
        clock=_FrozenClock(_NOW),  # type: ignore[arg-type]
        session=session,
    )


async def _seed(session: AsyncSession, user_id: int, *, age: timedelta) -> None:
    """A wallet registered ``age`` ago, never having claimed."""
    session.add(
        EconomyUser(
            user_id=user_id,
            balance=100,
            language="ru",
            daily_streak=0,
            last_daily=None,
            registered=_NOW - age,
        )
    )
    await session.commit()


def _locked(hours: int = 24, *, exempt: frozenset[int] = frozenset()) -> DailyConfig:
    return DailyConfig(
        random_enabled=False,  # deterministic payout
        new_user_lockout_hours=hours,
        lockout_exempt_ids=exempt,
    )


async def test_a_fresh_wallet_cannot_claim_while_locked(session: AsyncSession) -> None:
    """The regression: the throwaway account gets nothing for N hours."""
    await _seed(session, _USER, age=timedelta(hours=1))

    result = await _service(session, _locked()).claim(_USER)
    await session.commit()

    assert result.outcome is ClaimOutcome.NEW_USER_LOCKED
    assert result.cooldown_remaining == timedelta(hours=23)


async def test_a_refused_claim_burns_nothing(session: AsyncSession) -> None:
    """Refusing before ``mark_daily_claimed`` is what makes it safe.

    The lockout must not spend the user's day: once the window passes
    they claim normally, streak and all. A guard placed after the SQL
    mark would have written ``last_daily`` and cost them 24 more hours.
    """
    await _seed(session, _USER, age=timedelta(hours=1))

    await _service(session, _locked()).claim(_USER)
    await session.commit()

    wallet = await EconomyRepo(session).get(_USER)
    assert wallet is not None
    assert wallet.last_daily is None
    assert wallet.daily_streak == 0
    assert wallet.balance == 100


async def test_an_aged_wallet_claims_normally(session: AsyncSession) -> None:
    """The window is a window, not a ban."""
    await _seed(session, _USER, age=timedelta(hours=25))

    result = await _service(session, _locked()).claim(_USER)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS


async def test_zero_hours_leaves_the_guard_off(session: AsyncSession) -> None:
    """The shipped default. Legacy gated its call site on ``> 0`` and so
    does ours, so a deployment that never sets the key is untouched."""
    await _seed(session, _USER, age=timedelta(minutes=1))

    result = await _service(session, _locked(hours=0)).claim(_USER)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS


async def test_developers_are_exempt(session: AsyncSession) -> None:
    """Legacy's ``user_id not in DEVELOPER_IDS`` at the same call site —
    the owner has to be able to test the flow with the guard switched on."""
    await _seed(session, _DEV, age=timedelta(minutes=1))

    result = await _service(session, _locked(exempt=frozenset({_DEV}))).claim(_DEV)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS


async def test_cooldown_still_wins_over_the_lockout(session: AsyncSession) -> None:
    """Legacy checks ``can_claim`` first (bot.py:12017) and the lockout
    after (:12024). A user who is both on cooldown and inside the window
    reads the cooldown card, unchanged by this ticket."""
    session.add(
        EconomyUser(
            user_id=_USER,
            balance=100,
            language="ru",
            daily_streak=3,
            last_daily=_NOW - timedelta(hours=2),
            registered=_NOW - timedelta(hours=2),
        )
    )
    await session.commit()

    result = await _service(session, _locked()).claim(_USER)

    assert result.outcome is ClaimOutcome.COOLDOWN


async def test_the_repo_hands_the_service_a_registration_time(
    session: AsyncSession,
) -> None:
    """Without this the guard reads ``None`` and waves everyone through.

    ``Wallet`` carried no ``registered`` field before #1946, so the
    column existed on the row and stopped at the entity boundary.
    """
    await _seed(session, _USER, age=timedelta(hours=3))

    wallet = await EconomyRepo(session).get(_USER)
    assert wallet is not None
    assert wallet.registered == _NOW - timedelta(hours=3)


class _FakeMessage:
    def __init__(self) -> None:
        self.sender_chat = None
        self.from_user = type("U", (), {"id": _USER})()
        self.sent: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:  # noqa: ARG002
        self.sent.append(text)


class _FakeEffects:
    async def resolve_daily_effects(self, user_id: int, *, now: datetime) -> DailyEffects:  # noqa: ARG002
        return DailyEffects()


async def test_the_card_does_not_promise_tomorrow(session: AsyncSession) -> None:
    """The wording matters as much as the refusal.

    The cooldown card says "next bonus in {wait}", which for a locked
    account is a claim about a day that is not coming — the user waits it
    out, presses again, and reads the same promise. The lockout card
    names the reason instead. Asserting only "some text was sent" would
    pass on the old code, where the claim SUCCEEDED and the text was the
    congratulations card.
    """
    await _seed(session, _USER, age=timedelta(hours=1))
    message = _FakeMessage()

    await handle_daily(
        message,  # type: ignore[arg-type]
        EconomyRepo(session),
        _service(session, _locked()),
        _FakeEffects(),  # type: ignore[arg-type]
        "ru",
    )

    assert len(message.sent) == 1
    card = message.sent[0]
    assert "23" in card  # the remaining window, not a 24h "tomorrow"
    assert "Бонус уже получен" not in card
    assert "Ежедневный бонус получен" not in card
