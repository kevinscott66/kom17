"""Pure PvP stake-game resolvers (no I/O, no money) — coin & dice.

Mirrors legacy ``_pvp_resolve_coin`` (``bot.py:14891-14893``) and
``_pvp_resolve_dice`` (``:14896-14897``). Note that legacy split the
work differently: those two functions only ROLL, returning a raw dict,
and the winner is decided by their caller in ``pvp_accept_and_resolve``
(``:14949`` for the coin, ``:14953-14959`` for the dice). Both halves
are folded into the resolvers here, which is why the results below carry
a ``winner`` the legacy functions never had.

The RNG is injectable so tests pin outcomes deterministically (same
posture as the roulette/duel resolvers).

Seat convention shared across the PvP pipeline: ``CREATOR`` is the
player who created the offer, ``OPPONENT`` is whoever accepted it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from telegram_invite_bot.utils.rng import money_rng

CREATOR = 0
OPPONENT = 1

# #1350: the canonical MACHINE form of a coin side in the PvP
# pipeline, and the suffix source for the ``h_pvp_side_*`` i18n
# family. ``/flip`` canonicalises the same concept the other way —
# to legacy's Russian ``орёл``/``решка``
# (``handlers/games.py``) — so a value from there interpolated into
# an ``h_pvp_side_{}`` key would ask i18n for ``h_pvp_side_орёл``
# and get the raw key back. The alias is a ``Literal`` precisely so
# mypy refuses that mix at type-check time rather than a user
# finding it in a group chat.
CoinSide = Literal["heads", "tails"]

_COIN_SIDES: tuple[CoinSide, ...] = ("heads", "tails")

# Heads/tails input aliases (RU + EN), mirroring legacy cmd_pvp_coin
# (bot.py:20928-20933) and the /flip side parser.
_HEADS_ALIASES = frozenset({"орел", "орёл", "heads", "head", "h", "о"})
_TAILS_ALIASES = frozenset({"решка", "tails", "tail", "t", "р"})


@dataclass(frozen=True, slots=True)
class CoinResult:
    """A resolved coin game. Coin has NO tie — the flip always decides."""

    flip: CoinSide
    winner: int  # CREATOR or OPPONENT


@dataclass(frozen=True, slots=True)
class DiceResult:
    """A resolved dice game. ``winner is None`` ⇒ tie (equal rolls)."""

    creator_roll: int
    opponent_roll: int
    winner: int | None  # CREATOR / OPPONENT / None


def normalize_side(raw: str) -> CoinSide | None:
    """Map a user heads/tails token to ``"heads"``/``"tails"`` or ``None``."""
    token = raw.strip().lower()
    if token in _HEADS_ALIASES:
        return "heads"
    if token in _TAILS_ALIASES:
        return "tails"
    return None


def resolve_coin(creator_side: str, *, rng: random.Random | None = None) -> CoinResult:
    """Flip a fair coin; the creator wins iff it matches their chosen side.

    Legacy splits this in two: ``_pvp_resolve_coin``
    (``bot.py:14891-14893``) only flips, and its caller decides the seat
    with ``winner_id = p1 if p1_choice == flip else player2_id``
    (``:14949``). There is no tie.
    """
    flip = (rng or money_rng).choice(_COIN_SIDES)
    winner = CREATOR if flip == creator_side else OPPONENT
    return CoinResult(flip=flip, winner=winner)


def resolve_dice(*, rng: random.Random | None = None) -> DiceResult:
    """Both seats roll 1-6; higher wins, equal is a tie (refund both).

    Legacy splits this in two as well: ``_pvp_resolve_dice``
    (``bot.py:14896-14897``) only rolls ``p1_roll`` / ``p2_roll``, each
    ``randint(1, 6)``, and its caller applies the higher-wins /
    equal-is-``None`` rule (``:14953-14959``).
    """
    source = rng or money_rng
    creator = source.randint(1, 6)
    opponent = source.randint(1, 6)
    if creator > opponent:
        winner: int | None = CREATOR
    elif opponent > creator:
        winner = OPPONENT
    else:
        winner = None
    return DiceResult(creator_roll=creator, opponent_roll=opponent, winner=winner)
