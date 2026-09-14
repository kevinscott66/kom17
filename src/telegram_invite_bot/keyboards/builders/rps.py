"""/cpc inline-keyboard CallbackData factories (Stage 34).

Three wire formats — Accept, Decline, Move — all rendered by
``handlers/rps`` and all consumed there. Every payload carries
``challenger_id`` because the FSM session that owns the match lives
on the challenger's ``(chat_id, user_id)`` key (see
``fsm/rps.py``'s "Variant A" rationale). Opponent-side callbacks
need that id to locate the match state; without it the handler
couldn't tell which match a click belongs to.

Prefix selection — none collide with existing prefixes
------------------------------------------------------
* ``rps_acc`` — the "✅ Принять" button on the challenge card.
* ``rps_dec`` — the "❌ Отказаться" button on the challenge card.
* ``rps_mv``  — the three move buttons (rock/paper/scissors).

Distinct from each other under aiogram's exact-first-segment filter
match. Distinct from legacy's literal ``cpc_accept_<sid>``,
``cpc_decline_<sid>``, ``cpc_choice_<sid>_<move>`` payloads
(``rock_paper_scissors.py:378-380``, ``:484-486``) — legacy emits no
colon in the first segment, so an aiogram filter for ``rps_acc`` /
``rps_dec`` / ``rps_mv`` cannot match a legacy ``cpc_*`` callback and
vice-versa. The strangler bridge regression pin in
``tests/e2e/handlers/test_support.py`` already locks unrelated
callbacks falling through to legacy; this keeps that property.

Why ``challenger_id`` and not ``session_id``
--------------------------------------------
Legacy uses a synthetic ``session_id`` integer counter (in-memory
across processes — not durable). The new pipeline keys on the
challenger's ``user_id`` because:

* It's already known to both renderers (challenger initiates, opponent
  is messaged BY the bot acting on the challenger's behalf).
* It maps directly to the FSM key ``(chat_id, user_id)`` so the
  handler can locate the right :class:`FSMContext` without an
  intermediate lookup table.
* A second simultaneous /cpc from the same challenger is rejected
  upstream (FSM state != None means busy), so ``challenger_id``
  uniquely identifies the in-flight match.

Field budget
------------
Telegram caps callback_data at 64 bytes. ``rps_mv:<int64>:scissors``
is at most ``5 + 1 + 19 + 1 + 8 = 34`` bytes — comfortable. The
``move`` field is constrained to one of three short tokens by
:class:`telegram_invite_bot.games.rps.RpsMove`, so a hand-crafted
``rps_mv:1:eat_paper`` from a curious user fails the
:class:`RpsMove` round-trip in the handler and gets a generic toast,
not a service call.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class RpsAccept(CallbackData, prefix="rps_acc"):
    """ "✅ Принять" button on the challenge card sent to the opponent.

    Carries:

    * ``challenger_id`` — the FSM session key (see module docstring
      for why challenger_id, not a synthetic session id).
    * ``bet`` — the stake the opponent is agreeing to. Stamped on the
      wire so a stale challenge card (the FSM expired between render
      and click) still shows the user what they would have agreed to;
      authorization is still gated by the FSM, but the payload makes
      "you accepted N coins" auditable from the log line.

    Authorization rides on the FSM: the handler reads
    ``state.get_data()`` for the challenger's FSM session and checks
    ``data["opponent_id"] == callback.from_user.id`` before flipping
    state. A hand-crafted ``rps_acc:<A>:100`` from user C reaches the
    handler, the FSM data says opponent is B, C != B → silent toast
    rejection, no state flip, no escrow.
    """

    challenger_id: DbInt
    bet: DbInt
    # R-FIX-008: chat where the challenger issued /cpc. Callback
    # handlers reconstruct the FSM key as
    # ``StorageKey(chat_id=chat_id, user_id=challenger_id)``.
    # Without this field, two parallel /cpc by the same challenger in
    # two different group chats would collide on a single FSM slot.
    chat_id: DbInt


class RpsDecline(CallbackData, prefix="rps_dec"):
    """ "❌ Отказаться" button on the challenge card.

    Single field ``challenger_id`` — same FSM key as :class:`RpsAccept`.
    Bet is not carried because the decline path renders no bet-aware
    copy; the toast is just "challenge declined". Authorization works
    the same way — the handler reads the challenger's FSM, verifies
    ``opponent_id`` matches the clicker, clears the FSM.
    """

    challenger_id: DbInt
    # R-FIX-008: see :class:`RpsAccept` for the rationale.
    chat_id: DbInt


class RpsMove(CallbackData, prefix="rps_mv"):
    """One of the three move buttons (✊ / 🖐 / ✌️) sent to BOTH players.

    Carries:

    * ``challenger_id`` — FSM session key. Both seats use the same key
      so the handler can write challenger_move OR opponent_move into
      the same FSMContext depending on which user clicked.
    * ``move`` — string value of :class:`RpsMove` enum. Constrained at
      handler time via ``RpsMove(callback_data.move)`` which raises on
      a hand-crafted unknown value — same pattern as legacy's check
      against :data:`CHOICES` at ``rock_paper_scissors.py:628``.

    Note this class is named identically to the enum
    :class:`telegram_invite_bot.games.rps.RpsMove`. The collision is
    deliberate — they're the same wire vocabulary at two layers
    (CallbackData factory vs. typed enum), and the import sites
    disambiguate via ``from ... import RpsMove as RpsMoveCallback``
    where both are needed. The handler module makes this explicit.
    """

    challenger_id: DbInt
    move: str
    # R-FIX-008: see :class:`RpsAccept` for the rationale.
    chat_id: DbInt
