"""FSM states for the /cpc challenge-accept / choose-move dance (Stage 34).

Stage 34 lands the FIRST aiogram FSM-driven flow in the strangler
pipeline. The MemoryStorage that backs it is already wired by
``di/providers.py`` (Stage 0-3 scaffolding) so no dispatcher-level
change is needed here — this module is purely the typed state vocab
+ a place to pin the architectural decision below.

Architectural choice — ONE FSM session per match (Variant A)
------------------------------------------------------------
Aiogram's :class:`FSMContext` keys on ``(bot_id, chat_id, user_id)``,
so a /cpc match between A and B technically spans two distinct FSM
sessions (A's private chat, B's private chat). Two reasonable shapes:

* **Variant A** — ONE FSM session, owned by the challenger. Every
  callback (Accept / Decline / Move) carries ``challenger_id`` in its
  payload; the handler reads the challenger's FSMContext to look up
  match state, even when the click came from the opponent. Single
  source of truth, no cross-session synchronisation, opponent-side
  state lives only in callback payloads (stateless from FSM's view).

* **Variant B** — TWO FSM sessions, one per participant, synchronised
  via the same ``challenger_id`` payload. Each side stores a mirror
  of the match data; writes have to land twice.

This module pins **Variant A**. Why:

1. **Atomicity of state transitions.** When opponent clicks Accept,
   the handler flips state from ``awaiting_acceptance`` → ``awaiting_moves``
   in ONE place. With Variant B, the same flip would have to land in
   two FSMContext writes; if the second fails (storage glitch, process
   crash mid-flow), the two sides desync. Single source of truth means
   the race window collapses.

2. **Opponent-side authorization rides on payload, not FSM data.** The
   callback payload carries ``challenger_id``, so the handler reads
   the challenger's FSM and verifies ``data["opponent_id"] ==
   callback.from_user.id`` — that's the gate. Variant B would force
   the opponent's FSM to ALSO carry ``challenger_id`` (else
   authorization is one-sided), which is the same payload bit just
   stored twice.

3. **/cancel parity.** Stage 36's /cancel calls ``state.clear()`` on
   the caller's FSMContext. With Variant A, the challenger's /cancel
   wipes the only match state; with Variant B, /cancel would have to
   chase the opposite party's FSM too, which means /cancel either
   knows about /cpc (cross-cutting bleed) or only half-cancels.

The cost: the OPPONENT cannot use /cancel to abort a match, because
their FSM holds no match state. The opponent's escape hatch is the
inline "❌ Decline" button on the challenge card. Documented in the
handler module; legacy has the same posture (only the challenger has
a /cpc_cancel slash, the opponent has "decline").

What the FSM data dict carries
-------------------------------
On entering ``awaiting_acceptance`` the handler writes::

    {
        "opponent_id": int,
        "bet": int,
        # filled in only after opponent accepts:
        "challenger_move": str | None,  # RpsMove value
        "opponent_move": str | None,
    }

``challenger_move``/``opponent_move`` start absent (``state.get_data()``
returns the dict without the keys) and land via ``state.update_data``
as each side picks. The resolution branch in the move handler reads
both, and if both are set, calls ``RpsService.play`` and clears state.

Deferred to Stage 35
--------------------
* Accept/move timeouts. Aiogram has no built-in TTL on FSM state; a
  scheduler-driven sweeper (apscheduler or a background task in the
  runner) is needed to clear stuck states + refund escrows (though
  Stage 33's ``RpsService.play`` does the escrow atomically at MOVE
  time, not at ACCEPT time, so a stuck ``awaiting_acceptance`` is
  zero-coin-leak — the only damage is a "stuck busy" feel for the
  challenger). Today the escape valve is /cancel for the challenger.
* Group form (``/cpc`` in a supergroup, reply to a message). The new
  pipeline gates private-only at the router level. Group calls used to
  fall through to legacy via the strangler bridge; T-011 removed it,
  and what answers them now is the #123 refusal twin
  (:func:`~handlers.chat_scope.with_chat_type_refusal`), which says the
  command works in a DM rather than saying nothing. Stage 35 lands the
  real group form + a custom in-chat "challenge accepted" edit,
  matching ``rock_paper_scissors.py:391``.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class RpsStates(StatesGroup):
    """States for the /cpc challenge flow, owned by the challenger.

    Two states because the flow has two distinct "waiting on someone"
    phases:

    * ``awaiting_acceptance`` — opponent has been pinged with the
      challenge card; we're waiting on their Accept/Decline click. No
      coins are at stake yet (escrow happens at resolution, see
      :class:`RpsService.play`'s docstring on the atomic escrow+payout).
    * ``awaiting_moves`` — opponent accepted; both sides have a move
      keyboard, we're waiting on the second click to land. Either
      side may have already moved (stored as ``challenger_move`` /
      ``opponent_move`` in FSM data); the resolution fires when both
      are non-None.

    Terminal states (decline, /cancel, both-moved-and-resolved, escrow
    failure at resolution) all collapse to ``state.clear()`` rather
    than another named state — there's no "done" UI to render from a
    DONE state, the next /cpc just reopens the flow from scratch.
    """

    awaiting_acceptance = State()
    awaiting_moves = State()
