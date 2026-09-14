"""``/admin_withdrawals`` approve/reject CallbackData factories (#28, T-027).

The admin panel renders an Approve/Reject button pair per pending row.
Both payloads carry the ``request_id`` — the row the action targets.
Authorization is NOT a wire tag here: the callback handlers re-check
``settings.bot.is_developer(clicker_id)`` server-side, so a forged
payload from a non-dev is rejected regardless of what id it carries.

Distinct prefixes (``wd_adm_ok`` / ``wd_adm_no``) keep these clear of
both the user-side withdraw card (``wd_ok`` / ``wd_no``) and any legacy
``withdraw_*`` payload — no cross-matching across the strangler bridge.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class WithdrawApprove(CallbackData, prefix="wd_adm_ok"):
    """ "✅ Approve" button on the admin pending-withdrawals card.

    ``request_id`` names the ``withdrawal_requests`` row to pay out.
    The handler re-checks developer authorization before acting.
    """

    request_id: DbInt


class WithdrawReject(CallbackData, prefix="wd_adm_no"):
    """ "🚫 Reject" button — refund the escrow and close the row."""

    request_id: DbInt
