"""Unit coverage for :func:`resolve_round` and friends (Stage 32).

The resolver is pure logic — no fixtures, no async, no I/O. Tests
exercise the 3x3 outcome matrix, the payout/net_delta arithmetic,
the tie-refund posture, the optional house-edge-on-tie knob, the
beats() transitivity, and the determinism of :func:`choose_random_move`.
"""

from __future__ import annotations

import random

import pytest

from telegram_invite_bot.games.rps import (
    RpsConfig,
    RpsMove,
    RpsOutcome,
    RpsRoundResult,
    choose_random_move,
    resolve_round,
)

# ----------------------------------------------------------------------
# 3x3 outcome matrix — exhaustive
# ----------------------------------------------------------------------
# Mirrors legacy ``WIN_MATRIX`` at rock_paper_scissors.py:48. Listed as
# (challenger, opponent, expected_outcome) so a future maintainer can
# eyeball the table against the legacy dict without mental translation.
_MATRIX: list[tuple[RpsMove, RpsMove, RpsOutcome]] = [
    (RpsMove.ROCK, RpsMove.ROCK, RpsOutcome.TIE),
    (RpsMove.ROCK, RpsMove.PAPER, RpsOutcome.OPPONENT_WIN),
    (RpsMove.ROCK, RpsMove.SCISSORS, RpsOutcome.CHALLENGER_WIN),
    (RpsMove.PAPER, RpsMove.ROCK, RpsOutcome.CHALLENGER_WIN),
    (RpsMove.PAPER, RpsMove.PAPER, RpsOutcome.TIE),
    (RpsMove.PAPER, RpsMove.SCISSORS, RpsOutcome.OPPONENT_WIN),
    (RpsMove.SCISSORS, RpsMove.ROCK, RpsOutcome.OPPONENT_WIN),
    (RpsMove.SCISSORS, RpsMove.PAPER, RpsOutcome.CHALLENGER_WIN),
    (RpsMove.SCISSORS, RpsMove.SCISSORS, RpsOutcome.TIE),
]


@pytest.mark.parametrize(("challenger", "opponent", "expected"), _MATRIX)
def test_resolve_round_outcome_matrix(
    challenger: RpsMove, opponent: RpsMove, expected: RpsOutcome
) -> None:
    """Every (challenger, opponent) pair resolves to the legacy verdict."""
    result = resolve_round(challenger, opponent, bet=100)
    assert result.outcome is expected
    assert result.challenger_move is challenger
    assert result.opponent_move is opponent
    assert result.bet == 100


# ----------------------------------------------------------------------
# beats() — transitivity + reflexivity (rock_paper_scissors.py:48)
# ----------------------------------------------------------------------


def test_beats_transitivity() -> None:
    """The legacy cycle: rock>scissors>paper>rock, none of them beats itself."""
    assert RpsMove.ROCK.beats(RpsMove.SCISSORS)
    assert RpsMove.SCISSORS.beats(RpsMove.PAPER)
    assert RpsMove.PAPER.beats(RpsMove.ROCK)


def test_beats_reverse_is_false() -> None:
    """The losing side never beats the winning side — sanity guard."""
    assert not RpsMove.SCISSORS.beats(RpsMove.ROCK)
    assert not RpsMove.PAPER.beats(RpsMove.SCISSORS)
    assert not RpsMove.ROCK.beats(RpsMove.PAPER)


@pytest.mark.parametrize("move", list(RpsMove))
def test_beats_self_is_false(move: RpsMove) -> None:
    """A move never beats itself; tie is signalled via outcome, not beats()."""
    assert not move.beats(move)


# ----------------------------------------------------------------------
# Payout arithmetic — legacy rock_paper_scissors.py:541 paid the whole
# ``bet * 2`` pot; T-020/R8 pays ``int(bet * 1.9)`` and burns the rest.
# ----------------------------------------------------------------------


def test_win_pays_the_pot_minus_the_house_cut() -> None:
    """CHALLENGER_WIN: payout=int(bet*1.9), net_delta=payout-bet."""
    result = resolve_round(RpsMove.ROCK, RpsMove.SCISSORS, bet=100)
    assert result.outcome is RpsOutcome.CHALLENGER_WIN
    assert result.payout == 190
    assert result.net_delta == 90
    assert result.rake == 10


def test_loss_carries_the_same_payout_and_negative_net_delta() -> None:
    """OPPONENT_WIN from challenger seat: payout is what the OPPONENT
    collects; the challenger's net_delta is -bet (legacy
    remove_coins(loser, bet)), and the rake is the same either way."""
    result = resolve_round(RpsMove.ROCK, RpsMove.PAPER, bet=100)
    assert result.outcome is RpsOutcome.OPPONENT_WIN
    assert result.payout == 190
    assert result.net_delta == -100
    assert result.rake == 10


def test_win_payout_at_min_bet() -> None:
    """Pin the arithmetic at the legacy min_bet=10 boundary."""
    result = resolve_round(RpsMove.PAPER, RpsMove.ROCK, bet=10)
    assert result.payout == 19
    assert result.net_delta == 9
    assert result.rake == 1


def test_win_payout_at_max_bet() -> None:
    """Pin the arithmetic at the configured ceiling. T-020/R9 lowered
    that from legacy's ``cpc_max_bet = 100000`` to the shared 10 000 —
    the ceiling itself is pinned in ``tests/unit/games/test_limits.py``,
    so the literals here stay literal. The resolver is unbounded (bet
    validation is a service concern), and the no-mint sweep below still
    runs a 100 000 bet to catch a hypothetical 32-bit overflow."""
    result = resolve_round(RpsMove.SCISSORS, RpsMove.PAPER, bet=10_000)
    assert result.payout == 19_000
    assert result.net_delta == 9_000
    assert result.rake == 1_000


def test_payout_never_exceeds_the_escrowed_pot() -> None:
    """The load-bearing no-mint property of the ``int()`` FLOOR: only
    ``2 * bet`` was ever escrowed, so paying more would create coins.

    Swept across bets whose 1.9x lands on a fraction (13 -> 24.7,
    17 -> 32.3, ...) — exactly the cases a round-to-nearest would push
    over the line.
    """
    for bet in (10, 11, 13, 15, 17, 33, 99, 101, 999, 100_000):
        result = resolve_round(RpsMove.ROCK, RpsMove.SCISSORS, bet=bet)
        assert result.payout <= bet * 2
        assert result.payout + result.rake == bet * 2
        assert result.rake >= 0
        assert isinstance(result.payout, int)
        assert isinstance(result.rake, int)


def test_the_game_is_not_zero_edge() -> None:
    """Pin the INEQUALITY, not just the literal: the edge can be
    re-tuned without touching this test, but a silent return to
    legacy's zero-edge ``bet * 2`` fails loudly."""
    config = RpsConfig()
    assert config.payout_multiplier < 2, "a 2x payout on a 2x pot is a zero-edge game"
    assert 0.9 <= config.payout_multiplier / 2 < 1.0


# ----------------------------------------------------------------------
# Tie posture — rock_paper_scissors.py:546-547
# ----------------------------------------------------------------------


def test_tie_refunds_stake_by_default() -> None:
    """Legacy posture, kept by T-020/R8: payout=bet (refund),
    net_delta=0, and NO rake — a tie is a non-event, and taking a cut
    of it would punish players for an outcome neither of them chose."""
    result = resolve_round(RpsMove.ROCK, RpsMove.ROCK, bet=500)
    assert result.outcome is RpsOutcome.TIE
    assert result.payout == 500
    assert result.net_delta == 0
    assert result.rake == 0


def test_tie_with_house_edge_forfeits_stake() -> None:
    """Optional house_edge_on_tie=True: payout=0, net_delta=-bet, and
    the whole pot becomes rake. Not used by legacy or by the current
    config; covered so the knob stays safe to flip."""
    result = resolve_round(RpsMove.PAPER, RpsMove.PAPER, bet=500, house_edge_on_tie=True)
    assert result.outcome is RpsOutcome.TIE
    assert result.payout == 0
    assert result.net_delta == -500
    assert result.rake == 1_000


# ----------------------------------------------------------------------
# RpsRoundResult — frozen + carries both moves
# ----------------------------------------------------------------------


def test_result_is_frozen() -> None:
    """Dataclass is frozen so the service can't accidentally mutate it
    between resolving and writing to EconomyRepo."""
    result = resolve_round(RpsMove.ROCK, RpsMove.SCISSORS, bet=10)
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        result.payout = 999  # type: ignore[misc]


def test_result_carries_both_moves_verbatim() -> None:
    """The result echoes the inputs so the eventual handler can render
    "👤 A — ✊ / 👤 B — ✋" without re-deriving from session state."""
    result = resolve_round(RpsMove.PAPER, RpsMove.ROCK, bet=42)
    assert result.challenger_move is RpsMove.PAPER
    assert result.opponent_move is RpsMove.ROCK
    assert result.bet == 42


# ----------------------------------------------------------------------
# Determinism — two calls = equal results
# ----------------------------------------------------------------------


def test_resolve_round_is_deterministic() -> None:
    """Pure function: same inputs → equal RpsRoundResult."""
    a = resolve_round(RpsMove.ROCK, RpsMove.PAPER, bet=77)
    b = resolve_round(RpsMove.ROCK, RpsMove.PAPER, bet=77)
    assert a == b


# ----------------------------------------------------------------------
# RpsConfig — pinned to legacy defaults at rock_paper_scissors.py:664-665
# ----------------------------------------------------------------------


def test_config_defaults_pin_legacy_bounds() -> None:
    """Defaults matched ``register_cpc_handlers`` exactly so Stage 33's
    service did not need to rediscover the legacy bounds. Two of the
    three have since moved on purpose — see the notes below."""
    config = RpsConfig()
    assert config.min_bet == 10
    # NOT legacy's 100000 — T-020/R9. That ceiling was ten times every
    # sibling game's; /cpc is the same two-seat pot as /duel, so the
    # gap bought nothing but a command where one round could swing
    # 190 000 COM. tests/unit/games/test_limits.py owns the sweep.
    assert config.max_bet == 10_000
    # NOT legacy's 2 — T-020/R8 took a ~5% rake. The inequality that
    # matters lives in test_the_game_is_not_zero_edge.
    assert config.payout_multiplier == 1.9


# ----------------------------------------------------------------------
# choose_random_move — seeded determinism
# ----------------------------------------------------------------------


def test_choose_random_move_is_deterministic_under_seed() -> None:
    """Two Random(seed) instances with the same seed pick the same move —
    pins the function as RNG-injected and side-effect-free on imports."""
    a = choose_random_move(rng=random.Random(0))
    b = choose_random_move(rng=random.Random(0))
    assert a is b
    assert isinstance(a, RpsMove)


def test_choose_random_move_seed_actually_varies() -> None:
    """Some other seed picks a different move — guards against a
    degenerate "always returns ROCK" regression masquerading as
    deterministic."""
    samples = {choose_random_move(rng=random.Random(seed)) for seed in range(20)}
    # Across 20 seeds we should hit at least two distinct moves; if the
    # function silently always returns the same value, this fails loudly.
    assert len(samples) >= 2


def test_choose_random_move_returns_only_legal_moves() -> None:
    """50 seeded picks must all be one of the three legal moves —
    catches a hypothetical "rng.choice over the wrong sequence" bug."""
    legal = set(RpsMove)
    for seed in range(50):
        assert choose_random_move(rng=random.Random(seed)) in legal


# ----------------------------------------------------------------------
# Result type sanity
# ----------------------------------------------------------------------


def test_resolve_round_returns_rps_round_result() -> None:
    """Pin the return type — protects against a refactor that
    accidentally returns a tuple or dict from the service stage."""
    result = resolve_round(RpsMove.ROCK, RpsMove.SCISSORS, bet=1)
    assert isinstance(result, RpsRoundResult)
