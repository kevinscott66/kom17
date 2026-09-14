"""``/admin_withdrawals`` pagination CallbackData (cluster A3, L-97).

The pending-withdrawals card shows :data:`~telegram_invite_bot.handlers.
admin.withdrawals._SAMPLE_SIZE` rows per page; this payload drives the
◀️/▶️ navigation row. ``page`` is zero-based and clamped server-side —
a stale button on an old card (queue shrank since render) lands on the
last existing page rather than an empty one.

Lives in its own module (not ``admin_withdraw.py``) because that file
belongs to the approve/reject port (#28) and this one to the L-97
paging extension — distinct ownership, distinct prefix namespace.
Authorization is re-checked server-side in the handler; the payload is
a view cursor, not a capability.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class WithdrawPage(CallbackData, prefix="wd_adm_pg"):
    """◀️/▶️ page-navigation button on the admin withdrawals card.

    ``page`` is the zero-based target page. The handler clamps it to
    the current queue size before rendering, so racing against
    approvals that shrink the queue is harmless.
    """

    page: DbInt
