"""Shared chat-type constants.

Single source of truth for the "is this a group chat?" check that every
group-only handler used to redeclare as a module-level ``_GROUP_TYPES``
copy. Two flavours, matching the two comparison styles in the codebase:

* :data:`GROUP_TYPES` — :class:`aiogram.enums.ChatType` members, for
  call sites comparing ``message.chat.type`` against the enum.
* :data:`GROUP_TYPE_NAMES` — plain strings, for call sites that compare
  the raw string value (do NOT swap those to the enum set — keep the
  comparison style each call site already uses).
"""

from __future__ import annotations

from aiogram.enums import ChatType

GROUP_TYPES = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})
GROUP_TYPE_NAMES = frozenset({"group", "supergroup"})
