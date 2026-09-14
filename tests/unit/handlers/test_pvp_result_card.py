"""Unit tests for the ``/pvp_*`` resolved-game card (T-020/R8).

The offer card has e2e coverage; the RESULT card had none, which is
awkward now that it renders numbers taken off the service result rather
than re-derived from ``bet``. A missing kwarg does not raise — the
translator renders the placeholder literally (see
``test_translator.test_missing_placeholder_renders_literally``) — so a
dropped ``rake=`` would have shipped a card reading "House fee: {rake}"
to real users. These tests fail on exactly that.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.games.pvp import CREATOR, OPPONENT, CoinResult, DiceResult
from telegram_invite_bot.handlers.pvp_stake import _result_text
from telegram_invite_bot.services.pvp_service import PvpAcceptOutcome, PvpAcceptResult

NAMES = {1: "Creator", 2: "Opponent"}


def _coin_result(winner_seat: int) -> PvpAcceptResult:
    return PvpAcceptResult(
        outcome=PvpAcceptOutcome.SUCCESS,
        game="coin",
        bet=100,
        creator_id=1,
        opponent_id=2,
        winner_id=1 if winner_seat == CREATOR else 2,
        coin=CoinResult(flip="heads", winner=winner_seat),
        payout=190,
        rake=10,
    )


def _dice_result(*, tie: bool) -> PvpAcceptResult:
    return PvpAcceptResult(
        outcome=PvpAcceptOutcome.SUCCESS,
        game="dice",
        bet=100,
        creator_id=1,
        opponent_id=2,
        winner_id=None if tie else 1,
        dice=DiceResult(
            creator_roll=3 if tie else 6,
            opponent_roll=3 if tie else 1,
            winner=None if tie else CREATOR,
        ),
        payout=0 if tie else 190,
        rake=0 if tie else 10,
    )


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_coin_card_quotes_the_payout_not_the_pot(lang: str) -> None:
    """190 is what the wallet received; 200 is the pot the winner no
    longer collects in full. Printing 200 would be a lie the ledger
    contradicts."""
    card = _result_text(_coin_result(CREATOR), lang, NAMES)
    assert "190" in card
    assert "200" not in card


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_coin_card_names_the_house_cut(lang: str) -> None:
    """R8 pays less than legacy's 2×; the card says so out loud rather
    than letting the player discover it by subtracting."""
    card = _result_text(_coin_result(OPPONENT), lang, NAMES)
    assert "10" in card
    assert ("Комиссия банка" if lang == "ru" else "House fee") in card


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_dice_card_quotes_the_payout_and_the_cut(lang: str) -> None:
    card = _result_text(_dice_result(tie=False), lang, NAMES)
    assert "190" in card
    assert ("Комиссия банка" if lang == "ru" else "House fee") in card


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_tie_card_mentions_no_fee(lang: str) -> None:
    """A tie refunds both stakes and takes nothing, so the fee line must
    not appear — quoting a 0-coin fee would read as a charge."""
    card = _result_text(_dice_result(tie=True), lang, NAMES)
    assert ("Комиссия банка" if lang == "ru" else "House fee") not in card


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize(
    "factory",
    [
        lambda: _coin_result(CREATOR),
        lambda: _dice_result(tie=False),
        lambda: _dice_result(tie=True),
    ],
)
def test_no_placeholder_survives_rendering(lang: str, factory: object) -> None:
    """The catch-all: an unpassed kwarg renders literally rather than
    raising, so scan for leftover braces on every branch."""
    card = _result_text(factory(), lang, NAMES)  # type: ignore[operator]
    assert "{" not in card
    assert "}" not in card
