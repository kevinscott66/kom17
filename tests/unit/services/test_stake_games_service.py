"""Unit matrix for :class:`StakeGamesService` (L-17).

Pins the money rules over a fake economy repo:

* dice: correct 1-6 guess pays ``bet × 5.7`` GROSS
  (``DICE_MULTIPLIER``); net win ``+4.7×bet``; loss ``−bet``.
* flip: correct орёл/решка call pays ``bet × 1.9`` GROSS
  (``FLIP_MULTIPLIER``); net win ``+0.9×bet``; loss ``−bet``.
* the two multipliers sit BELOW the fair odds (6 and 2) on purpose —
  T-020/R7 gave both games the ~5% house edge /roulette has carried
  since A-10. A test below pins that inequality directly, so restoring
  legacy parity here cannot pass silently.
* validate() is read-only and rejects bounds / missing wallet /
  unaffordable bets BEFORE any animation or debit.
* settle() debits atomically first; a lost debit race refuses with the
  fresh balance and writes NO games row.
* a payout credit the wallet rejects (balance cap) is fatal: the
  service raises so the caller's transaction undoes the whole play,
  rather than announcing a win it could not pay (#1560, mirrors
  /roulette and PvpService).
* every settled play records exactly one A-11 games row with the
  signed net profit and the verbatim detail blob.
* every settled play also books its ledger rows (#225): one
  ``<game>_stake`` debit row always, plus a ``<game>_win`` credit row
  that is only reached once the payout credit has landed.

The fake repo implements only the four methods the service touches
(``get`` / ``debit`` / ``credit`` / ``record_game``) with the same
None-on-failure contract as :class:`EconomyRepo`; the real SQL flows
are covered by the /roll & /flip e2e tests + the economy repo
integration suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import pytest

from telegram_invite_bot.services.stake_games_service import (
    DICE_MAX_BET,
    DICE_MIN_BET,
    DICE_MULTIPLIER,
    FLIP_MULTIPLIER,
    StakeGamesService,
    StakeOutcome,
)


@dataclass
class _Wallet:
    balance: int


class _FakeEconomyRepo:
    """EconomyRepo stand-in: dict wallet + None-on-failure mutators."""

    def __init__(self, *, balance: int | None = None, credit_fails: bool = False) -> None:
        self.wallet = _Wallet(balance) if balance is not None else None
        self.credit_fails = credit_fails
        self.recorded: list[dict[str, Any]] = []
        self.awarded_on_record: list[str] = []
        # When set, the first debit call drops the balance below the
        # stake BEFORE debiting — simulates losing the TOCTOU race.
        self.drain_before_debit = False

    async def get(self, user_id: int) -> _Wallet | None:  # noqa: ARG002
        return self.wallet

    async def debit(self, user_id: int, amount: int) -> _Wallet | None:  # noqa: ARG002
        if self.wallet is None:
            return None
        if self.drain_before_debit:
            self.wallet.balance = 0
            self.drain_before_debit = False
        if self.wallet.balance < amount:
            return None
        self.wallet.balance -= amount
        return _Wallet(self.wallet.balance)

    async def credit(self, user_id: int, amount: int) -> _Wallet | None:  # noqa: ARG002
        if self.wallet is None or self.credit_fails:
            return None
        self.wallet.balance += amount
        return _Wallet(self.wallet.balance)

    async def record_game(self, user_id: int, **kwargs: Any) -> list[str]:
        self.recorded.append({"user_id": user_id, **kwargs})
        return list(self.awarded_on_record)


class _FakeLedger:
    """TransactionsRepo stand-in: appends the row kwargs, nothing else."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def _service(repo: _FakeEconomyRepo, ledger: _FakeLedger | None = None) -> StakeGamesService:
    # The fakes satisfy the structural slice the service uses; the cast
    # keeps mypy --strict quiet without a Protocol just for tests.
    return StakeGamesService(cast("Any", repo), cast("Any", ledger or _FakeLedger()))


# ── validate(): read-only rejections ─────────────────────────────────


async def test_validate_below_min_bet() -> None:
    repo = _FakeEconomyRepo(balance=1_000)
    rejection = await _service(repo).validate(
        user_id=1, bet=DICE_MIN_BET - 1, min_bet=DICE_MIN_BET, max_bet=DICE_MAX_BET
    )
    assert rejection is not None
    assert rejection.outcome is StakeOutcome.BELOW_MIN_BET
    assert repo.wallet is not None and repo.wallet.balance == 1_000  # untouched


async def test_validate_above_max_bet() -> None:
    repo = _FakeEconomyRepo(balance=10**9)
    rejection = await _service(repo).validate(
        user_id=1, bet=DICE_MAX_BET + 1, min_bet=DICE_MIN_BET, max_bet=DICE_MAX_BET
    )
    assert rejection is not None
    assert rejection.outcome is StakeOutcome.ABOVE_MAX_BET


async def test_validate_no_wallet() -> None:
    repo = _FakeEconomyRepo(balance=None)
    rejection = await _service(repo).validate(user_id=1, bet=100, min_bet=10, max_bet=10_000)
    assert rejection is not None
    assert rejection.outcome is StakeOutcome.NO_WALLET


async def test_validate_insufficient_carries_balance() -> None:
    repo = _FakeEconomyRepo(balance=50)
    rejection = await _service(repo).validate(user_id=1, bet=100, min_bet=10, max_bet=10_000)
    assert rejection is not None
    assert rejection.outcome is StakeOutcome.INSUFFICIENT_FUNDS
    assert rejection.balance == 50  # legacy showed the balance in the refusal


async def test_validate_ok_returns_none_and_writes_nothing() -> None:
    repo = _FakeEconomyRepo(balance=1_000)
    assert (await _service(repo).validate(user_id=1, bet=100, min_bet=10, max_bet=10_000)) is None
    assert repo.recorded == []
    assert repo.wallet is not None and repo.wallet.balance == 1_000


# ── settle(): dice money rules ───────────────────────────────────────


async def test_dice_win_pays_gross_below_fair_odds() -> None:
    repo = _FakeEconomyRepo(balance=1_000)
    result = await _service(repo).settle(
        user_id=1,
        bet=100,
        game="dice",
        won=True,
        multiplier=DICE_MULTIPLIER,
        detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
    )
    assert result.outcome is StakeOutcome.SUCCESS
    assert result.won is True
    assert result.payout == 570  # int(round(bet × 5.7))
    # 1000 − 100 (stake) + 570 (gross payout) = 1470 → net +470 = 4.7×bet.
    assert result.balance == 1_470
    [row] = repo.recorded
    assert row["game"] == "dice"
    assert row["bet"] == 100
    assert row["won"] is True
    assert row["profit"] == 470  # signed NET P&L
    assert json.loads(row["result"]) == {"roll": 4, "guess": 4, "multiplier": 5.7}


async def test_dice_lose_burns_stake_only() -> None:
    repo = _FakeEconomyRepo(balance=1_000)
    result = await _service(repo).settle(
        user_id=1,
        bet=100,
        game="dice",
        won=False,
        multiplier=DICE_MULTIPLIER,
        detail={"roll": 2, "guess": 4, "multiplier": DICE_MULTIPLIER},
    )
    assert result.outcome is StakeOutcome.SUCCESS
    assert result.won is False
    assert result.payout == 0
    assert result.balance == 900  # −bet, no credit ever issued
    [row] = repo.recorded
    assert row["won"] is False
    assert row["profit"] == -100


# ── settle(): flip money rules ───────────────────────────────────────


async def test_flip_win_pays_gross_below_fair_odds() -> None:
    repo = _FakeEconomyRepo(balance=500)
    result = await _service(repo).settle(
        user_id=1,
        bet=50,
        game="flip",
        won=True,
        multiplier=FLIP_MULTIPLIER,
        detail={"side": "орёл", "guess": "орёл", "multiplier": FLIP_MULTIPLIER},
    )
    assert result.payout == 95  # int(round(bet × 1.9))
    assert result.balance == 545  # 500 − 50 + 95 → net +0.9×bet
    [row] = repo.recorded
    assert row["game"] == "flip"
    assert row["profit"] == 45
    assert json.loads(row["result"])["side"] == "орёл"


async def test_flip_lose_burns_stake_only() -> None:
    repo = _FakeEconomyRepo(balance=500)
    result = await _service(repo).settle(
        user_id=1,
        bet=50,
        game="flip",
        won=False,
        multiplier=FLIP_MULTIPLIER,
        detail={"side": "решка", "guess": "орёл", "multiplier": FLIP_MULTIPLIER},
    )
    assert result.payout == 0
    assert result.balance == 450
    [row] = repo.recorded
    assert row["profit"] == -50


# ── the house edge itself (T-020/R7) ─────────────────────────────────


async def test_both_games_pay_less_than_fair_odds() -> None:
    """The multipliers must stay BELOW the reciprocal of the win chance.

    Dice wins 1 time in 6 and flip 1 in 2, so paying 6× / 2× would make
    expected return exactly 1.0 — a game the house cannot win and a
    patient script farms for free. Every coin paid out here is one the
    owner has to honour at /withdraw, so this inequality is an economic
    invariant, not a tuning preference. Asserting the *shape* rather
    than the literals leaves room to re-tune the edge without editing
    this test, while still failing loudly on a return to legacy parity.
    """
    assert DICE_MULTIPLIER < 6, "dice would be zero-edge at 6×"
    assert FLIP_MULTIPLIER < 2, "flip would be zero-edge at 2×"
    # ...and not so steep the games stop being worth playing: both keep
    # an expected return of at least 0.9 per coin staked.
    assert 0.9 <= DICE_MULTIPLIER / 6 < 1.0
    assert 0.9 <= FLIP_MULTIPLIER / 2 < 1.0


async def test_payout_rounds_to_a_whole_coin() -> None:
    """Balances are integers; a fractional gross payout must not leak.

    ``DICE_MIN_BET`` is 10, so 5.7× lands on .0 for round bets — but a
    bet of 15 gives 85.5, and the credit path must hand the repo an
    ``int``. (A float would poison the SQL balance column.)
    """
    repo = _FakeEconomyRepo(balance=1_000)
    result = await _service(repo).settle(
        user_id=1,
        bet=15,
        game="dice",
        won=True,
        multiplier=DICE_MULTIPLIER,
        detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
    )
    # 85.5 → 86: Python's round() is banker's rounding, so an exact .5
    # goes to the even neighbour and may land on either side. The half
    # coin is noise against a 10-coin floor; what must hold is that the
    # wallet only ever sees an int.
    assert result.payout == 86
    assert isinstance(result.payout, int)
    assert result.balance == 1_071  # 1000 − 15 + 86
    [row] = repo.recorded
    assert isinstance(row["profit"], int)
    assert row["profit"] == 71


# ── settle(): failure posture ────────────────────────────────────────


async def test_settle_lost_debit_race_refuses_without_games_row() -> None:
    """A wallet drained between validate() and the atomic debit must
    collapse to INSUFFICIENT_FUNDS with the FRESH balance and leave no
    games row (the play never happened).
    """
    repo = _FakeEconomyRepo(balance=1_000)
    repo.drain_before_debit = True
    result = await _service(repo).settle(
        user_id=1,
        bet=100,
        game="dice",
        won=True,
        multiplier=DICE_MULTIPLIER,
        detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
    )
    assert result.outcome is StakeOutcome.INSUFFICIENT_FUNDS
    assert result.balance == 0  # re-read, race-faithful
    assert repo.recorded == []


async def test_settle_raises_when_the_payout_credit_is_rejected() -> None:
    """#1560: a rejected payout credit is fatal, not a logged shrug.

    ``credit`` returns None only at the balance cap, or if the wallet
    row went away mid-play — either way the payout did not land. The
    service used to return SUCCESS anyway, so the card read "you won
    570" directly above a balance 100 LOWER than before the game and a
    ``games`` row booked +470 profit the wallet never received.

    Raising hands the whole play to the caller's transaction to undo,
    which is the posture ``PvpService`` already took on this same edge.
    """
    repo = _FakeEconomyRepo(balance=1_000, credit_fails=True)
    with pytest.raises(RuntimeError, match="dice payout credit failed"):
        await _service(repo).settle(
            user_id=1,
            bet=100,
            game="dice",
            won=True,
            multiplier=DICE_MULTIPLIER,
            detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
        )
    # The fake has no transaction to roll back, so this asserts the
    # service itself never got as far as booking the play.
    assert repo.recorded == []


async def test_settle_surfaces_awarded_achievements() -> None:
    repo = _FakeEconomyRepo(balance=1_000)
    repo.awarded_on_record = ["first_win"]
    result = await _service(repo).settle(
        user_id=1,
        bet=100,
        game="flip",
        won=True,
        multiplier=FLIP_MULTIPLIER,
        detail={"side": "орёл", "guess": "орёл", "multiplier": FLIP_MULTIPLIER},
    )
    assert result.awarded == ["first_win"]


# ── #225: ledger rows ────────────────────────────────────────────────


async def test_settle_win_books_stake_and_win_ledger_rows() -> None:
    """A won play writes both halves: the stake leaves the wallet with
    no counterparty (the house is not a wallet — same shape /duel uses),
    the payout arrives with no payer. Without these rows the play was
    invisible to /balance's weekly cashflow.
    """
    repo = _FakeEconomyRepo(balance=1_000)
    ledger = _FakeLedger()
    await _service(repo, ledger).settle(
        user_id=7,
        bet=100,
        game="dice",
        won=True,
        multiplier=DICE_MULTIPLIER,
        detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
    )
    assert ledger.rows == [
        {
            "from_id": 7,
            "to_id": None,
            "amount": 100,
            "reason": "dice stake",
            "type": "dice_stake",
        },
        {
            "from_id": None,
            "to_id": 7,
            "amount": 570,
            "reason": "dice win",
            "type": "dice_win",
        },
    ]


async def test_settle_loss_books_only_the_stake_row() -> None:
    repo = _FakeEconomyRepo(balance=1_000)
    ledger = _FakeLedger()
    await _service(repo, ledger).settle(
        user_id=7,
        bet=100,
        game="flip",
        won=False,
        multiplier=FLIP_MULTIPLIER,
        detail={"side": "орёл", "guess": "решка", "multiplier": FLIP_MULTIPLIER},
    )
    assert [row["type"] for row in ledger.rows] == ["flip_stake"]


async def test_settle_refused_debit_books_nothing() -> None:
    """The wallet never moved, so neither may the ledger."""
    repo = _FakeEconomyRepo(balance=1_000)
    repo.drain_before_debit = True
    ledger = _FakeLedger()
    await _service(repo, ledger).settle(
        user_id=7,
        bet=100,
        game="dice",
        won=True,
        multiplier=DICE_MULTIPLIER,
        detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
    )
    assert ledger.rows == []


async def test_settle_books_no_win_row_when_the_payout_credit_is_rejected() -> None:
    """#1560 on the ledger side: the raise lands before the ``*_win``
    row, so the only row the service ever wrote is the stake debit —
    and the caller's rollback takes that one with it too.
    """
    repo = _FakeEconomyRepo(balance=1_000, credit_fails=True)
    ledger = _FakeLedger()
    with pytest.raises(RuntimeError, match="dice payout credit failed"):
        await _service(repo, ledger).settle(
            user_id=7,
            bet=100,
            game="dice",
            won=True,
            multiplier=DICE_MULTIPLIER,
            detail={"roll": 4, "guess": 4, "multiplier": DICE_MULTIPLIER},
        )
    assert [row["type"] for row in ledger.rows] == ["dice_stake"]
