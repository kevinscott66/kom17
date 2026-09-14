"""Unit tests for the shared PvP pot split (T-020/R8).

Three games settle a two-seat ``2 × bet`` pot — ``/duel``, ``/cpc`` and
the ``/pvp_*`` stake games. They used to each carry their own copy of
the payout arithmetic, which is exactly the shape that drifts: a player
would simply move to whichever command paid best. These tests pin the
one implementation and pin that all three still agree with it.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.games.pot import PVP_PAYOUT_MULTIPLIER, split_pot

# ----------------------------------------------------------------------
# The no-mint property — the whole reason this floors instead of rounds
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bet",
    [10, 11, 13, 15, 17, 33, 99, 101, 999, 1_001, 10_000, 100_000],
)
def test_the_split_never_pays_more_than_the_pot_holds(bet: int) -> None:
    """Only ``2 * bet`` was ever escrowed. Paying a coin more than that
    would mint it out of nothing, so the floor is load-bearing — the
    fractional bets here are the ones a round-to-nearest would push
    over the line."""
    payout, rake = split_pot(bet)
    assert payout + rake == bet * 2, "the split must be exhaustive"
    assert payout <= bet * 2
    assert rake >= 0
    assert isinstance(payout, int)
    assert isinstance(rake, int)


def test_the_rounding_error_always_favours_the_house() -> None:
    """A floor can only ever move coins from the player to the burn, and
    by at most one coin. The opposite direction would be a leak."""
    for bet in range(10, 400):
        payout, _ = split_pot(bet)
        exact = bet * PVP_PAYOUT_MULTIPLIER
        assert payout <= exact
        assert exact - payout < 1


# ----------------------------------------------------------------------
# The edge itself
# ----------------------------------------------------------------------


def test_the_pot_is_not_zero_edge() -> None:
    """Pin the INEQUALITY, not just the literal: the edge can be
    re-tuned without touching this test, but a silent return to
    legacy's zero-edge ``bet * 2`` fails loudly."""
    assert PVP_PAYOUT_MULTIPLIER < 2, "a 2x payout on a 2x pot is a zero-edge game"
    assert 0.9 <= PVP_PAYOUT_MULTIPLIER / 2 < 1.0, "edge should stay in the ~5% band"


def test_a_restored_two_times_multiplier_takes_nothing() -> None:
    """The zero-rake case is not a crash and not a negative — the
    ``rake > 0`` guards in the services rely on it being exactly 0."""
    payout, rake = split_pot(100, 2.0)
    assert (payout, rake) == (200, 0)


def test_explicit_multiplier_overrides_the_default() -> None:
    payout, rake = split_pot(100, 1.5)
    assert (payout, rake) == (150, 50)


# ----------------------------------------------------------------------
# No drift between the three PvP games
# ----------------------------------------------------------------------


def test_every_pvp_game_carries_the_same_edge() -> None:
    """All three are the same product from a player's seat; a drift
    between them would be an arbitrage, not a feature."""
    from telegram_invite_bot.games.duel import DuelConfig
    from telegram_invite_bot.games.rps import RpsConfig

    assert DuelConfig().payout_multiplier == PVP_PAYOUT_MULTIPLIER
    assert RpsConfig().payout_multiplier == PVP_PAYOUT_MULTIPLIER


def test_the_three_resolvers_agree_coin_for_coin() -> None:
    """Same bet through the duel resolver, the rps resolver and the raw
    helper the pvp service calls — one number, three call sites."""
    from telegram_invite_bot.games.duel import resolve_round as duel_round
    from telegram_invite_bot.games.rps import RpsMove
    from telegram_invite_bot.games.rps import resolve_round as rps_round

    for bet in (10, 137, 10_000):
        expected = split_pot(bet)
        duel = duel_round(6, 1, bet=bet)
        rps = rps_round(RpsMove.ROCK, RpsMove.SCISSORS, bet=bet)
        assert (duel.payout, duel.rake) == expected
        assert (rps.payout, rps.rake) == expected
