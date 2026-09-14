"""Unit tests for the pure ``/duel`` resolver.

Twin of :mod:`tests.unit.games.test_rps`. Until T-020/R8 the duel
resolver had no unit coverage at all — it was exercised only through
:class:`DuelService` and the e2e handler flow, both of which pin the
*settled* balances rather than the arithmetic that produces them. The
house-edge change is exactly the kind of edit that wants a test at the
level where the number is computed, so the gap is closed here.
"""

from __future__ import annotations

import random

import pytest

from telegram_invite_bot.games.duel import (
    DuelConfig,
    DuelOutcome,
    DuelRoundResult,
    resolve_round,
    roll_die,
)

# ----------------------------------------------------------------------
# Outcome selection — higher roll wins, equal rolls tie
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("challenger_roll", "opponent_roll", "expected"),
    [
        (6, 1, DuelOutcome.CHALLENGER_WIN),
        (2, 5, DuelOutcome.OPPONENT_WIN),
        (4, 4, DuelOutcome.TIE),
        (1, 6, DuelOutcome.OPPONENT_WIN),
        (6, 5, DuelOutcome.CHALLENGER_WIN),
    ],
)
def test_outcome_follows_the_higher_roll(
    challenger_roll: int, opponent_roll: int, expected: DuelOutcome
) -> None:
    result = resolve_round(challenger_roll, opponent_roll, bet=100)
    assert result.outcome is expected


# ----------------------------------------------------------------------
# Payout arithmetic — legacy bot.py paid the whole ``bet * 2`` pot;
# T-020/R8 pays ``int(bet * 1.9)`` and burns the rest.
# ----------------------------------------------------------------------


def test_win_pays_the_pot_minus_the_house_cut() -> None:
    result = resolve_round(6, 1, bet=100)
    assert result.outcome is DuelOutcome.CHALLENGER_WIN
    assert result.payout == 190
    assert result.net_delta == 90
    assert result.rake == 10


def test_loss_carries_the_same_payout_and_negative_net_delta() -> None:
    """``payout`` is what the WINNER collects regardless of seat; the
    challenger's ``net_delta`` is ``-bet`` when they lose."""
    result = resolve_round(2, 5, bet=100)
    assert result.outcome is DuelOutcome.OPPONENT_WIN
    assert result.payout == 190
    assert result.net_delta == -100
    assert result.rake == 10


def test_win_payout_at_bet_bounds() -> None:
    """Pin the arithmetic at both legacy bet bounds (10 / 10000)."""
    at_min = resolve_round(6, 1, bet=10)
    assert (at_min.payout, at_min.net_delta, at_min.rake) == (19, 9, 1)
    at_max = resolve_round(6, 1, bet=10_000)
    assert (at_max.payout, at_max.net_delta, at_max.rake) == (19_000, 9_000, 1_000)


def test_payout_never_exceeds_the_escrowed_pot() -> None:
    """The load-bearing no-mint property of the ``int()`` FLOOR: only
    ``2 * bet`` was ever escrowed, so paying more would create coins.

    Swept across bets whose 1.9x lands on a fraction (13 -> 24.7,
    17 -> 32.3, ...) — exactly the cases a round-to-nearest would push
    over the line.
    """
    for bet in (10, 11, 13, 15, 17, 33, 99, 101, 999, 10_000):
        result = resolve_round(6, 1, bet=bet)
        assert result.payout <= bet * 2
        assert result.payout + result.rake == bet * 2
        assert result.rake >= 0
        assert isinstance(result.payout, int)
        assert isinstance(result.rake, int)


def test_the_game_is_not_zero_edge() -> None:
    """Pin the INEQUALITY, not just the literal: the edge can be
    re-tuned without touching this test, but a silent return to
    legacy's zero-edge ``bet * 2`` fails loudly."""
    config = DuelConfig()
    assert config.payout_multiplier < 2, "a 2x payout on a 2x pot is a zero-edge game"
    assert 0.9 <= config.payout_multiplier / 2 < 1.0


def test_duel_and_rps_carry_the_same_edge() -> None:
    """Both PvP games are the same product from a player's seat; a
    drift between them would be an arbitrage, not a feature."""
    from telegram_invite_bot.games.rps import RpsConfig

    assert DuelConfig().payout_multiplier == RpsConfig().payout_multiplier


# ----------------------------------------------------------------------
# Tie posture — refund both, no rake
# ----------------------------------------------------------------------


def test_tie_refunds_both_stakes_and_takes_no_rake() -> None:
    """A tie is a non-event: both stakes go straight back and the house
    takes nothing. Taking a cut here would charge players for an outcome
    neither of them chose."""
    result = resolve_round(4, 4, bet=500)
    assert result.outcome is DuelOutcome.TIE
    assert result.payout == 500  # per-player refund
    assert result.net_delta == 0
    assert result.rake == 0


# ----------------------------------------------------------------------
# Result shape + purity
# ----------------------------------------------------------------------


def test_result_is_frozen() -> None:
    result = resolve_round(6, 1, bet=10)
    with pytest.raises(AttributeError):
        result.payout = 999  # type: ignore[misc]


def test_result_echoes_its_inputs() -> None:
    result = resolve_round(3, 5, bet=42)
    assert result.challenger_roll == 3
    assert result.opponent_roll == 5
    assert result.bet == 42
    assert isinstance(result, DuelRoundResult)


def test_resolve_round_pins_the_arithmetic_of_one_win() -> None:
    """#1981. This used to read ``resolve_round(6, 2, bet=77) ==
    resolve_round(6, 2, bet=77)`` under the name "is deterministic".
    Both sides were the same expression, so the assertion held for any
    implementation at all — including one replaced by a constant, which
    is how it was checked. Determinism is not a property a test can
    reach here anyway: the function is pure and takes the rolls as
    arguments, so it is a property of the signature.

    What is worth pinning is the odd-bet arithmetic, where the floor in
    :func:`split_pot` decides who keeps the stray coin. 77 at 1.9 pays
    146 out of a 154 pot; the missing 8 is burned rake, not change owed
    to a player.
    """
    result = resolve_round(6, 2, bet=77)

    assert result.outcome is DuelOutcome.CHALLENGER_WIN
    assert result.payout == 146
    assert result.net_delta == 69
    assert result.rake == 8
    assert result.payout + result.rake == 2 * result.bet


# ----------------------------------------------------------------------
# Config + die
# ----------------------------------------------------------------------


def test_config_defaults_pin_legacy_bet_bounds() -> None:
    config = DuelConfig()
    assert config.min_bet == 10
    assert config.max_bet == 10_000
    assert config.dice_faces == 6
    assert config.max_wins_cap == 5
    # NOT legacy's 2 — T-020/R8 took a ~5% rake.
    assert config.payout_multiplier == 1.9


def test_roll_die_is_in_range_and_seeded_deterministic() -> None:
    rolls = [roll_die(rng=random.Random(1234)) for _ in range(20)]
    assert all(1 <= r <= 6 for r in rolls)
    assert len(set(rolls)) == 1, "same seed, same first draw"
