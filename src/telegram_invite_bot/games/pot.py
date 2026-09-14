"""The two-seat pot split — one implementation for every PvP game.

Three games in this bot escrow one stake from each of two players and
pay a single winner out of the combined ``2 × bet``: ``/duel``
(:mod:`telegram_invite_bot.games.duel`), ``/cpc`` (:mod:`~.rps`) and
``/pvp_coin`` / ``/pvp_dice`` (:mod:`~.pvp`, settled in
:class:`~telegram_invite_bot.services.pvp_service.PvpService`).

Legacy paid the whole ``2 × bet`` to the winner in all three, so the
bot took nothing while every coin in the pot stayed redeemable at
``/withdraw`` out of the owner's pocket. T-020/R8 takes a ~5 % rake
instead, matching ``/roulette`` and the R7 stake games.

"Zero-edge" is the right verdict for only ONE of the three, though, and
this docstring claimed it for all three until #327. Only ``/pvp_*``
actually escrowed: ``bot.py:14866`` debits the creator, ``:14929`` the
accepter, and ``bot.py:14975`` pays ``bet * 2`` — a genuine zero-sum
transfer. The other two never held a stake at all:

* ``/cpc`` **minted coins.** ``rock_paper_scissors.py:539-541`` debits
  only the LOSER and credits the winner ``bet * 2``; the winner never
  paid in. Every resolved round emitted ``+bet`` out of nothing, and a
  draw emitted ``+2 × bet`` with no debit at all
  (``rock_paper_scissors.py:546-547``). A colluding pair throwing the
  same hand printed ``2 × MAX_BET`` per round, all of it withdrawable.
  ``:539`` also ignores what ``remove_coins`` returned, so a
  broke loser still funded a full ``bet * 2`` payout.
* ``/duel`` was zero-sum but at DOUBLE the stake. It escrowed nothing —
  ``bot.py:21279-21286`` only *checks* both balances against ``bet`` —
  and settled with ``transfer_coins(loser, winner, bet * 2)``
  (``bot.py:15247-15252``), i.e. the loser paid twice what they had
  agreed to risk. And since ``transfer_coins`` refuses when the sender
  cannot cover the amount (``bot.py:10240-10241``) while ``:15247``
  discards its return value, a loser holding less than ``2 × bet``
  simply left the winner unpaid on a duel marked finished.

So R8's real effect is larger than "6 → 5.7 on the payout": it put an
escrow under all three games, which is what makes the pot a pot.

The split lives here rather than three times over because the three
games are the same product from a player's seat: a drift between them
would be an arbitrage, not a feature — a player would simply move to
whichever command paid best. ``tests/unit/games/test_pot.py`` pins that
each game's configured multiplier is this one.

#1566 — DO NOT "restore parity" with legacy's flat ``bet * 2``.
``PVP_PAYOUT_MULTIPLIER`` is 1.9 on the owner's explicit instruction:
the rake this leaves behind is the house margin, and it is his. A diff
that puts the 2 back looks like a faithfulness fix and is a revenue
change. Same guard as ``services/stake_games_service.py``'s two
multipliers.
"""

from __future__ import annotations

#: Fraction of a single stake paid to the winner of a two-seat pot.
#: Below ``2`` by construction — see :func:`split_pot`.
PVP_PAYOUT_MULTIPLIER: float = 1.9


def split_pot(bet: int, multiplier: float = PVP_PAYOUT_MULTIPLIER) -> tuple[int, int]:
    """Split a ``2 × bet`` pot into ``(payout_to_winner, rake_burned)``.

    ``int()`` **floors**, and the direction is load-bearing rather than
    merely tidy. The single-player casino services
    (:class:`~telegram_invite_bot.services.stake_games_service.StakeGamesService`,
    :class:`~telegram_invite_bot.services.roulette_service.RouletteService`)
    pay from the house and can afford ``round()``; a PvP pot cannot,
    because exactly ``2 × bet`` was escrowed and nothing else backs it.
    Rounding UP would hand the winner more than the two stakes hold and
    mint coins out of nothing. Flooring can only ever err toward the
    house, so the rake is non-negative for every ``multiplier <= 2`` —
    which is every multiplier this bot ships.

    The rake is *burned*, not credited: there is no house wallet, and
    inventing one would create a balance that is itself withdrawable,
    which is the liability this whole change exists to shrink.
    """
    payout = int(bet * multiplier)
    return payout, bet * 2 - payout
