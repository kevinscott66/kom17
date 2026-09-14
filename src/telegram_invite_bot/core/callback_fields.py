"""#1978: the magnitude bound every ``CallbackData`` int field needs.

``CallbackData`` is a pydantic model, so a field declared ``int``
validates the TYPE of what came back from Telegram and nothing about
its size. A 25-digit number is still an ``int``, and ``ratnav:`` plus
25 digits is 32 bytes — comfortably inside Telegram's 64-byte callback
limit — so :meth:`CallbackData.unpack` accepts it and hands the handler
a value no database column can hold::

    RatingNav.unpack("ratnav:" + "9" * 25)
    # RatingNav(page=9999999999999999999999999)
    # ... -> OverflowError: Python int too large to convert to SQLite INTEGER

That is the same crash ``utils/numbers.MAX_DB_INT`` closes for numbers
typed into a message, and ``handlers/errors.py`` names ``OverflowError``
in its own list precisely so the sites still lacking a bound would show
up as a distinct signal rather than as another anonymous "Other". The
typed-text path got its discipline; this one never did.

What the crash costs is not authorisation — the queries still match on
the caller's own id — it is the contract around a miss.
``handlers/mygroups.py`` says an unknown group and someone else's group
are "deliberately indistinguishable"; at 31 digits they become
distinguishable, because one of them crashes.

:data:`DbInt` is the fix, and it is deliberately the WIDEST bound that
is still safe rather than a domain limit. A page number has no business
being 10**18 either, but that is the call site's clamp to make and
several already make it; what belongs here is the one bound that is
about the storage rather than about the meaning, so that adding it to a
field can never change which legitimate value round-trips.

The failure mode it produces is the good one. aiogram's
``CallbackDataFilter`` catches ``(TypeError, ValueError)`` around
``unpack``, and pydantic's ``ValidationError`` is a ``ValueError`` — so
an out-of-range payload simply matches no handler and falls through to
``handlers/stale_callback.py``, which answers the "button expired"
toast. No error branch, no log noise, no reply about an internal error.

Every ``CallbackData`` int field in this tree uses this alias, and
``tests/regression/test_callback_int_bounds.py`` keeps it that way by
walking the subclasses rather than by trusting review.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from telegram_invite_bot.utils.numbers import MAX_DB_INT

#: An ``int`` field that cannot carry a value SQLite refuses to bind.
DbInt = Annotated[int, Field(ge=-MAX_DB_INT, le=MAX_DB_INT)]

__all__ = ["DbInt"]
