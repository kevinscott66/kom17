"""Real-SQLite tests for :class:`PvpService` — escrow stake games (AUD-2).

Covers the money invariants: escrow-on-create, atomic accept + payout,
dice-tie refund, double-accept can't double-pay, creator-can't-accept,
cancel/expire refunds, and insufficient-funds short-circuits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.pvp import PvpEscrow, PvpOffer  # noqa: F401 — register tables
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.pvp_service import (
    PvpAcceptOutcome,
    PvpCreateOutcome,
    PvpService,
)
from telegram_invite_bot.utils.time import db_now

pytestmark = pytest.mark.asyncio


class _Rng:
    """Deterministic stand-in for ``random``: fixed coin side + dice rolls."""

    def __init__(self, *, choice: str = "heads", rolls: list[int] | None = None) -> None:
        self._choice = choice
        self._rolls = list(rolls or [])

    def choice(self, _seq: object) -> str:
        return self._choice

    def randint(self, _a: int, _b: int) -> int:
        return self._rolls.pop(0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _svc(session: AsyncSession) -> PvpService:
    return PvpService(EconomyRepo(session), TransactionsRepo(session), session)


async def _seed(session: AsyncSession, user_id: int, balance: int = 1_000) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _bal(session: AsyncSession, user_id: int) -> int | None:
    row = (
        await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
    ).scalar_one_or_none()
    return int(row) if row is not None else None


async def _offer_status(session: AsyncSession, offer_id: int) -> str:
    return (
        await session.execute(select(PvpOffer.status).where(PvpOffer.id == offer_id))
    ).scalar_one()


# --- create -----------------------------------------------------------------


async def test_create_escrows_creator_stake(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    res = await _svc(session).create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    assert res.outcome is PvpCreateOutcome.OK
    assert res.offer_id is not None
    assert await _bal(session, 1) == 900  # stake held in escrow
    held = (await session.execute(select(func.count()).select_from(PvpEscrow))).scalar_one()
    assert held == 1


async def test_create_rejects_out_of_range_bet(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    res = await _svc(session).create_offer(
        creator_id=1, game="dice", bet=5, side=None, chat_id=-100, now=db_now()
    )
    assert res.outcome is PvpCreateOutcome.INVALID_BET
    assert await _bal(session, 1) == 1_000  # untouched


async def test_create_insufficient_funds(session: AsyncSession) -> None:
    await _seed(session, 1, 50)
    res = await _svc(session).create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    assert res.outcome is PvpCreateOutcome.INSUFFICIENT_FUNDS
    assert await _bal(session, 1) == 50


# --- accept + resolve -------------------------------------------------------


async def test_coin_creator_wins_takes_pot(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(choice="heads"),  # matches creator's side → creator wins
    )
    assert res.outcome is PvpAcceptOutcome.SUCCESS
    assert res.winner_id == 1
    # T-020/R8: the pot is still 200, but the winner collects 190 of it
    # on top of the 900 they were left holding. The other 10 is burned.
    assert (res.payout, res.rake) == (190, 10)
    assert await _bal(session, 1) == 1_090
    assert await _bal(session, 2) == 900
    assert await _offer_status(session, created.offer_id) == "finished"


async def test_coin_opponent_wins(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(choice="tails"),  # opposite of creator's side → opponent wins
    )
    assert res.winner_id == 2
    assert (res.payout, res.rake) == (190, 10)
    assert await _bal(session, 1) == 900
    assert await _bal(session, 2) == 1_090


async def test_dice_tie_refunds_both(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[3, 3]),  # equal → tie
    )
    assert res.winner_id is None
    # A tie is a non-event — full refund both sides, no house cut.
    assert (res.payout, res.rake) == (0, 0)
    assert await _bal(session, 1) == 1_000  # stakes returned
    assert await _bal(session, 2) == 1_000


async def test_dice_creator_wins(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[6, 1]),
    )
    assert res.winner_id == 1
    assert (res.payout, res.rake) == (190, 10)
    assert await _bal(session, 1) == 1_090
    assert await _bal(session, 2) == 900


async def test_double_accept_cannot_double_pay(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    await _seed(session, 3, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    first = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(choice="heads"),
    )
    assert first.outcome is PvpAcceptOutcome.SUCCESS
    # A second opponent tapping Accept on the resolved card gets nothing.
    second = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=3,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(choice="heads"),
    )
    assert second.outcome is PvpAcceptOutcome.NOT_FOUND
    assert await _bal(session, 3) == 1_000  # never debited


async def test_creator_cannot_accept_own_offer(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id, opponent_id=1, chat_id=-100, now=db_now()
    )
    assert res.outcome is PvpAcceptOutcome.CREATOR_CANNOT_ACCEPT
    assert await _bal(session, 1) == 900  # still just the created hold


async def test_accept_from_another_chat_is_refused_but_leaves_the_offer(
    session: AsyncSession,
) -> None:
    """#1604: an offer belongs to the chat it was published in.

    The row has always carried ``chat_id``, but ``accept_and_resolve``
    never read it back, so a card forwarded into another chat stayed
    takeable there — Telegram keeps an inline keyboard live across a
    forward. The second half matters as much as the first: the refusal
    must not consume the offer, or an outsider could kill any
    challenge just by tapping a forwarded copy of it.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    # Narrowed once here rather than passed as ``created.offer_id``:
    # the field is ``int | None`` and every call below would other-
    # wise add a fresh mypy arg-type error to the file's tally.
    offer_id = created.offer_id
    assert offer_id is not None

    foreign = await svc.accept_and_resolve(
        offer_id=offer_id, opponent_id=2, chat_id=-200, now=db_now()
    )
    assert foreign.outcome is PvpAcceptOutcome.NOT_FOUND
    assert await _bal(session, 2) == 1_000  # never debited

    home = await svc.accept_and_resolve(
        offer_id=offer_id, opponent_id=2, chat_id=-100, now=db_now()
    )
    assert home.outcome is PvpAcceptOutcome.SUCCESS


# --- cancel + expire --------------------------------------------------------


async def test_cancel_refunds_creator(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    assert await svc.cancel(offer_id=created.offer_id, creator_id=1) is True
    assert await _bal(session, 1) == 1_000
    assert await _offer_status(session, created.offer_id) == "cancelled"


async def test_cancel_rejects_non_creator(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    assert await svc.cancel(offer_id=created.offer_id, creator_id=999) is False
    assert await _bal(session, 1) == 900  # hold not refunded


async def test_expire_refunds_creator(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    cutoff = db_now() + timedelta(minutes=30)
    retired = await svc.expire(offer_id=created.offer_id, cutoff=cutoff, now=db_now())
    assert retired is not None
    assert await _bal(session, 1) == 1_000
    assert await _offer_status(session, created.offer_id) == "expired"


# --- background expiry sweep -------------------------------------------------


async def test_sweep_expired_refunds_stale_offers(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    old = db_now() - timedelta(hours=1)
    created = await svc.create_offer(
        creator_id=1, game="coin", bet=100, side="heads", chat_id=-100, now=old
    )
    report = await svc.sweep_expired(db_now(), ttl_minutes=30)
    assert report.count == 1
    assert await _bal(session, 1) == 1_000  # stake refunded
    assert await _offer_status(session, created.offer_id) == "expired"


async def test_sweep_expired_leaves_fresh_offers(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    report = await svc.sweep_expired(db_now(), ttl_minutes=30)
    assert report.count == 0
    assert await _bal(session, 1) == 900  # still held
    assert await _offer_status(session, created.offer_id) == "pending"


async def test_sweep_expired_is_capped_per_pass(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1619: one pass refunds at most ``_EXPIRY_SCAN_LIMIT`` offers.

    The cap is patched down instead of seeding 200 offers: what is
    under test is that the constant is APPLIED, not its value. The
    residue is not lost — the next pass takes it, in ``id ASC`` order.
    """
    import telegram_invite_bot.services.pvp_service as mod

    monkeypatch.setattr(mod, "_EXPIRY_SCAN_LIMIT", 2)
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    old = db_now() - timedelta(hours=1)
    offers: list[int] = []
    for _ in range(3):
        created = await svc.create_offer(
            creator_id=1, game="coin", bet=100, side="heads", chat_id=-100, now=old
        )
        assert created.offer_id is not None
        offers.append(created.offer_id)
    assert await _bal(session, 1) == 700  # three stakes held

    assert (await svc.sweep_expired(db_now(), ttl_minutes=30)).count == 2
    assert await _bal(session, 1) == 900
    statuses = [await _offer_status(session, oid) for oid in offers]
    assert statuses == ["expired", "expired", "pending"]

    assert (await svc.sweep_expired(db_now(), ttl_minutes=30)).count == 1
    assert await _bal(session, 1) == 1_000  # every stake back, one pass later
    assert await _offer_status(session, offers[2]) == "expired"


# --- T-020/R8 house cut ------------------------------------------------------


async def _money_supply(session: AsyncSession) -> int:
    """Every coin held by every wallet. The number R8 is meant to shrink."""
    return int(
        (
            await session.execute(select(func.coalesce(func.sum(EconomyUser.balance), 0)))
        ).scalar_one()
    )


async def _rows_of_type(session: AsyncSession, row_type: str) -> list[Transaction]:
    return list(
        (await session.execute(select(Transaction).where(Transaction.type == row_type))).scalars()
    )


async def test_decided_game_burns_the_rake_out_of_the_supply(
    session: AsyncSession,
) -> None:
    """Coins actually LEAVE the ecosystem here. Legacy paid the full 200
    pot and left the supply flat, which is exactly what made the PvP
    stake games free money for a patient (or colluding) pair."""
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    supply_before = await _money_supply(session)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )
    await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(choice="heads"),
    )
    assert await _money_supply(session) == supply_before - 10


async def test_rake_row_is_attributed_to_neither_wallet(
    session: AsyncSession,
) -> None:
    """``from_id``/``to_id`` stay NULL so a per-user audit
    (``WHERE from_id = :uid OR to_id = :uid``) never double-counts the
    burn against the loser, whose stake their own ``pvp_hold`` row
    already accounts for in full."""
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[6, 1]),
    )
    rake_rows = await _rows_of_type(session, "pvp_rake")
    assert len(rake_rows) == 1
    assert rake_rows[0].amount == 10
    assert rake_rows[0].from_id is None
    assert rake_rows[0].to_id is None


async def test_tie_writes_no_rake_row(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    supply_before = await _money_supply(session)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[3, 3]),
    )
    assert await _rows_of_type(session, "pvp_rake") == []
    assert await _money_supply(session) == supply_before


# --- ledger ↔ wallet agreement ----------------------------------------------


async def _ledger_delta(session: AsyncSession, user_id: int) -> int:
    """What the ledger CLAIMS happened to ``user_id``'s balance.

    Reads the rows exactly the way the two consumers do —
    ``TransactionsRepo.window_stats`` (received = rows with ``to_id``,
    sent = rows with ``from_id``) and ``recent()`` (the signed lines on
    the ``/profile`` finances panel). Neither filters on ``type``, so
    every row this service writes lands in a user's numbers.
    """
    rows = list((await session.execute(select(Transaction))).scalars())
    delta = 0
    for row in rows:
        if row.to_id == user_id:
            delta += abs(int(row.amount))
        if row.from_id == user_id:
            delta -= abs(int(row.amount))
    return delta


async def test_ledger_matches_the_real_wallet_move_on_a_decided_game(
    session: AsyncSession,
) -> None:
    """Each seat's signed rows sum to what its wallet actually did.

    Two things used to break this: the payout row named the loser as
    the sender (so their 100-coin loss read as a 290-coin spend), and
    the opponent's stake was never ledgered at all.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[6, 1]),
    )

    assert await _bal(session, 1) == 1_090
    assert await _bal(session, 2) == 900
    assert await _ledger_delta(session, 1) == 90
    assert await _ledger_delta(session, 2) == -100


async def test_ledger_matches_the_real_wallet_move_on_a_tie(
    session: AsyncSession,
) -> None:
    """A draw moves no money, so it must sum to zero on both seats.

    The opponent's refund used to stand alone — no hold row anywhere
    against it — so a draw showed up as +bet of income from nowhere.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[3, 3]),
    )

    assert await _bal(session, 1) == 1_000
    assert await _bal(session, 2) == 1_000
    assert await _ledger_delta(session, 1) == 0
    assert await _ledger_delta(session, 2) == 0


# ── #263: the payout guard raises, and says whose coins ──────────────


async def test_payout_failure_raises_runtime_error_not_assertion(
    session: AsyncSession,
) -> None:
    """A vanished winner wallet at payout time raises ``RuntimeError``.

    Third of the three sites #263 converted. Same argument as the /duel
    and /cpc twins: the site was a bare ``assert credited is not None
    # noqa: S101``, the deployed systemd unit runs without ``-O`` in
    production, and a revert to it fails here
    on the exception type — or, under ``-O``, on nothing raising at all.

    The PvP flavour of the same reasoning: both stakes are sitting in
    ``pvp_escrow`` rows at this point, and only an exception unwinds the
    accept transaction that holds them.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now(),
    )

    assert created.offer_id is not None
    offer_id: int = created.offer_id

    async def _vanished(user_id: int, amount: int) -> None:
        return None

    svc._economy.credit = _vanished  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="pvp payout to winner failed"):
        await svc.accept_and_resolve(
            offer_id=offer_id,
            opponent_id=2,
            chat_id=-100,
            now=db_now(),
            rng=_Rng(choice="heads"),  # type: ignore[arg-type]  # creator wins
        )

    await session.rollback()
    assert await _bal(session, 1) == 1_000
    assert await _bal(session, 2) == 1_000


# --- #1766: the sweep must name what it expired --------------------------


async def test_sweep_expired_reports_each_offer_with_its_card(
    session: AsyncSession,
) -> None:
    """The refund is silent unless the caller learns which card to close.

    ``economy_cleanup`` is the only production caller and it lives in a
    different database from ``users``; it cannot re-read the offers the
    sweep just retired without a second query racing the next pass. So
    the pass reports them, exactly as ``P2pService.sweep`` already does.
    """
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    old = db_now() - timedelta(hours=1)
    created = await svc.create_offer(
        creator_id=1, game="coin", bet=100, side="heads", chat_id=-100, now=old
    )
    assert created.offer_id is not None
    await svc.set_offer_message(created.offer_id, -100, 777)

    report = await svc.sweep_expired(db_now(), ttl_minutes=30)

    assert report.count == 1
    (expired,) = report.expired
    assert expired.offer_id == created.offer_id
    assert expired.creator_id == 1
    assert expired.bet == 100
    assert expired.chat_id == -100
    assert expired.message_id == 777


async def test_sweep_expired_reports_an_offer_that_never_got_a_card(
    session: AsyncSession,
) -> None:
    """``message_id`` stays NULL when the card post failed.

    The refund still happened, so the offer belongs in the report — with
    ``None``, which is what tells the notifier there is nothing to edit.
    """
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="dice",
        bet=100,
        side=None,
        chat_id=-100,
        now=db_now() - timedelta(hours=1),
    )
    assert created.offer_id is not None

    report = await svc.sweep_expired(db_now(), ttl_minutes=30)

    assert report.count == 1
    assert report.expired[0].message_id is None


async def test_a_poisoned_offer_is_absent_from_the_report(
    session: AsyncSession,
) -> None:
    """#268's skip must also skip the notice.

    An offer whose refund did not apply stays ``pending``. Reporting it
    would edit a live card into "expired" while the stake is still held.
    """
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now() - timedelta(hours=1),
    )
    assert created.offer_id is not None
    await svc.set_offer_message(created.offer_id, -100, 555)

    async def _vanished(user_id: int, amount: int) -> None:
        return None

    svc._economy.release = _vanished  # type: ignore[method-assign]

    report = await svc.sweep_expired(db_now(), ttl_minutes=30)

    assert report.count == 0
    assert report.expired == ()
