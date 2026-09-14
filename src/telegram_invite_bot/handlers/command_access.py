"""Per-command minimum-rank gate — the /cmdcfg enforcement half (R4).

DESIGN_RANKS.md §2.2: a root OUTER message middleware (word-filter
posture: never raises into the dispatch pipeline) that maps an incoming
``/command`` to its catalog key (legacy ``COMMAND_CATALOG``,
bot.py:42339-42417) and rejects callers below the command's minimum
rank with a localized denial — legacy ``check_command_access``
semantics over ``command_rank_overrides`` (bot.py:42808-42894).

Decision sequence (cheapest first, all grants fail-CLOSED, the
infrastructure path fail-OPEN):

1. Not a ``/command`` text message → pass. "Is it a command" is
   decided on the LEFT-STRIPPED text, because that is what aiogram's
   filter decides it on (#404 — see :meth:`_denial_text`).
2. An anonymous admin of THIS chat (``sender_chat.id == chat.id``,
   the #246 test — a foreign ``sender_chat`` is a member posting as a
   channel they own and is NOT flagged, #1928) is flagged here but not
   yet passed: the rank half of this gate defers to the handler-level
   anonymous-admin policy (R-FIX-011, ``handlers/moderation``), while
   the kill switch in step 5 has no handler-side twin and must still
   reach them (#406). The pass therefore happens between steps 5 and 6.
3. Developer (``settings.bot.is_developer``) → pass (legacy precedence
   head, bot.py:42874).
4. Effective min rank = DB override else catalog default. ``<= 0`` →
   pass (the overwhelmingly common case; the override map is cached for
   300s in :mod:`repositories.rank_repo`, so this is usually one dict
   lookup).
5. ``6`` → command DISABLED for everyone but developers (legacy /cmdcfg
   "0=all … 6=disabled", bot.py:42808-42894) — localized notice, update
   consumed.
6. Actor rank (``RankService.get_rank`` — dev-pin, 300s cache,
   DB error degrades to 0, never raises) ``>= min`` → pass.
7. Live-TG-admin bypass in group chats — a DELIBERATE DIVERGENCE, not
   parity (#405). Legacy's rank gate had no such branch at all:
   ``check_command_access`` (bot.py:42858-42894) goes developer →
   ``required_rank >= 6`` → ``get_user_rank``, and nothing else. What
   legacy had instead was an auto-promote — the hourly
   ``sync_ranks_with_telegram_admins`` sweep raised a TG admin with
   real moderation rights from rank 0 to MODERATOR (bot.py:7479-7519),
   so by the time the gate ran the rank was already there. This port
   replaced that sweep with :func:`handlers.rank_self.lazy_staff_sync`,
   which looks only at the MAIN chat and defaults to demote-only
   (``RANK_AUTOSYNC=0``). Drop this bypass and the 11 catalog commands
   at rank 2 stop working for Telegram admins in every secondary group.
   The bypass is a live-state check, not a stored rank, so it is
   narrower than legacy's promotion in one direction (it grants nothing
   outside the moment) and wider in another (it needs no sweep). An API
   error (``None``) does NOT grant — fail-closed on grants, same
   posture as RankService.
8. Otherwise: localized below-rank denial, update consumed.

Any unexpected exception during 4-7 fails OPEN (the command passes
through to its handler, which carries its own authorisation) — a DB
hiccup must not brick every command; this mirrors the word-filter
middleware's never-raise contract.

That fail-OPEN is an availability trade, NOT safety-by-construction.
For most commands it only drops a restriction layered on top of a
handler gate, but two cases have no handler-side twin: ``min_rank == 6``
(the owner's kill switch, which lives ONLY here) and any command whose
catalog default is 0 that the owner RAISED via ``/cmdcfg set``. So step
4 snapshots every override map it reads successfully, and the except
branch still refuses kill-switched commands from that snapshot
(:meth:`CommandAccessMiddleware._stale_denial`). Raised-but-not-6
overrides do still fail open — re-deciding those needs the actor's
rank, i.e. the DB read that just failed.

That snapshot also outlives the process (#1917). An in-memory one is
empty at boot, which is exactly the moment it is most needed: a
restart into an unhealthy moderation.db would leave the kill switch
unenforced until the first read succeeds — and if it never succeeds,
forever. So every map that CHANGES is also written to
``<database_dir>/command_access_overrides.json``, and the first stale
denial of a process with no snapshot yet reads it back. Best-effort in
both directions: a file that cannot be written or cannot be parsed
degrades to the in-memory behaviour and is never allowed to raise into
the pipeline.

Orchestrator attach lines (main_router.py — LAST of the message outer
middlewares: after TextAlias + GroupAlias so rewritten aliases are gated
as their /command, and after both automods because a denial here
consumes the update and anything registered later would never see a
denied command at all (#1918). The earlier position, ahead of
MessageActivityMiddleware, was justified as "a denied command must not
earn coins" — that never applied: MessageActivity skips any text
starting with ``/`` regardless of who else ran)::

    root.message.outer_middleware(gate)
    root.callback_query.outer_middleware(gate)

The second line is #1428. The private ``/start`` welcome carries an
inline menu whose taps reach the same read surfaces as eight catalog
commands (``handlers/main_menu``), and a middleware on ``message``
alone never sees them — so ``/cmdcfg set balance 6`` switched off the
typed command and left the button working. The kill switch has no
handler-side twin (see the fail-OPEN note above), so that gap was the
whole switch for anyone who taps instead of types. Both registrations
share ONE instance on purpose: the stale-override snapshot that keeps
the kill switch alive through a DB failure is per-instance, and a
second instance would start with an empty one.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.ranks import command_key_for, default_min_rank, rank_name
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.repositories.rank_repo import (
    RankRepo,
    command_override_generation,
)
from telegram_invite_bot.services.rank_service import RankService
from telegram_invite_bot.utils.telegram_admin import is_user_admin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram import Bot

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="handlers.command_access")


#: Min rank that means "command disabled" (legacy /cmdcfg scale 0..6,
#: bot.py:42808-42894: 6 = developer-only = off for everyone else).
DISABLED_MIN_RANK: Final[int] = 6

#: #1428: main-menu inline actions that are also catalog commands, so a
#: tap reaches the same surface the ``/command`` does and must answer to
#: the same /cmdcfg setting. The map is an identity — the menu was built
#: out of the command names — but it is written out rather than derived,
#: so a new menu action cannot silently invent a catalog key (an unknown
#: key gets ``default_min_rank`` 0, i.e. a gate that always passes, which
#: would look like enforcement and be none). ``home`` is deliberately
#: absent: it re-renders the menu itself and has no command behind it.
_MENU_ACTION_KEYS: Final[dict[str, str]] = {
    "profile": "profile",
    "balance": "balance",
    "referral": "referral",
    "shop": "shop",
    "games": "games",
    "help": "help",
    "commission": "commission",
    "daily": "daily",
}

#: #1917: where the last good override map is kept across restarts,
#: under ``settings.paths.database_dir``. A cache, not a source of
#: truth — moderation.db owns the values, this file only answers the
#: single question :meth:`CommandAccessMiddleware._stale_denial` asks
#: when that DB is unreachable.
_SNAPSHOT_FILENAME: Final[str] = "command_access_overrides.json"

#: Schema marker in the snapshot file. Read back strictly: a file
#: written by a future version with a different shape is ignored rather
#: than half-understood, because a half-understood kill switch is worse
#: than none.
_SNAPSHOT_VERSION: Final[int] = 1

#: The help card pages in place, so its action carries the page number
#: inside itself (``help``, ``help2``, ``help3`` — see
#: :class:`keyboards.builders.main_menu.MainMenu`). Every page is the
#: same command.
_HELP_ACTION: Final[str] = "help"


def _write_snapshot(path: Path, overrides: dict[str, int]) -> None:
    """Atomically replace ``path`` with a JSON copy of ``overrides``.

    Same shape as :meth:`cms.guide_site.editor.FileEditorBridge._write`:
    the temp file is created in the destination directory so
    ``os.replace`` is a same-volume rename (atomic on POSIX and
    Windows), and the directory is created on demand so a deployment
    that has not written a database yet doesn't fail the first save.

    A half-written file here would be read back as the owner's kill
    switch, so "atomic" is the requirement, not the polish.

    Runs in a worker thread — see :meth:`_persist_snapshot`.

    #1940: the ``finally`` is not decoration. ``delete=False`` hands
    the temp file's lifetime to this function, and every save that
    raised between creation and rename used to leave a stray
    ``<name>.<random>.tmp`` behind for good. This path runs on every
    /cmdcfg change, in a directory the operator never lists.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(
                {"version": _SNAPSHOT_VERSION, "overrides": overrides},
                tmp,
                ensure_ascii=False,
                sort_keys=True,
            )
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        # Consumed by the rename — there is no longer a path to clean.
        tmp_name = ""
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


class CommandAccessMiddleware(BaseMiddleware):
    """Root outer message middleware enforcing /cmdcfg min ranks."""

    def __init__(self, registry: EngineRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings
        # Stateless service over APP-scoped refs — built once, reused.
        self._ranks = RankService(registry, settings)
        # Last override map read successfully. Kept so a LATER read
        # failure can still enforce the owner's kill switch; ``None``
        # until the first successful read — see ``_stale_denial``.
        self._last_overrides: dict[str, int] | None = None
        # #1917: the on-disk copy of that map, so a restart doesn't
        # start with an empty one. ``_snapshot_loaded`` makes the read
        # back a once-per-process attempt: a missing file is the normal
        # state of a fresh deployment, and re-stat'ing it on every
        # denial of a broken-DB window would buy nothing.
        self._snapshot_path = Path(settings.paths.database_dir) / _SNAPSHOT_FILENAME
        self._snapshot_loaded = False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            denial = await self._denial_text(event, data)
            if denial is not None:
                try:
                    await event.reply(denial)
                except Exception as exc:  # noqa: BLE001 — denial stands either way
                    log.bind(chat_id=event.chat.id, exc=repr(exc)).info(
                        "command-access denial reply failed",
                    )
                # Consume the update: the deny is the fail-closed
                # outcome; the handler must not run.
                return None
        elif isinstance(event, CallbackQuery):
            # #1428. Answered as an alert rather than replied: a callback
            # has no message of its own to answer, and Telegram shows an
            # unanswered tap as a spinner that never stops.
            denial = await self._callback_denial(event, data)
            if denial is not None:
                try:
                    await event.answer(denial, show_alert=True)
                except Exception as exc:  # noqa: BLE001 — denial stands either way
                    log.bind(callback=event.id, exc=repr(exc)).info(
                        "command-access denial answer failed",
                    )
                return None
        return await handler(event, data)

    async def _callback_denial(self, callback: CallbackQuery, data: dict[str, Any]) -> str | None:
        """Return the localized denial for a menu tap, or ``None`` to pass.

        Only taps this module recognises as a route to a catalog command
        are decided here; everything else on ``callback_query`` — every
        other router's buttons — passes through untouched, because an
        unrecognised payload cannot be mapped to a command and guessing
        would gate the whole bot on one dict lookup.

        The message path's two extra branches are absent on purpose:
        there is no anonymous actor (a callback always carries a real
        ``from_user``), and there is no live-TG-admin bypass because the
        menu is private-chat only (``handlers/main_menu.build_router``
        filters on ``ChatType.PRIVATE``) and that bypass exists solely
        for group chats.
        """
        raw = callback.data
        if not raw:
            return None
        try:
            action = MainMenu.unpack(raw).action
        except (ValueError, TypeError):
            # Not a main-menu payload — another router owns this tap.
            return None
        if action.startswith(_HELP_ACTION):
            action = _HELP_ACTION
        key = _MENU_ACTION_KEYS.get(action)
        if key is None:
            return None

        user_id = callback.from_user.id
        if self._settings.bot.is_developer(user_id):
            return None
        chat_id = callback.message.chat.id if isinstance(callback.message, Message) else None
        try:
            min_rank = await self._min_rank(key)
            if min_rank <= 0:
                return None

            lang = str(data.get("lang") or "ru")
            if min_rank >= DISABLED_MIN_RANK:
                log.bind(chat_id=chat_id, user=user_id, command=key).info(
                    "command-access: disabled command refused (menu tap)",
                )
                return t("h_cmdaccess_disabled", lang)

            rank = await self._ranks.get_rank(user_id)
            if rank >= min_rank:
                return None

            log.bind(
                chat_id=chat_id,
                user=user_id,
                command=key,
                rank=rank,
                min_rank=min_rank,
            ).info("command-access: below-min-rank menu tap refused")
            return t(
                "h_cmdaccess_denied",
                lang,
                rank_name=rank_name(min_rank, lang, in_group=True),
            )
        except Exception:  # noqa: BLE001 — availability fail-OPEN (word-filter posture)
            log.opt(exception=True).warning(
                "command-access check failed for menu '{key}'; failing open", key=key
            )
            return self._stale_denial(chat_id, data, key)

    async def _denial_text(self, message: Message, data: dict[str, Any]) -> str | None:
        """Return the localized denial to send (and consume the update),
        or ``None`` to pass the message through untouched."""
        # A command is whatever aiogram's ``Command`` filter will route,
        # and that filter reads ``message.text or message.caption``
        # (aiogram/filters/command.py). Gating on ``text`` alone left the
        # caption of any photo/video/document as a one-keystroke way past
        # this middleware — including past min-rank 6, the owner's kill
        # switch. Same posture as ``WordFilterAutomodMiddleware``.
        # ...and that filter parses with ``text.split(maxsplit=1)``
        # (aiogram/filters/command.py:131), which drops leading
        # whitespace before it ever looks at the prefix. So " /warn",
        # "\n/warn" and "\t/warn" all reach the handler while a raw
        # ``startswith("/")`` says they are not commands at all — a
        # second one-keystroke way past this middleware, kill switch
        # included (#404). Legacy had no such gap: ``bot.py:42782``
        # strips BEFORE testing the slash. Strip the same way the filter
        # does, then decide on the result.
        text = (message.text or message.caption or "").lstrip()
        if not text.startswith("/") or len(text) < 2:
            return None
        if message.from_user is None:
            return None
        # Anonymous admin of THIS chat: no per-user rank exists to look
        # up. The RANK half of this gate defers to the handler-level
        # anonymous policy (R-FIX-011) so it never narrows what those
        # handlers already allow — but the kill switch below has no
        # handler-side twin (see the module docstring), and legacy
        # refused these actors on it: ``bot.py:42878`` tests
        # ``required_rank >= 6`` before any rank lookup, so no actor
        # could reach a disabled command by having no rank. Hence the
        # flag, and the pass further down once ``min_rank`` is known
        # (#406).
        #
        # #1928: the test is ``sender_chat.id == chat.id``, the same line
        # #246 drew in ``handlers/moderation._is_anonymous_admin``, and
        # for the same reason — being sent on behalf of A chat is not
        # being sent on behalf of THIS one. Telegram sets ``sender_chat``
        # for an anonymous admin here (and vouches for the adminship
        # itself, because nobody else is offered that identity) and, with
        # the ``Channel_Bot`` placeholder in ``from``, for ANY member
        # posting as a channel they own — which most supergroups permit
        # and which proves only that they own a channel. The wider test
        # that stood here (a bare ``sender_chat is not None``, plus a
        # blanket ``is_bot`` that the placeholder also satisfies) handed
        # the second actor the first one's pass: one click in the "send
        # as" chooser and any member cleared a ``/cmdcfg set``
        # restriction on a command whose catalog default is 0 — exactly
        # the commands the module docstring names as having no
        # handler-side gate to fall back on. A gate that DEFERS to a
        # policy may not pass actors that policy refuses.
        #
        # Everything else chat-backed now takes the ordinary path: the
        # placeholder id has no rank (0) and is no live TG admin, so it
        # is refused, fail-closed.
        is_anonymous = message.sender_chat is not None and message.sender_chat.id == message.chat.id

        user_id = message.from_user.id
        if self._settings.bot.is_developer(user_id):
            return None

        key = command_key_for(text.split(maxsplit=1)[0])
        try:
            min_rank = await self._min_rank(key)
            if min_rank <= 0:
                return None

            lang = str(data.get("lang") or "ru")
            if min_rank >= DISABLED_MIN_RANK:
                log.bind(chat_id=message.chat.id, user=user_id, command=key).info(
                    "command-access: disabled command refused",
                )
                return t("h_cmdaccess_disabled", lang)

            if is_anonymous:
                # Kill switch cleared; the rank half is the handlers' (#406).
                return None

            if message.sender_chat is not None:
                # #1931: chat-backed but NOT this chat — a member posting
                # as a channel they own. #1928 correctly stopped treating
                # this as an anonymous admin, but then dropped the actor
                # into the rank branch below, which reads a rank for the
                # ``Channel_Bot`` placeholder id: it has none, so the
                # refusal arrived as "this command requires rank X". That
                # is misleading in the one direction that matters — the
                # person behind the channel may well hold that rank, and
                # the copy sends them looking at their rank instead of at
                # the "send as" chooser that is actually blocking them.
                # The refusal is the same (fail-closed either way); only
                # the reason it states is now true.
                log.bind(
                    chat_id=message.chat.id,
                    sender_chat_id=message.sender_chat.id,
                    from_id=user_id,
                    command=key,
                ).warning("command-access: refused a command sent on behalf of a foreign chat")
                return t("h_cmdaccess_channel_actor", lang)

            rank = await self._ranks.get_rank(user_id)
            if rank >= min_rank:
                return None

            # Live-TG-admin bypass (group chats only). NOT legacy
            # precedence — legacy's rank gate had no such branch at all
            # (bot.py:42858-42894); it relied on the auto-promote sweep
            # instead. See the module docstring for why we keep the
            # bypass anyway (#405). ``None`` (API error) must NOT grant —
            # fail-closed on grants.
            if message.chat.type in GROUP_TYPES:
                bot = data.get("bot")
                if bot is not None and await self._is_admin(bot, message.chat.id, user_id):
                    return None

            log.bind(
                chat_id=message.chat.id,
                user=user_id,
                command=key,
                rank=rank,
                min_rank=min_rank,
            ).info("command-access: below-min-rank command refused")
            return t(
                "h_cmdaccess_denied",
                lang,
                rank_name=rank_name(min_rank, lang, in_group=True),
            )
        except Exception:  # noqa: BLE001 — availability fail-OPEN (word-filter posture)
            log.opt(exception=True).warning(
                "command-access check failed for '{key}'; failing open", key=key
            )
            return self._stale_denial(message.chat.id, data, key)

    def _stale_denial(self, chat_id: int | None, data: dict[str, Any], key: str) -> str | None:
        """Kill-switch-only gate from the last good override snapshot.

        The broad fail-OPEN above is an availability decision, and for
        almost every command it costs nothing: the handler behind it
        carries its own authorisation, so losing this middleware loses a
        second lock, not the only one. ``min_rank == DISABLED_MIN_RANK``
        is the exception — that setting exists ONLY here, so failing
        open on it hands every user a command the owner switched off.

        Once the override map has been read even once we can answer that
        one question without touching the DB again, so we do. Everything
        else still falls through: a raised-but-not-6 override needs the
        actor's rank to decide, and that is the read which just failed.
        Never raises — it runs inside the ``except`` block whose whole
        point is the never-brick contract.

        "Read even once" includes a previous PROCESS since #1917: with
        no in-memory snapshot the on-disk one is loaded here, once. That
        is the whole point of persisting it — a restart into a broken
        moderation.db is precisely when there is nothing in memory yet.
        """
        try:
            if self._last_overrides is None and not self._snapshot_loaded:
                self._snapshot_loaded = True
                self._last_overrides = self._load_snapshot()
            overrides = self._last_overrides
            if overrides is None or overrides.get(key, 0) < DISABLED_MIN_RANK:
                return None
            log.bind(chat_id=chat_id, command=key).info(
                "command-access: disabled command refused from stale snapshot",
            )
            return t("h_cmdaccess_disabled", str(data.get("lang") or "ru"))
        except Exception:  # noqa: BLE001 — never-raise contract
            log.opt(exception=True).warning("stale command-access snapshot unusable")
            return None

    async def _is_admin(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """True only on a CONFIRMED live admin status (None → False)."""
        return await is_user_admin(bot, chat_id, user_id) is True

    async def _min_rank(self, key: str) -> int:
        """Effective minimum rank for ``key``: DB override else catalog.

        The override map is 300s-cached module-level in ``rank_repo``,
        so the short moderation.db session here is only actually used
        once per TTL window. A map that comes back is snapshotted on the
        middleware so a later failure can still enforce the owner's kill
        switch — see :meth:`_stale_denial`. #1977: *a* map, not every
        one. A read that an invalidation overtook is answered but not
        stored, because the snapshot must never be older than the last
        ``/cmdcfg``.
        """
        generation = command_override_generation().snapshot()
        sessionmaker = self._registry.session(DBName.MODERATION)
        async with sessionmaker() as session:
            overrides = await RankRepo(session).command_overrides()
        if not command_override_generation().unchanged(generation):
            # #1977: ``/cmdcfg`` committed while we were suspended, so
            # this map predates it. Returning it is fine — the answer is
            # as fresh as the read that produced it, and this update is
            # the one that raced. Storing it is not: the snapshot is
            # what enforces the kill switch when moderation.db cannot be
            # read, and last-writer-wins here would put the switched-off
            # command back for everyone. Same reasoning, and the same
            # counter, as the cache fill in ``rank_repo`` (#1942); a
            # false positive costs one skipped store.
            return overrides.get(key, default_min_rank(key))
        changed = overrides != self._last_overrides
        # Before the ``await`` below, not after: nothing may interleave
        # between the check above and this assignment.
        self._last_overrides = overrides
        if changed:
            # Only a CHANGED map is written. This runs on every command,
            # while the map changes when the owner runs ``/cmdcfg`` —
            # the equality test is what keeps a per-command ``fsync``
            # from being the cost of this feature.
            await self._persist_snapshot(overrides)
        return overrides.get(key, default_min_rank(key))

    async def _persist_snapshot(self, overrides: dict[str, int]) -> None:
        """Write ``overrides`` to :attr:`_snapshot_path`, best-effort.

        Off the event loop: the write ends in an ``fsync``, and this
        middleware sits in front of every message in the bot. Failures
        are logged and dropped — the snapshot is a convenience for a
        degraded mode, so nothing about it may make the healthy path
        fail.
        """
        try:
            await asyncio.to_thread(_write_snapshot, self._snapshot_path, overrides)
        except Exception:  # noqa: BLE001 — cache write, never fatal
            log.opt(exception=True).warning(
                "command-access: could not persist override snapshot to {path}",
                path=self._snapshot_path,
            )

    def _load_snapshot(self) -> dict[str, int] | None:
        """Read the persisted map back, or ``None`` if there isn't a
        usable one. Sync on purpose: the single caller is
        :meth:`_stale_denial`, which is itself synchronous and runs at
        most once per process with a snapshot missing."""
        try:
            raw = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # The normal state of a deployment where /cmdcfg was never used.
            return None
        except (OSError, ValueError):
            log.opt(exception=True).warning(
                "command-access: override snapshot at {path} unreadable",
                path=self._snapshot_path,
            )
            return None
        if not isinstance(raw, dict) or raw.get("version") != _SNAPSHOT_VERSION:
            log.bind(path=str(self._snapshot_path)).warning(
                "command-access: override snapshot has an unknown shape; ignoring",
            )
            return None
        overrides = raw.get("overrides")
        if not isinstance(overrides, dict):
            return None
        # Filtered rather than trusted: the file is on the same disk as
        # the databases, and one junk entry must not decide a gate.
        # ``bool`` is excluded because it is an ``int`` in Python and
        # ``True`` would read as rank 1.
        return {
            key: value
            for key, value in overrides.items()
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
        }
