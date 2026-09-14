"""telegram_invite_bot — aiogram 3 application package (new architecture).

The legacy telebot monolith this package replaced (``bot.py`` + ``main.py``)
no longer runs anywhere: T-011 (2026-05-26) removed the strangler bridge and
left no legacy unit on any host (``CUTOVER.md``). Every Telegram update is
resolved here.
"""

from __future__ import annotations

__version__ = "0.1.0"
