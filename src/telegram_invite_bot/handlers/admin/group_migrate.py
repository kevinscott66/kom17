"""``/admin_group_migrate <old_id> <new_id>`` — hand-run the supergroup remap.

:mod:`~telegram_invite_bot.handlers.group_migration` catches the upgrade
live, which covers every group from now on. It cannot cover the ones
that upgraded while the bot had no handler for it — those groups already
have their rows stranded under an id that no update will ever carry
again, and no service message is coming a second time to fix them.

This is the repair tool for exactly that backlog. It is also the escape
hatch for the rarer live failure: a database that was locked for the
whole of the automatic run is named in the result's ``failed`` tuple and
needs someone to run the remap again.

Dev-only, and deliberately so. The remap rewrites group-keyed rows
across all five databases with no undo; the ids come straight off the
command line with nothing to cross-check them against, so a typo in the
destination silently merges two unrelated groups. That is a footgun
worth keeping in the developers' hands. The service refuses non-negative
and equal ids, which stops the two mistakes that would do the most
damage, but it cannot tell one valid group id from another.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from loguru import logger

from telegram_invite_bot.services.group_migration_service import migrate_group_id
from telegram_invite_bot.utils.aiogram import command_args
from telegram_invite_bot.utils.numbers import parse_int_token

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db.engines import EngineRegistry


log = logger.bind(component="handlers.admin.group_migrate")


_USAGE = (
    "<b>Использование:</b> <code>/admin_group_migrate &lt;old_id&gt; "
    "&lt;new_id&gt;</code>\n\n"
    "Переносит все данные группы со старого chat_id на новый — так, как "
    "это делается автоматически при апгрейде группы в супергруппу.\n"
    "Оба id должны быть отрицательными и различаться.\n\n"
    "<i>Пример:</i> <code>/admin_group_migrate -1234500011 "
    "-1002222222222</code>"
)


def _parse(body: str) -> tuple[int, int] | None:
    """``(old_id, new_id)`` from the command arguments, or ``None``.

    Only the shape is checked here — that there are exactly two tokens
    and both are integers. The semantic rules (negative, distinct) stay
    in the service, so they hold for the automatic path too and cannot
    drift between the two callers.
    """
    parts = body.split()
    if len(parts) != 2:
        return None
    old_id = parse_int_token(parts[0], signed=True)
    new_id = parse_int_token(parts[1], signed=True)
    if old_id is None or new_id is None:
        return None
    return old_id, new_id


async def handle_admin_group_migrate(
    message: Message,
    command: CommandObject,
    registry: EngineRegistry,
    settings: Settings,
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_group_migrate; silently dropped"
        )
        return

    # ``CommandObject.args`` is filled from text OR caption, so an
    # invocation typed under an attached screenshot keeps its
    # arguments (#105).
    parsed = _parse(command_args(command))
    if parsed is None:
        await message.answer(_USAGE)
        return
    old_id, new_id = parsed

    try:
        result = await migrate_group_id(registry, old_id=old_id, new_id=new_id)
    except ValueError as exc:
        # The service's own guard — negative-id and distinct-id. Its
        # message names the offending pair, and it is developer-facing.
        await message.answer(f"❌ {html.escape(str(exc))}")
        return

    lines = [
        "✅ <b>Данные группы перенесены</b>",
        "",
        f"Откуда: <code>{old_id}</code>",
        f"Куда: <code>{new_id}</code>",
        "",
        f"Перенесено строк: <code>{result.moved}</code>",
        f"Отброшено дублей: <code>{result.dropped}</code>",
        f"Объединено счётчиков: <code>{result.merged}</code>",
        f"Просмотрено колонок: <code>{result.columns}</code>",
    ]
    if result.skipped:
        # Rows still living under the dead id. Louder than the dropped
        # count because nothing else will report them.
        lines += [
            "",
            "⚠ <b>Колонки, которые не удалось обработать:</b> "
            f"<code>{html.escape(', '.join(result.skipped))}</code>",
            "<i>Строки под ними остались на старом id — нужна ручная правка.</i>",
        ]
    if result.failed:
        lines += [
            "",
            "⚠ <b>Базы, которые не удалось обработать:</b> "
            f"<code>{html.escape(', '.join(result.failed))}</code>",
            "<i>Повторный запуск команды безопасен и допереносит остаток.</i>",
        ]
    await message.answer("\n".join(lines))
    log.bind(
        user_id=user.id,
        old_id=old_id,
        new_id=new_id,
        moved=result.moved,
        dropped=result.dropped,
        merged=result.merged,
        columns=result.columns,
        failed=result.failed,
        skipped=result.skipped,
    ).info("/admin_group_migrate completed")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    router = Router(name="admin.group_migrate")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message, command: CommandObject) -> None:
        await handle_admin_group_migrate(message, command, registry, settings)

    router.message.register(_entry, Command("admin_group_migrate", ignore_case=True))
    return router
