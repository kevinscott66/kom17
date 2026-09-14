"""Declarative command registry (Stage 14b).

Public surface re-exported from :mod:`.registry` so the call-site is
``from telegram_invite_bot.commands import find_canonical, get_spec``
rather than the deeper module path.
"""

from __future__ import annotations

from telegram_invite_bot.commands.registry import (
    CommandSpec,
    find_canonical,
    get_spec,
    iter_commands,
)

__all__ = ("CommandSpec", "find_canonical", "get_spec", "iter_commands")
