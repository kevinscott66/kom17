"""Pure outcome resolver for /duel (PvP dice game).

Mirrors the shape of :mod:`telegram_invite_bot.games.rps`:

* Pure: no I/O, no clock, no module state. Resolution is a function
  of (challenger_roll, opponent_roll, bet, payout_multiplier).
* No PvE branch — legacy is PvP only (bot.py ``Duel`` class).
* Tie refunds both stakes, no house edge — legacy parity.
* A decided round pays the winner ``bet × 1.9`` and burns the rest of
  the ``2 × bet`` pot (T-020/R8). This is the one deliberate break
  from legacy's ``bet × 2`` in this module; see :class:`DuelConfig`.

Legacy posture
--------------
Legacy ``Duel`` (bot.py ~15021) is a best-of-N dice game where each
side rolls 1d6 and the higher roll wins the round; the match ends
when a side reaches ``max_wins``. RR-2 #22 restored that: the handler
repeats the ``awaiting_rolls`` state with a round counter and settles
ONCE, on the decisive round. This resolver stays round-local — it
knows nothing about the running score, which lives in the FSM.

The stake still moves exactly once per match, not once per round:
:meth:`DuelService.play` is called only on the round that ends the
match. Because a tied round advances no one's score, the match can
only end on a round somebody won, so that final round's winner IS
the match winner — which is what makes settling on it correct.

Bet bounds match legacy ``DUEL_MIN_BET = 10``, ``DUEL_MAX_BET =
10000`` (bot.py:3786-3787) — and since T-020/R9 they are the shared
:mod:`~.limits` constants rather than local copies of them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from telegram_invite_bot.games.limits import MAX_BET, MIN_BET
from telegram_invite_bot.games.pot import PVP_PAYOUT_MULTIPLIER, split_pot


class DuelOutcome(StrEnum):
    """Resolver verdict for a single duel round.

    Framed from the challenger's seat, same convention as
    :class:`telegram_invite_bot.games.rps.RpsOutcome`.
    """

    CHALLENGER_WIN = "challenger_win"
    OPPONENT_WIN = "opponent_win"
    TIE = "tie"


@dataclass(frozen=True, slots=True)
class DuelConfig:
    """Tunables consulted by :class:`DuelService` before calling the
    resolver. Bet bounds match legacy ``DUEL_MIN_BET`` /
    ``DUEL_MAX_BET`` (bot.py:3786-3787).

    T-020/R8: ``payout_multiplier`` is ``1.9``, not the ``2`` legacy
    shared with /cpc. Paying the winner the entire ``2 × bet`` pot is
    a pure transfer between two players and leaves the bot with a
    zero-edge game — while every coin in that pot is redeemable at
    /withdraw out of the owner's pocket. The ~5% rake matches
    :class:`telegram_invite_bot.games.rps.RpsConfig`, /roulette, and
    the R7 stake games, so no game in the ecosystem is free money.
    """

    min_bet: int = MIN_BET
    max_bet: int = MAX_BET
    payout_multiplier: float = PVP_PAYOUT_MULTIPLIER
    dice_faces: int = 6
    # Upper bound on the ``max_wins`` argument of ``/duel <bet> <wins>``,
    # NOT a cap on how many rounds get played: a best-of-5 match can run
    # past five rounds because tied rounds advance nobody. Mirrors legacy
    # ``DUEL_MAX_ROUNDS`` (settings default 5, bot.py:2584), which gates
    # the same argument — legacy's name is the misleading one.
    max_wins_cap: int = 5


@dataclass(frozen=True, slots=True)
class DuelRoundResult:
    """What :func:`resolve_round` decided for one (roll, roll, bet) triple.

    ``rake`` (T-020/R8) is the slice of the ``2 × bet`` pot that is not
    paid out — burned, never credited to anyone, and ``0`` on a tie
    where both stakes go straight back. Carried on the result so the
    service can write an auditable ``duel_rake`` ledger row and the
    result card can name the cut, rather than each re-deriving it.
    """

    outcome: DuelOutcome
    challenger_roll: int
    opponent_roll: int
    bet: int
    payout: int
    net_delta: int
    rake: int = 0


def resolve_round(
    challenger_roll: int,
    opponent_roll: int,
    *,
    bet: int,
    payout_multiplier: float = PVP_PAYOUT_MULTIPLIER,
) -> DuelRoundResult:
    """Higher roll wins; equal rolls are a tie (refund both).

    Pure. Trusts caller to have validated rolls / bet bounds — the
    SERVICE layer (DuelService) handles those.

    The pot split (and why it floors rather than rounds) lives in
    :func:`telegram_invite_bot.games.pot.split_pot`, shared with
    ``/cpc`` and the ``/pvp_*`` stake games so the three cannot drift.
    """
    payout_on_win, rake_on_win = split_pot(bet, payout_multiplier)
    if challenger_roll == opponent_roll:
        return DuelRoundResult(
            outcome=DuelOutcome.TIE,
            challenger_roll=challenger_roll,
            opponent_roll=opponent_roll,
            bet=bet,
            payout=bet,
            net_delta=0,
            rake=0,
        )
    if challenger_roll > opponent_roll:
        return DuelRoundResult(
            outcome=DuelOutcome.CHALLENGER_WIN,
            challenger_roll=challenger_roll,
            opponent_roll=opponent_roll,
            bet=bet,
            payout=payout_on_win,
            net_delta=payout_on_win - bet,
            rake=rake_on_win,
        )
    return DuelRoundResult(
        outcome=DuelOutcome.OPPONENT_WIN,
        challenger_roll=challenger_roll,
        opponent_roll=opponent_roll,
        bet=bet,
        payout=payout_on_win,
        net_delta=-bet,
        rake=rake_on_win,
    )


def roll_die(*, rng: random.Random, faces: int = 6) -> int:
    """Uniform 1..faces inclusive. RNG injected for test determinism."""
    return rng.randint(1, faces)
