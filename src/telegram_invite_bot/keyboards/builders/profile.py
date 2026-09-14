"""``/profile`` inline-button CallbackData factory (T-024.4).

The restored profile card carries a "🔄 refresh" button so the user
can re-snapshot their live stats without re-typing the command — the
whole point of a *real-time* preview. The payload carries the owner's
``user_id`` so the handler can reject taps from other members (a card
posted in a group is visible to everyone; only the owner re-renders).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class ProfileRefresh(CallbackData, prefix="prof_refresh"):
    """ "🔄 Refresh" button on a profile card.

    ``user_id`` is the card owner; the handler re-checks
    ``callback.from_user.id == user_id`` before re-rendering so a
    bystander's tap is a no-op.
    """

    user_id: DbInt


class ProfilePanel(CallbackData, prefix="prof_panel"):
    """A drill-down button on the private ``/profile`` hub (#1/#2).

    ``panel`` selects the view (``fin`` finances, ``ach`` achievements,
    ``home`` back to the main card); ``user_id`` is the card owner so a
    bystander's tap on someone else's card is refused, exactly like
    :class:`ProfileRefresh`.
    """

    panel: str
    user_id: DbInt
