"""``/admin_check_groups`` — developer-only DB diagnostics for groups.

Legacy ``/check_groups`` (bot.py:3503) dumps row counts and the first
five sample rows for ``users.bot_groups`` and ``users.group_settings``.
Operators reach for it when a group "looks missing" from the panel,
to confirm whether the row exists in the bot's own DB or the gap is
elsewhere (panel renderer, cache, Telegram membership probe).

Differences from legacy worth pinning:

* Private + developer-only, same gates as legacy.
* ``group_settings`` sample omits the ``features_mode`` column — it's
  not modelled yet in :mod:`db.models.users` and adding a column the
  new pipeline never reads otherwise would be additive-for-show only.
  Operators get the row count from group_settings, then read sample
  *rows* from ``bot_groups`` which IS modelled (chat_id, chat_title,
  added_by_user_id). When ``features_mode`` lands in a write-side
  port, extend the model and re-add the sample.
* No legacy ``get_all_groups_for_features_panel()`` derived count —
  that helper folds in Telegram-API membership probes and panel-
  level filtering, both out of scope for a pure DB diagnostic. The
  raw count is what the operator actually needs to disambiguate
  "the row exists" vs "the row doesn't exist".

Same silent-drop posture as :mod:`handlers.admin.status` for non-
developers — existence of the command must not be a side-channel
for enumerating dev IDs.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import func, select

from telegram_invite_bot.db.models.users import BotGroup, GroupSettings
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.check_groups")


_SAMPLE_SIZE = 5


async def _gather(registry: EngineRegistry) -> tuple[int, int, list[tuple[int, str | None, int]]]:
    """Three independent reads on ``users.db``: two counts + a sample.

    All against the same engine but each in its own ``connect()`` —
    keeps the function easy to reason about (each block is one query,
    failure of one doesn't poison the others) and the diagnostic is
    not hot-path so connection-reuse savings are immaterial. Same
    posture as legacy which opens a fresh sqlite3 cursor per block.
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        bot_groups_count = int(
            (await conn.execute(select(func.count()).select_from(BotGroup))).scalar_one()
        )
    async with engine.connect() as conn:
        group_settings_count = int(
            (await conn.execute(select(func.count()).select_from(GroupSettings))).scalar_one()
        )
    sample: list[tuple[int, str | None, int]] = []
    if bot_groups_count > 0:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    BotGroup.chat_id,
                    BotGroup.chat_title,
                    BotGroup.added_by_user_id,
                ).limit(_SAMPLE_SIZE)
            )
            sample = [(int(r[0]), r[1], int(r[2])) for r in rows.all()]
    return bot_groups_count, group_settings_count, sample


def _render(
    *,
    bot_groups_count: int,
    group_settings_count: int,
    sample: list[tuple[int, str | None, int]],
) -> str:
    lines = ["📊 <b>Groups diagnostics</b>", ""]
    lines.append(f"• bot_groups: <code>{bot_groups_count}</code>")
    lines.append(f"• group_settings: <code>{group_settings_count}</code>")
    if sample:
        lines.append("")
        lines.append("<b>bot_groups sample:</b>")
        for chat_id, title, added_by in sample:
            # Titles can contain HTML-special chars. The bot uses HTML
            # parse_mode by default — operators have free-text-typed
            # group titles via Telegram, so a literal ``<`` would break
            # the message render under HTML.
            safe_title = html.escape(title or "—")
            lines.append(
                f"  • <code>{chat_id}</code>: {safe_title} (added_by: <code>{added_by}</code>)"
            )
    return "\n".join(lines)


async def handle_admin_check_groups(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
) -> None:
    """Render the diagnostics card iff the caller is a recognised dev."""
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_check_groups; silently dropped"
        )
        return

    bot_groups_count, group_settings_count, sample = await _gather(registry)
    text_out = _render(
        bot_groups_count=bot_groups_count,
        group_settings_count=group_settings_count,
        sample=sample,
    )
    await message.answer(text_out)
    log.bind(
        user_id=user.id,
        bot_groups=bot_groups_count,
        group_settings=group_settings_count,
    ).info("/admin_check_groups rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only at the router level — the legacy command also
    short-circuits in non-private chats (bot.py:3508), and a group
    invocation would surface DB internals in front of regular
    members. The router-level filter keeps the handler's defence-in-
    depth body simple.
    """
    router = Router(name="admin.check_groups")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_check_groups(message, settings, registry)

    router.message.register(_entry, Command("admin_check_groups", ignore_case=True))
    return router
