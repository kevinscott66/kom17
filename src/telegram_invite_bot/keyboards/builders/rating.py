"""CallbackData factories for the donations rating leaderboard (A-04).

The cross-group rating page (``/rating`` / ``/top_groups``) is paginated
and each row drills into that group's stats card. Legacy encoded both
navigations as raw ``rating_page_<n>`` / ``group_stats_<id>`` callback
strings; the new pipeline uses typed :class:`CallbackData` factories so
the page number / group id round-trip through aiogram's parser with no
hand-rolled ``int(data.split("_")[-1])`` at the call site.

Two distinct prefixes keep the namespaces apart:

* :class:`RatingNav` (``ratnav``) — page navigation (prev / next /
  current). ``page`` is 1-based.
* :class:`RatingGroupStats` (``ratgs``) — drill into one group's stats
  card from the leaderboard. ``group_id`` is the Telegram chat id.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class RatingNav(CallbackData, prefix="ratnav"):
    """A rating-leaderboard page navigation tap. ``page`` is 1-based."""

    page: DbInt


class RatingGroupStats(CallbackData, prefix="ratgs"):
    """Drill into one group's stats card from the leaderboard."""

    group_id: DbInt
