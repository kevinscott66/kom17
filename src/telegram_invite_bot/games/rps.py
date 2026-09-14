"""Pure outcome resolver for rock-paper-scissors (Stage 32 of 32/33/34).

Stage 32 — pure resolver only. Stage 33 will wire an ``RpsService`` over
:class:`EconomyRepo` for the bet escrow + payout writes; Stage 34 ships
the /cpc handler + e2e coverage. The split mirrors the inventory_use
strangler track (Stage 27 → 28 → 29): land the math first so the
service stage is "given a resolved round, do the writes" — easy to
review on its own — and so this stage is unit-testable without touching
the DB, aiogram, or the FSM session manager that legacy uses to hold
in-flight challenges.

Legacy posture (file: ``rock_paper_scissors.py`` at repo root)
--------------------------------------------------------------
* **PvP only, no PvE.** Legacy's :class:`CPCManager` (``:78``) requires
  two real user ids (challenger_id, opponent_id) and there is no
  bot-opponent branch anywhere in the file. ``register_cpc_handlers``
  takes no RNG, and ``resolve_winner`` (``:278``) takes two human
  choices. So the resolver here also takes two human moves and there
  is deliberately no ``choose_bot_move`` — the brief considered one,
  but adding it would invent a PvE surface legacy never had. If a
  future stage ships PvE, the obvious extension is a sibling
  ``resolve_pve_round`` plus a ``choose_bot_move(*, rng)`` factory; the
  current resolver shape doesn't need to change.

* **Payout multiplier: 2.0 on win.** ``rock_paper_scissors.py:541``:
  ``add_coins(winner_id, bet * 2, ...)``. The winner gets their stake
  back plus the loser's stake — a flat 2× of the bet. No house cut, no
  rake, no tax: ``remove_coins(loser_id, bet, ...)`` at ``:539`` moves
  exactly ``bet`` from loser to winner via the two-call pattern. This
  resolver mirrored that exactly until T-020/R8, which took a ~5 %
  rake — see :class:`RpsConfig`. It is the one deliberate break from
  legacy in this module, and the reason is that a zero-edge game whose
  every coin is redeemable at ``/withdraw`` is free money paid out of
  the owner's pocket.

* **Tie posture: refund-both, no house edge.**
  ``rock_paper_scissors.py:546-547`` on a draw calls
  ``add_coins(challenger_id, bet, ...)`` and
  ``add_coins(opponent_id, bet, ...)`` — both stakes returned in full.
  No "house keeps half on tie", no carry-over to a next round. So
  ``house_edge_on_tie`` defaults to ``False`` here and the
  ``TIE`` outcome ships ``payout = bet`` (refund) and ``net_delta = 0``.

* **Bet bounds: 10 / 10 000, unchanged in effect by T-020/R9.**
  ``rock_paper_scissors.py:664-665`` reads ``cpc_min_bet: int = 10,
  cpc_max_bet: int = 100000``, but those defaults are dead: the sole
  registration overrides both (``bot.py:21564-21565``, from
  ``DUEL_MIN_BET`` / ``DUEL_MAX_BET`` = 10 / 10 000 at ``bot.py:2582-2583``).
  R9 replaced them with the shared
  :data:`~telegram_invite_bot.games.limits.MAX_BET` — same numbers, one
  source — see that module for what it did and did not buy. Bet
  validation lives in
  :class:`CPCManager.create` (``:101-104``) before the round resolves,
  so legacy never resolves a round with an out-of-bounds bet. The
  resolver here trusts its caller likewise — bet bounds are a SERVICE
  concern (Stage 33 will validate before calling the resolver), not a
  resolver concern, and so the ``RpsOutcome`` enum carries no
  ``BET_OUT_OF_BOUNDS`` / ``INSUFFICIENT_FUNDS`` variants.

* **No per-user daily cap, no streak state.** Legacy keeps no
  per-user counters across rounds: ``CPCGameSession`` (``:51``) is
  per-round and ``_remove`` (``:220``) drops it at terminal status.
  ``save_game_result`` (``:543``) writes a history row but nothing in
  the resolution path consults it. So the resolver is stateless —
  no streak bonuses, no anti-grind tax.

* **WIN_MATRIX** (``rock_paper_scissors.py:48``):
  ``rock>scissors, scissors>paper, paper>rock`` — mirrored byte-for-byte
  by :meth:`RpsMove.beats`. Tested transitively in the unit suite.

Deferred to the service stage (Stage 33)
----------------------------------------
* Bet bounds (min/max) validation.
* Insufficient-funds checks (``CPCManager.create`` ``:105-108`` reads
  both balances before the challenge issues).
* Atomic escrow + payout via EconomyRepo (legacy's
  ``remove_coins`` / ``add_coins`` pair at ``:539-541`` is non-atomic;
  Stage 33 should write a single transaction).
* "Already in a game" guard (``:99``).
* Timeout coercion: legacy ``cleanup_choose_timeouts`` (``:254``)
  substitutes the sentinel ``"timeout_lose"`` for a missing choice
  before calling :meth:`CPCManager.resolve_winner`. That's session-
  lifecycle plumbing, not game math, so it lives in the service
  alongside the FSM port (Stage 33 or Stage 34, depending on how the
  challenge-accept timer translates to aiogram).
* History row write (``save_game_result`` at ``:543``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

from telegram_invite_bot.games.limits import MAX_BET, MIN_BET
from telegram_invite_bot.games.pot import PVP_PAYOUT_MULTIPLIER, split_pot


class RpsMove(StrEnum):
    """The three legal moves a player can pick.

    String-valued (StrEnum) so the values round-trip with legacy's
    callback payload (``rock_paper_scissors.py:44``:
    ``CHOICES = ("rock", "scissors", "paper")``) — the eventual Stage 34
    handler will parse ``cpc_choice_<sid>_rock`` and feed the substring
    straight into ``RpsMove(...)``. Matches the convention
    :class:`InventoryEffectKind` already follows.
    """

    ROCK = "rock"
    PAPER = "paper"
    SCISSORS = "scissors"

    def beats(self, other: RpsMove) -> bool:
        """Return True iff ``self`` beats ``other`` under the legacy matrix.

        Mirrors ``WIN_MATRIX`` at ``rock_paper_scissors.py:48``:
        rock>scissors, scissors>paper, paper>rock. Ties (``self == other``)
        return False — the caller distinguishes tie from loss via
        :func:`resolve_round`'s :class:`RpsOutcome`, not via this method.
        """
        return (
            (self is RpsMove.ROCK and other is RpsMove.SCISSORS)
            or (self is RpsMove.SCISSORS and other is RpsMove.PAPER)
            or (self is RpsMove.PAPER and other is RpsMove.ROCK)
        )


class RpsOutcome(StrEnum):
    """Resolver verdict for a single round.

    Three variants — the resolver's job is "who won this round?", not
    "is this round legal to play?". Validation outcomes
    (insufficient funds, out-of-bounds bet, already-in-game) belong to
    the SERVICE layer (Stage 33) and never reach this enum: see the
    "Deferred to the service stage" block in the module docstring for
    the full split.

    Player-side framing (PLAYER_WIN / BOT_WIN naming from the brief)
    is rewritten as CHALLENGER_WIN / OPPONENT_WIN here because legacy
    is strictly PvP — there is no bot opponent. The "challenger" is
    the user who issued ``/cpc`` (``rock_paper_scissors.py:317``) and
    the "opponent" is the user they targeted (``:344``). Outcome is
    framed from the challenger's seat so the eventual service can read
    the same field whether the user-of-interest is challenger or
    opponent (it flips the sign on net_delta when reporting for the
    opponent — same idiom as a chess engine reports from White's seat).
    """

    CHALLENGER_WIN = "challenger_win"
    """The challenger's move beats the opponent's move."""

    OPPONENT_WIN = "opponent_win"
    """The opponent's move beats the challenger's move."""

    TIE = "tie"
    """Both players picked the same move; both stakes refund. Legacy
    ``rock_paper_scissors.py:546-547`` issues two ``add_coins(uid, bet)``
    calls on draw — no house edge, no carry-over."""


@dataclass(frozen=True, slots=True)
class RpsConfig:
    """Tunables the SERVICE will consult before calling the resolver.

    Lives in the resolver module (not the service module) so the
    legacy posture is pinned next to the math it constrains; Stage 33's
    service imports this rather than re-deriving constants. Frozen +
    slotted so a hand-built config in tests can't drift mid-test.

    ``min_bet`` / ``max_bet`` are bet *bounds*, not resolver inputs —
    :func:`resolve_round` trusts the caller has already validated.
    ``payout_multiplier`` is re-exposed here so the service can pass it
    through to the resolver without two sources of truth.

    T-020/R9: ``max_bet`` is the ecosystem-wide
    :data:`~telegram_invite_bot.games.limits.MAX_BET` = 10 000, which is
    the number legacy ``/cpc`` already enforced — the ``100000`` default
    at ``rock_paper_scissors.py:664-665`` never applied, the sole
    registration overriding it at ``bot.py:21564-21565``.

    T-020/R8: the multiplier is ``1.9``, not legacy's ``2``, and here the
    deviation is much larger than the two numbers suggest. Legacy ``/cpc``
    ran no escrow: ``rock_paper_scissors.py:539-541`` debited only the
    loser and credited the winner ``bet * 2``, so every resolved round
    EMITTED ``+bet`` from nothing and every draw emitted ``+2 × bet``
    (``:546-547``) — all of it withdrawable out of the owner's pocket.
    Paying ``1.9`` out of a real ``2 × bet`` escrow both closes that and
    leaves a ~5% rake, the same edge /roulette and the stake games (R7)
    carry. The rake is burned, not credited anywhere: coins the bot never
    has to honour are exactly the point. See :mod:`~.pot`.
    """

    min_bet: int = MIN_BET
    max_bet: int = MAX_BET
    payout_multiplier: float = PVP_PAYOUT_MULTIPLIER


@dataclass(frozen=True, slots=True)
class RpsRoundResult:
    """What :func:`resolve_round` decided about a single (move, move, bet).

    Frozen + slotted so a resolved round is immutable once handed to
    the service — Stage 33 reads ``net_delta`` for the challenger's
    EconomyRepo write, flips the sign for the opponent's write, and
    never needs to mutate the result. Carries both moves so the
    eventual /cpc result message (``rock_paper_scissors.py:556-557``)
    can render "👤 A — ✊ Rock / 👤 B — ✋ Paper" without re-deriving
    them from session state.

    Field meanings (always from the challenger's seat, per
    :class:`RpsOutcome`'s docstring):

    * ``payout``: coins credited to the WINNER on win
      (``int(bet * payout_multiplier)``, 1.9× since T-020/R8 — legacy
      paid the full ``bet * 2`` pot at ``:541``); coins refunded to
      EACH player on tie (``bet``, matching ``:546-547``); ``0`` on a
      loss seen from the challenger seat where the opponent collects
      elsewhere. Used by the service to drive its EconomyRepo write —
      it's NOT a signed delta.

    * ``net_delta``: signed wallet change for the CHALLENGER.
      ``payout - bet`` on CHALLENGER_WIN, ``-bet`` on OPPONENT_WIN,
      ``0`` on TIE. The service flips the sign when posting for the
      opponent. Always an int — :func:`resolve_round` floors via
      ``int()`` so the fractional multiplier never leaks a float into
      the wallet.

    * ``rake`` (T-020/R8): the slice of the ``2 × bet`` pot that is
      NOT paid out — burned, never credited to anyone. ``0`` on a tie
      (both stakes go straight back). Carried so the service can write
      an auditable ``rps_rake`` ledger row and the result card can be
      honest about the cut, instead of both re-deriving it.
    """

    outcome: RpsOutcome
    challenger_move: RpsMove
    opponent_move: RpsMove
    bet: int
    payout: int
    net_delta: int
    rake: int = 0


def resolve_round(
    challenger_move: RpsMove,
    opponent_move: RpsMove,
    *,
    bet: int,
    payout_multiplier: float = PVP_PAYOUT_MULTIPLIER,
    house_edge_on_tie: bool = False,
) -> RpsRoundResult:
    """Decide the winner and the per-player coin movement.

    Pure: no I/O, no clock, no RNG, no module state. All arithmetic
    derives from the four inputs.

    * ``payout_multiplier=1.9`` (T-020/R8): legacy paid the whole
      ``bet * 2`` pot to the winner at ``rock_paper_scissors.py:541``,
      which is a zero-edge game for the bot. See :class:`RpsConfig`
      for why that had to change.
    * ``house_edge_on_tie=False``: ``rock_paper_scissors.py:546-547``
      refunds both stakes on tie. Still the default — a tie is a
      non-event and taking a cut of it reads as punishing players for
      an outcome neither of them chose.

    The ``house_edge_on_tie`` knob is exposed even though legacy never
    sets it because Stage 33's service config might later toggle it
    (operator-tunable rake on draws); shipping the parameter now means
    Stage 33 doesn't need to grow the resolver signature. When True,
    ``payout`` on TIE is ``0`` and ``net_delta`` is ``-bet`` — both
    players forfeit their stake to the house, matching the obvious
    "house edge" semantics.

    The pot split — and why it floors rather than rounds — lives in
    :func:`telegram_invite_bot.games.pot.split_pot`, shared with
    ``/duel`` and the ``/pvp_*`` stake games so the three cannot drift.
    """
    # ``rake_on_win`` is what stays in the pot after the winner is
    # paid: burned, not credited. Never negative — the multiplier is
    # ``< 2`` by construction (asserted in tests).
    payout_on_win, rake_on_win = split_pot(bet, payout_multiplier)
    challenger_gain_on_win = payout_on_win - bet

    if challenger_move is opponent_move:
        if house_edge_on_tie:
            return RpsRoundResult(
                outcome=RpsOutcome.TIE,
                challenger_move=challenger_move,
                opponent_move=opponent_move,
                bet=bet,
                payout=0,
                net_delta=-bet,
                # Nobody is paid, so the whole pot is the rake.
                rake=bet * 2,
            )
        return RpsRoundResult(
            outcome=RpsOutcome.TIE,
            challenger_move=challenger_move,
            opponent_move=opponent_move,
            bet=bet,
            payout=bet,
            net_delta=0,
            rake=0,
        )

    if challenger_move.beats(opponent_move):
        return RpsRoundResult(
            outcome=RpsOutcome.CHALLENGER_WIN,
            challenger_move=challenger_move,
            opponent_move=opponent_move,
            bet=bet,
            payout=payout_on_win,
            net_delta=challenger_gain_on_win,
            rake=rake_on_win,
        )

    return RpsRoundResult(
        outcome=RpsOutcome.OPPONENT_WIN,
        challenger_move=challenger_move,
        opponent_move=opponent_move,
        bet=bet,
        payout=payout_on_win,
        net_delta=-bet,
        rake=rake_on_win,
    )


def choose_random_move(*, rng: random.Random) -> RpsMove:
    """Uniformly sample a move from an injected :class:`random.Random`.

    Legacy has no bot opponent (see the module docstring), so this is
    NOT used by the current /cpc port — it ships now as scaffolding
    for two foreseeable callers that DO need randomness:

    * A future PvE variant (``/cpc_bot``).

    It is emphatically NOT for choose-timeout coercion, whatever the
    symmetry suggests. Legacy does not give the no-show a move at all:
    ``cleanup_choose_timeouts`` (``rock_paper_scissors.py:254``) stamps
    the ABSENT player with the sentinel ``"timeout_lose"``
    (``:267-272``), which :meth:`CPCManager.resolve_winner`
    short-circuits into an outright loss before any move comparison
    happens (``:280-283``). The ``or "rock"`` on the neighbouring line
    is a filler for the player who DID show up and is never consulted,
    because the sentinel has already decided the round. When BOTH sides
    miss the timer the session is ``cancelled`` and removed with no
    resolution at all (``:263-266``).

    Substituting a random move for the absent player would therefore
    not close an exploit — it would open one, handing a no-show a 1/3
    win and a 1/3 tie on a real-money stake they never played for. The
    port keeps legacy's rule; see the module docstring's timeout bullet.

    Uniform sampling (not weighted) — legacy never weights anything,
    and the obvious "house edge" knob is :func:`resolve_round`'s
    ``house_edge_on_tie``, not a biased move distribution. The
    ``Random`` instance is injected so tests can pin determinism via
    ``Random(seed)``; the function itself is stateless beyond ``rng``.
    """
    return rng.choice(list(RpsMove))
