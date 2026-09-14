"""Per-group dynamic command aliases — admin commands + routing middleware (L-60).

Legacy semantics (VERIFIED against the telebot monolith):

* ``/alias add <слово> <команда>`` / ``/alias del <слово>`` /
  ``/alias list`` — bot.py:42074 ``cmd_alias`` (``/aliases`` is a second
  trigger of the SAME handler, bot.py:42074 ``commands=['alias',
  'aliases']``; legacy has no separate "/aliases list" handler — any
  ``/aliases …`` call lands in ``cmd_alias`` and needs the ``list``
  action word too. Here ``/aliases`` with no args IS the list, which is
  a superset).
* Gate: legacy is **owner-only** (``is_owner``, bot.py:42083) and
  the alias map is **global** (``settings["command_aliases"]``,
  bot.py:42099-42103). Backlog L-60 deliberately re-scopes the feature
  to per-group aliases managed by group admins — so the gate here is
  ``handlers.moderation._require_admin`` (live Telegram admin check),
  and storage is per-``group_id``. Where legacy and L-60 disagree,
  the L-60 scoping wins.
* Word normalisation: ``normalize_alias_token`` (bot.py:41951-41953) —
  lower-case, strip all non-``[\\wа-яё]``. Reused verbatim via
  :func:`~telegram_invite_bot.repositories.group_aliases_repo.normalize_alias_word`.
* Target validation: legacy accepts only commands present in its
  ``get_shortcut_handler_map()`` whitelist (bot.py:42019-42038,
  42125-42136) — i.e. a real, registered command; an alias can never
  point at another alias. The new pipeline has no static handler map to
  introspect, so the port validates shape (``^[a-z][a-z0-9_]*$``, ≤32
  chars — the Bot-API command grammar) and refuses chaining (target may
  not be an existing alias word in the group). A typo'd target simply
  dispatches to nothing, same as legacy's unmapped-command fallthrough.
* Re-adding an existing word OVERWRITES its mapping
  (``aliases_cfg[alias_key] = command_str``, bot.py:42138).
* Firing: legacy resolves the FIRST token and passes the remainder of
  the payload through as arguments (``message.text = f"{command}
  {rest}"``, bot.py:43627). :class:`GroupAliasMiddleware` mirrors
  that: first token of a plain group message (normalised) matches an
  alias word → the message is rewritten to ``/target rest`` and normal
  dispatch picks it up. (Legacy additionally demanded a ``./?/!/бот``
  prefix in groups because its aliases were global noise-prone words;
  per-group aliases are explicit admin opt-ins, so L-60 fires them
  bare — that is the point of the feature.)

The routing middleware is self-contained (own short-lived
``moderation.db`` session per cache miss + TTL/LRU cache) and mirrors
the rewrite trick of ``middlewares.text_alias.TextAliasMiddleware``
(``message.model_copy(update={"text": …, "entities": None})``) — it
lives HERE, next to the aliases it serves, for the same reason as
``handlers.wordfilter.WordFilterAutomodMiddleware``.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.moderation import _require_admin
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.repositories.group_aliases_repo import (
    GroupAliasRepo,
    normalize_alias_word,
)
from telegram_invite_bot.utils.aiogram import command_body
from telegram_invite_bot.utils.render import paginate_lines
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry

log = logger.bind(component="handlers.group_aliases")


# Legacy is unbounded (a settings dict); sane ceilings keep the
# per-message middleware lookup map small and prevent abuse.
MAX_WORD_LENGTH: int = 32
MAX_ALIASES_PER_GROUP: int = 100

# Bot-API command grammar (lowercase latin / digits / underscore, ≤32).
# Legacy instead checked membership in get_shortcut_handler_map()
# (bot.py:42125-42136); see module docstring for why shape-validation
# replaces the whitelist here.
_TARGET_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def normalize_target(raw: str) -> str | None:
    """Canonical bare command name from user input, or ``None`` if invalid.

    Mirrors legacy coercion (bot.py:42121-42124): prepend ``/`` if
    missing, keep only the first token — then validate against the
    Bot-API command grammar and return the name WITHOUT the slash.
    """
    token = raw.strip().split()[0] if raw.strip() else ""
    token = token.removeprefix("/").lower()
    if not _TARGET_RE.match(token):
        return None
    return token


# ---------------------------------------------------------------------------
# Admin commands: /alias add|del, /aliases (list)
# ---------------------------------------------------------------------------


async def handle_alias(
    message: Message,
    bot: Bot,
    group_alias_repo: GroupAliasRepo,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/alias add <word> <command>`` | ``/alias del <word>`` | ``/alias list``."""
    if not await _require_admin(message, bot, settings, lang):
        return

    parts = command_body(message).split()
    action = parts[1].lower() if len(parts) > 1 else ""

    if action == "list" or not action:
        # Legacy replies with usage when no action (bot.py:42088-42095)
        # and so do we — this branch is a faithful port. The listing that
        # an earlier version of this comment claimed for the bare call
        # lives in ``_aliases`` (:266-270), on a different command (#716).
        if not action:
            await message.reply(t("h_galias_usage", lang))
            return
        await _send_list(message, group_alias_repo, lang)
        return

    if action == "add":
        if len(parts) < 4:
            await message.reply(t("h_galias_usage_add", lang))
            return
        word = normalize_alias_word(parts[2])
        if not word or len(word) > MAX_WORD_LENGTH:
            # Legacy: "Некорректный алиас" (bot.py:42126-42128).
            await message.reply(t("h_galias_bad_word", lang))
            return
        target = normalize_target(parts[3])
        if target is None:
            # Legacy rejects commands outside its whitelist
            # (bot.py:42129-42136); here = malformed command name.
            await message.reply(t("h_galias_bad_target", lang))
            return
        existing = await group_alias_repo.mapping(group_id=message.chat.id)
        # No chaining: the target may not itself be an alias word here
        # (legacy guaranteed this structurally — targets had to be real
        # handler-map commands, never alias words).
        if target in existing and word != target:
            await message.reply(t("h_galias_chain_rejected", lang))
            return
        if word not in existing and len(existing) >= MAX_ALIASES_PER_GROUP:
            await message.reply(t("h_galias_limit_reached", lang))
            return
        created = await group_alias_repo.upsert(
            group_id=message.chat.id,
            word=word,
            target_command=target,
            added_by=message.from_user.id if message.from_user else None,
        )
        # #1878: ``upsert`` flushes its write (group_aliases_repo.py:86),
        # so ``moderation.db`` is under ``BEGIN IMMEDIATE`` from here
        # until the session middleware commits. All that is left in the
        # handler is a Telegram reply: hold the writer slot across it and
        # every other admin command in the group queues behind one
        # FloodWait, and a confirmation that fails to send takes the
        # alias down with it — silently, since the admin was told
        # nothing and has no reason to re-check.
        if checkpoint is not None:
            await checkpoint()
        key = "h_galias_added" if created else "h_galias_updated"
        await message.reply(t(key, lang, word=html.escape(word), command=target))
        return

    if action in {"del", "delete", "remove", "rm"}:
        # Legacy accepts all four spellings (bot.py:42144).
        if len(parts) < 3:
            await message.reply(t("h_galias_usage_del", lang))
            return
        word = normalize_alias_word(parts[2])
        removed = await group_alias_repo.remove(group_id=message.chat.id, word=word)
        # #1878: a guarded DELETE takes the write lock in order to tell
        # us it matched nothing, so the commit is owed on BOTH outcomes
        # — not only on the one that removed a row.
        if checkpoint is not None:
            await checkpoint()
        if removed:
            await message.reply(t("h_galias_deleted", lang, word=html.escape(word)))
        else:
            # Legacy: "Такой алиас не найден" (bot.py:42155).
            await message.reply(t("h_galias_not_found", lang))
        return

    # Legacy: "Неизвестное действие" (bot.py:42158).
    await message.reply(t("h_galias_unknown_action", lang))


async def handle_aliases_list(
    message: Message,
    bot: Bot,
    group_alias_repo: GroupAliasRepo,
    settings: Settings,
    lang: str,
) -> None:
    """``/aliases`` — list this group's aliases (legacy ``/alias list``)."""
    if not await _require_admin(message, bot, settings, lang):
        return
    await _send_list(message, group_alias_repo, lang)


async def _send_list(message: Message, group_alias_repo: GroupAliasRepo, lang: str) -> None:
    rows = await group_alias_repo.list(group_id=message.chat.id)
    if not rows:
        # Legacy: "Дополнительных алиасов пока нет" (bot.py:42107).
        await message.reply(t("h_galias_list_empty", lang))
        return
    # Same 4096 trap ``/filter_list`` fell into: at the documented
    # ceiling (100 aliases, both sides up to 32 characters) one message
    # would be ~7000 characters and Telegram would refuse it silently.
    pages = paginate_lines(
        t("h_galias_list_header", lang, count=len(rows)),
        [
            f"• <code>{html.escape(word)}</code> → <code>/{html.escape(target)}</code>"
            for word, target in rows
        ],
        more_line=lambda count: t("h_galias_list_more", lang, count=count),
    )
    await message.reply(pages[0])
    for page in pages[1:]:
        await message.answer(page)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the /alias // /aliases admin-command router (group-only)."""
    router = Router(name="group_aliases")
    router.message.middleware(_GroupAliasRepoMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _alias(
        message: Message,
        bot: Bot,
        group_alias_repo: GroupAliasRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_alias(message, bot, group_alias_repo, settings, lang, checkpoint)

    router.message.register(
        _alias,
        Command("alias", ignore_case=True),
        F.from_user,
        group_filter,
    )

    async def _aliases(
        message: Message,
        bot: Bot,
        group_alias_repo: GroupAliasRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        # Legacy routes /aliases into the same cmd_alias handler
        # (bot.py:42074) — "/aliases list" must keep working; a bare
        # "/aliases" lists directly.
        parts = command_body(message).split()
        if len(parts) > 1:
            await handle_alias(message, bot, group_alias_repo, settings, lang, checkpoint)
            return
        await handle_aliases_list(message, bot, group_alias_repo, settings, lang)

    router.message.register(
        _aliases,
        Command("aliases", ignore_case=True),
        F.from_user,
        group_filter,
    )

    return with_chat_type_refusal(router, scope="group")


# ---------------------------------------------------------------------------
# Repo-binding middleware (group_alias_repo on the moderation.db session)
# ---------------------------------------------------------------------------


class _GroupAliasRepoMiddleware(BaseSessionMiddleware):
    """One ``moderation`` session per update; expose :class:`GroupAliasRepo`.

    Same pattern as ``handlers.wordfilter._WordFilterRepoMiddleware``.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.MODERATION)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["group_alias_repo"] = GroupAliasRepo(session)


# ---------------------------------------------------------------------------
# Routing outer middleware (alias word → /command rewrite)
# ---------------------------------------------------------------------------

# How long a per-group alias map is trusted before a DB re-read — same
# trade-off as the word-filter automod cache (fresh alias usable within
# seconds; busy chats are not one-read-per-message).
_CACHE_TTL_SECONDS: float = 30.0
_MAX_CACHED_GROUPS: int = 2000


class GroupAliasMiddleware(BaseMiddleware):
    """Rewrite a group message starting with an alias word to its ``/command``.

    Mounted by ``main_router`` as a root **outer** message middleware,
    AFTER ``TextAliasMiddleware`` (static legacy shortcuts win — they
    are the established surface) and BEFORE
    ``MessageActivityMiddleware`` (an intercepted alias is a command,
    not chatter — must not earn passive coins, matching legacy where a
    resolved shortcut never reached the chatter counter).

    Rewrite trick mirrors ``TextAliasMiddleware._maybe_rewrite``: hand a
    ``model_copy`` of the message with ``text`` replaced and ``entities``
    dropped to the rest of the chain, so normal command dispatch
    (filters, DI, throttling) treats it exactly as if the user typed the
    slash command. Argument pass-through mirrors legacy
    ``message.text = f"{command} {rest}"`` (bot.py:43627).

    Best-effort: a failed alias-map read is logged and the message
    passes through unchanged — routing must NEVER raise into dispatch.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        self._registry = registry
        self._cache: TTLLRUCache[int, dict[str, str]] = TTLLRUCache(
            _CACHE_TTL_SECONDS, _MAX_CACHED_GROUPS
        )

    async def _mapping_for(self, group_id: int) -> dict[str, str]:
        now = time.monotonic()
        cached = self._cache.get(group_id, now)
        if cached is not None:
            return cached
        sessionmaker = self._registry.session(DBName.MODERATION)
        async with sessionmaker() as session:
            mapping = await GroupAliasRepo(session).mapping(group_id=group_id)
        self._cache.put(group_id, mapping, now)
        return mapping

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            rewritten = await self._maybe_rewrite(event, data)
            if rewritten is not None:
                return await handler(rewritten, data)
        return await handler(event, data)

    async def _maybe_rewrite(self, message: Message, data: dict[str, Any]) -> Message | None:
        if message.chat.type not in GROUP_TYPES:
            return None
        if message.from_user is None or message.from_user.is_bot:
            return None
        text = message.text
        if not text:
            return None
        raw = text.strip()
        if not raw or raw.startswith("/"):
            return None

        # Never hijack an in-progress FSM text step — the same guard
        # TextAliasMiddleware carries (middlewares/text_alias.py:457-463),
        # including the fast path #1039 gave it and this middleware went
        # without until #1558. aiogram's ``FSMContextMiddleware`` is an
        # update-level OUTER middleware and resolves ``raw_state`` before
        # any router middleware runs (``aiogram/fsm/middleware.py:42``),
        # so the unconditional ``await state.get_state()`` that used to
        # stand here paid a storage round-trip on every plain group
        # message — under ``FSM_BACKEND=sqlite``, which is what
        # production runs, a database read per message, on the surface
        # that sees the most traffic in the whole bot.
        #
        # The ``state`` path stays as the fallback for the same reason it
        # does there: a hand-built ``data`` (tests, a bare dispatcher) can
        # carry the context without the resolved key, and silently
        # skipping the guard would let an alias hijack an FSM step.
        if "raw_state" in data:
            in_fsm_step = data["raw_state"] is not None
        else:
            state = data.get("state")
            in_fsm_step = state is not None and await state.get_state() is not None
        if in_fsm_step:
            return None

        parts = raw.split(maxsplit=1)
        word = normalize_alias_word(parts[0])
        if not word:
            return None

        try:
            mapping = await self._mapping_for(message.chat.id)
        except Exception as exc:  # noqa: BLE001 — routing must never raise
            log.bind(chat_id=message.chat.id, exc=repr(exc)).warning(
                "group-alias map read failed; passing message through",
            )
            return None

        target = mapping.get(word)
        if target is None:
            return None

        rest = parts[1].strip() if len(parts) > 1 else ""
        canonical = f"/{target} {rest}".strip()
        log.bind(chat_id=message.chat.id, word=word, command=target).debug(
            "group-alias: rewriting message to mapped command",
        )
        return message.model_copy(update={"text": canonical, "entities": None})
