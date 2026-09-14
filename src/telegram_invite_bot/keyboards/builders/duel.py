"""/duel inline-keyboard CallbackData factories (T-018).

Three wire formats — Accept, Decline, Roll — analogous to the /cpc
trio in :mod:`telegram_invite_bot.keyboards.builders.rps`. Every
payload carries ``challenger_id`` so the handler can locate the FSM
session that owns the match (Variant A — FSM owned by the challenger).
The group chat id is read from the callback's ``message.chat.id``
directly; carrying it in the payload would inflate the wire format
and is not needed for authorization.

Prefixes (``dl_acc``, ``dl_dec``, ``dl_rl``) are distinct from any
legacy ``duel_*`` payload (legacy emits ``duel_accept_<id>``,
``duel_decline_<id>``, ``duel_roll_<id>`` — bot.py:21299,21300,21374)
so the strangler bridge regression pin holds: the new pipeline's
filters cannot match a legacy callback and vice-versa.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class DuelAccept(CallbackData, prefix="dl_acc"):
    """ "✅ Accept" button on the challenge card.

    Carries ``challenger_id`` (FSM session key) and ``bet`` (stamped on
    the wire so a stale card's log line still records what the user
    was agreeing to). Authorization rides on the FSM: the handler
    verifies ``data["opponent_id"] == callback.from_user.id`` before
    flipping state.
    """

    challenger_id: DbInt
    bet: DbInt


class DuelDecline(CallbackData, prefix="dl_dec"):
    """ "❌ Decline" button on the challenge card."""

    challenger_id: DbInt


class DuelRoll(CallbackData, prefix="dl_rl"):
    """🎲 "Roll" button shown to both seats during awaiting_rolls.

    Both seats use the same payload prefix; the handler resolves which
    seat clicked via ``callback.from_user.id`` against the FSM data.
    The actual die roll is generated server-side via
    :func:`telegram_invite_bot.games.duel.roll_die` — the payload does
    NOT carry a client-supplied number (defense against tampering).
    """

    challenger_id: DbInt
