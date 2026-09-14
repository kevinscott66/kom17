"""Follow a group through its supergroup upgrade.

Telegram announces the upgrade twice — a service message in the old
basic group carrying ``migrate_to_chat_id``, and one in the freshly
created supergroup carrying ``migrate_from_chat_id``. Either is enough
to learn the pair, and both are handled because delivery of the first is
not guaranteed: a bot added to the group after the upgrade, or one whose
webhook was down for the minute the old chat still existed, only ever
sees the second.

Handling both is safe precisely because
:func:`~telegram_invite_bot.services.group_migration_service.migrate_group_id`
is idempotent — the second announcement finds nothing left under the old
id and walks away having moved zero rows.

Passive and silent: no reply is posted. The people in the group did not
ask for a bookkeeping notice and would not know what to do with one; the
correct visible outcome of a migration is that nothing appears to have
changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from loguru import logger

from telegram_invite_bot.services.group_migration_service import migrate_group_id

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db.engines import EngineRegistry

log = logger.bind(component="handlers.group_migration")


def _pair(message: Message) -> tuple[int, int] | None:
    """``(old_id, new_id)`` from whichever half of the pair this is."""
    if message.migrate_to_chat_id is not None:
        # Posted in the group that is going away; ``chat.id`` is the old one.
        return message.chat.id, message.migrate_to_chat_id
    if message.migrate_from_chat_id is not None:
        # Posted in the new supergroup; ``chat.id`` is already the new one.
        return message.migrate_from_chat_id, message.chat.id
    return None


async def handle_migration(message: Message, registry: EngineRegistry) -> None:
    pair = _pair(message)
    if pair is None:  # pragma: no cover — the router filter guarantees one
        return
    old_id, new_id = pair
    bound = log.bind(old_id=old_id, new_id=new_id)
    if old_id == new_id:
        # Telegram has never sent this, but the remap would refuse it and
        # the refusal is an exception — cheaper to notice it here.
        bound.warning("group migration announced with identical ids; ignoring")
        return

    try:
        result = await migrate_group_id(registry, old_id=old_id, new_id=new_id)
    except Exception as exc:  # noqa: BLE001 — a service message must never raise
        bound.opt(exception=exc).error("group migration failed")
        return

    bound.bind(moved=result.moved, dropped=result.dropped, failed=result.failed).info(
        "group upgraded to supergroup; data carried over"
    )


def build_router(registry: EngineRegistry) -> Router:
    """Router for the two halves of the supergroup-upgrade announcement.

    Chat-type filtered like every other group handler, and deliberately
    accepting both ``GROUP`` and ``SUPERGROUP``: the old-side message
    arrives from a basic group and the new-side one from a supergroup,
    so narrowing to either would silently drop half the coverage.
    """
    router = Router(name="group_migration")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(message: Message) -> None:
        await handle_migration(message, registry)

    router.message.register(
        _entry, F.migrate_to_chat_id.is_not(None) | F.migrate_from_chat_id.is_not(None)
    )
    return router
