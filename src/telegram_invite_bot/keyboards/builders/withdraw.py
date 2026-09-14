"""/withdraw inline-keyboard CallbackData factories (#28, T-027).

Two wire formats — Confirm and Cancel — for the amount-confirmation
card shown at the end of the ``/withdraw`` FSM. Both carry ``user_id``
purely as an authorization tag: the handler rejects a click whose
``callback.from_user.id`` doesn't match, so a foreign user can't drive
another user's confirmation card.

The amount is deliberately NOT carried on the wire. It lives in the
FSM data (set by the amount step) and is re-read there as the single
source of truth — a client-supplied amount on the callback payload
could be tampered with, and this is a money path. Stamping it would
invite exactly that.

Prefixes (``wd_ok``, ``wd_no``) are distinct from any legacy
``withdraw_*`` callback payload so the strangler bridge can't cross-
match: the new pipeline owns these, legacy owns its own ladder.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class WithdrawConfirm(CallbackData, prefix="wd_ok"):
    """ "✅ Confirm" button on the withdraw amount card.

    ``user_id`` is the authorization tag (must equal the clicker's id).
    The amount is read from FSM data, never from this payload.
    """

    user_id: DbInt


class WithdrawCancel(CallbackData, prefix="wd_no"):
    """ "❌ Cancel" button on the withdraw amount card."""

    user_id: DbInt
