"""Couple joint-activities inline-keyboard CallbackData factory.

One wire format powers the whole feature: the activity-menu buttons
(rendered by ``handlers/couple_activities``) and the do-activity click
that consumes them. ``kind`` discriminates marriage vs relationship,
``key`` names the catalog row, and ``partner_id`` pins the bond the
clicker is acting on so the do-activity handler can re-resolve it
without a second round trip through the menu.

Prefix ``cpl_act`` is distinct under aiogram's exact-first-segment
filter match from every other ``CallbackData`` prefix in this package
(``shop_*`` / ``inv_*`` / ``duel_*`` / ``rps_*`` …) and from the legacy
marriage/relationship literals (``marry_accept_<id>`` /
``rel_accept_<id>``), which carry no colon and so fail the
first-segment-equality match.

Wire budget: ``cpl_act:rel:walk_invite:123456789:987654321`` is 43
bytes, under Telegram's 64-byte callback_data cap. The longest activity
key is ``anniversary`` (marriage) at 11 chars, and the two ids are
Telegram user ids, which the Bot API guarantees fit in 52 significant
bits — at most 16 decimal digits each. Worst case is therefore
``cpl_act:marry:anniversary:<16>:<16>`` = 59 bytes — 5 bytes of slack,
not 7. Both numbers are counted, not eyeballed: the earlier 45/57 pair
was off by two in each direction. That is the tightest of the three
formats here; ``cpl_menu`` and ``cpl_hist`` carry no ``key`` and have
~11 bytes more slack. Adding a fifth field to ``CoupleActivity`` would
need this arithmetic redone, not just eyeballed.

#463: every one of the three carries ``owner_id`` — the id of the user
the card was rendered FOR. The handlers all re-resolve the bond from
the clicker, so a foreign click never leaked or spent anything; what it
did was EDIT somebody else's card in a group into the clicker's own
menu or history, which reads to the group as the card owner's data
changing under them. The owner tag makes that a rejected click instead.
Stale cards rendered before this field existed no longer unpack, so
their buttons go inert rather than misfire — acceptable for a card that
is re-rendered by the next ``/marriage``, ``/relationship`` or
``/activities``.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class CoupleActivity(CallbackData, prefix="cpl_act"):
    """A single couple-activity button (menu render + do-activity click).

    Fields:

    * ``kind`` — ``"marry"`` or ``"rel"``; selects which catalog +
      grant path the do-activity handler uses.
    * ``key`` — the catalog row key (e.g. ``"dinner"``, ``"big_gift"``).
    * ``partner_id`` — the other party in the bond. For marriage the
      handler re-resolves via ``get_marriage(chat, clicker)`` and uses
      this only for the mention; for relationship it's the second
      argument to ``get_relationship(chat, clicker, partner_id)``.
    * ``owner_id`` — who the menu was rendered for (#463). Not an
      authorization token for the money: the spend is always the
      clicker's own, on the clicker's own bond. It exists so a
      passer-by cannot repaint the owner's card.
    """

    kind: str
    key: str
    partner_id: DbInt
    owner_id: DbInt


class CoupleMenu(CallbackData, prefix="cpl_menu"):
    """Open the joint-activity menu for a bond from its status card (L-34/L-35).

    Rendered on the ``/marriage`` card and the ``/relationship`` per-pair
    card; clicking it edits the card into the activity menu (same buttons
    ``handlers/couple_activities`` builds for ``/activities``). ``kind``
    discriminates marriage vs relationship; ``partner_id`` pins the bond so
    the handler re-resolves it without a second round trip. Prefix
    ``cpl_menu`` is distinct under aiogram's first-segment match from
    ``cpl_act`` (the do-activity click) and ``cpl_hist`` (history).
    ``owner_id`` is the card's addressee — see the module docstring
    (#463).
    """

    kind: str
    partner_id: DbInt
    owner_id: DbInt


class CoupleHistory(CallbackData, prefix="cpl_hist"):
    """Open the last-15 joint-activity history for a bond (L-34/L-35).

    Rendered on the status cards + the activity menu; clicking it edits the
    message into the history list (read from the bond activity-log). ``kind``
    is ``"marry"`` or ``"rel"``; ``partner_id`` pins the pair. For marriage
    the handler re-resolves via ``get_marriage(chat, clicker)`` and uses
    ``partner_id`` only to scope the log query; for relationship it's the
    second arg to ``get_relationship``. ``owner_id`` is the card's
    addressee — see the module docstring (#463).
    """

    kind: str
    partner_id: DbInt
    owner_id: DbInt
