"""Per-group antiflood — auto-mute message bursts (L-56, cluster F4).

A self-contained **outer** message middleware (``main_router`` mounts it
on the root router, next to :class:`handlers.wordfilter.WordFilterAutomodMiddleware`)
that watches every group message and, when a non-admin user sends more
than ``flood_max_msgs`` messages within ``flood_window_sec`` seconds in a
group with ``antiflood_enabled`` set, best-effort mutes them for
``flood_mute_minutes`` minutes via ``bot.restrict_chat_member`` and posts
one localized notice per mute (NOT one per flooding message).

Configuration lives on the existing per-group ``group_mod_config`` row
(four columns added by migration ``0006_antiflood_config``) and is
managed through ``/modcfg``::

    /modcfg antiflood on      — enable (OFF by default; legacy had none)
    /modcfg floodmax 5        — burst threshold, messages
    /modcfg floodwin 10       — sliding-window length, seconds
    /modcfg floodmute 10      — auto-mute duration, minutes

Design constraints (mirroring the word-filter automod):

* NEVER raises and NEVER consumes the update — economy / stats /
  welcome middlewares and command handlers always still run. Every
  Telegram/DB call is wrapped; a failure is logged and swallowed.
* Config reads are cached per group with a short TTL + LRU bound, so a
  busy chat is not one-DB-read-per-message.
* The sliding-window counters are in-process and LRU-bounded; a restart
  forgets in-flight windows (acceptable — antiflood is best-effort
  hygiene, not an audit system). The mute it hands out, however, IS
  audited: #253 writes it to ``moderation_log`` like every other
  sanction, so ``/groupadmin → Статистика`` counts it and the last-five
  log names it.
* The bot owner is exempt unconditionally (#1863). They are let
  through every other moderation gate (``moderation._require_admin``)
  and are not necessarily an administrator of a group their own bot
  serves, so without this a burst of their own messages would have the
  bot mute its owner. The check is local and comes first, so it costs
  no round-trip — the same shape (and the same reasoning) as
  ``wordfilter._is_exempt_from_sanction``.
* Admins are exempt too, and here "admin" means ANY admin status —
  :func:`~telegram_invite_bot.utils.telegram_admin.is_chat_admin_any`,
  not the narrow moderation-rights predicate the command gates use.
  This is not an authority question: nobody is being granted a power, a
  sanction is being withheld, so the safe direction is the wide one. It
  is also the direction with no legacy to match — legacy has no group
  antiflood at all. (The token ``flood`` does not occur anywhere in
  ``bot.py``; the word it does use, «антифлуд», is a different feature —
  a per-user cap on AI requests, 8/minute with a 5 s pause, advertised
  by ``/ai_limits`` at ``bot.py:38379`` and ``:38386``. It counts one
  user's calls to one service, not messages in a chat.) So #337's
  narrowing of the command gates carries no parity argument over here.
  The check is only performed when a user actually trips the threshold
  — never on the per-message hot path — and its verdict is cached
  briefly so a sustained flood does not hammer ``get_chat_member``. An
  API error (``None`` verdict) is treated as "admin" — fail-safe: we
  would rather miss a mute than mute an admin.
* Anonymous-admin / channel posters (``sender_chat`` set, or no
  ``from_user``) and bots are skipped — they cannot be restricted.

This module lives under ``handlers/`` (not ``middlewares/``) because it
belongs to the antiflood feature rather than the shared stack; the class
itself is a standard aiogram ``BaseMiddleware``.
"""

from __future__ import annotations

import html
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from telegram_invite_bot.utils.chat_permissions import MUTED_PERMS
from telegram_invite_bot.utils.telegram_admin import is_chat_admin_any
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.group_mod_config_repo import (
        GroupModConfigView,
    )

log = logger.bind(component="handlers.antiflood")


# How long a per-group antiflood config is trusted before a DB re-read.
# Same trade-off as the word-filter automod: a /modcfg change takes
# effect within seconds, while a busy chat stays off the DB hot path.
_CONFIG_TTL_SECONDS: float = 30.0
_MAX_CACHED_GROUPS: int = 2000

# How long a confirmed admin/non-admin verdict is trusted. Only consulted
# when a user trips the flood threshold, so the bound is on API chatter
# during a sustained flood, not on normal traffic.
_ADMIN_TTL_SECONDS: float = 60.0
_MAX_CACHED_ADMIN_VERDICTS: int = 4000

# Bound on tracked (chat, user) sliding windows.
_MAX_TRACKED_WINDOWS: int = 10000


class _SlidingWindows:
    """LRU-bounded per-(chat, user) sliding-window message counters."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._data: OrderedDict[tuple[int, int], deque[float]] = OrderedDict()

    def hit(self, key: tuple[int, int], now: float, window: float) -> int:
        """Record one message at ``now``; return the count within ``window``."""
        bucket = self._data.get(key)
        if bucket is None:
            bucket = deque()
            self._data[key] = bucket
        bucket.append(now)
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        self._data.move_to_end(key)
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)
        return len(bucket)

    def reset(self, key: tuple[int, int]) -> None:
        self._data.pop(key, None)


class AntifloodMiddleware(BaseMiddleware):
    """Auto-mute per-group message bursts (L-56).

    Mounted by ``main_router`` as an **outer** message middleware on the
    root router so it observes every group message — including plain
    chatter no command handler claims — without consuming the update.

    Self-contained: opens its own short-lived ``moderation.db`` session
    per config cache-miss; depends on no repo another middleware binds.
    """

    def __init__(self, registry: EngineRegistry, settings: Settings) -> None:
        self._registry = registry
        # #1863: only ``bot.is_developer`` is read, but the whole
        # Settings object is held rather than the id set, so the owner
        # list this middleware honours is the same object every other
        # gate reads — one source of truth, no snapshot to drift.
        # Mirrors ``WordFilterAutomodMiddleware``.
        self._settings = settings
        self._config_cache: TTLLRUCache[Any, Any] = TTLLRUCache(
            _CONFIG_TTL_SECONDS, _MAX_CACHED_GROUPS
        )
        self._admin_cache: TTLLRUCache[Any, Any] = TTLLRUCache(
            _ADMIN_TTL_SECONDS, _MAX_CACHED_ADMIN_VERDICTS
        )
        self._windows = _SlidingWindows(_MAX_TRACKED_WINDOWS)
        # (chat_id, user_id) -> already-muted marker with an explicit
        # per-entry deadline (``put_until``): suppresses duplicate
        # restrict calls and repeat notices (one notice per mute, not
        # one per flooding message).
        self._muted_until: TTLLRUCache[Any, Any] = TTLLRUCache(
            ttl=0.0, capacity=_MAX_CACHED_ADMIN_VERDICTS
        )

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            await self._maybe_act(event, data)
        except Exception as exc:  # noqa: BLE001 — antiflood must never raise
            chat_id = event.chat.id if isinstance(event, Message) else None
            log.bind(chat_id=chat_id, exc=repr(exc)).warning(
                "antiflood check failed; passing message through",
            )
        return await handler(event, data)

    # -- config ------------------------------------------------------------

    async def _config_for(self, group_id: int, now: float) -> GroupModConfigView:
        cached = self._config_cache.get(group_id, now)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        sessionmaker = self._registry.session(DBName.MODERATION)
        async with sessionmaker() as session:
            cfg = await GroupModConfigRepo(session).get_or_default(group_id)
        self._config_cache.put(group_id, cfg, now)
        return cfg

    # -- admin exemption ----------------------------------------------------

    async def _is_exempt_admin(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """True if the user must not be auto-muted for this burst.

        #1863: the bot owner comes first and is answered locally — they
        pass every other moderation gate and need not be an admin of a
        group their bot serves, so the alternative is a bot that mutes
        its own owner. This closes the divergence #1848 introduced,
        where ``wordfilter._is_exempt_from_sanction`` honoured the owner
        and this twin did not.

        The Telegram probe is deliberately the WIDE one — a title-only
        administrator is exempt from antiflood even though #337 stopped
        them from issuing ``/mute`` themselves. See the module
        docstring.

        Verdicts are TTL-cached; an API error (``None``) is treated as
        admin (fail-safe — never mute on uncertainty) and cached too, so
        a Telegram outage does not turn into a get_chat_member storm.
        """
        if self._settings.bot.is_developer(user_id):
            return True
        now = time.monotonic()
        key = (chat_id, user_id)
        cached = self._admin_cache.get(key, now)
        if cached is not None:
            return bool(cached)
        verdict = await is_chat_admin_any(bot, chat_id, user_id)
        exempt = verdict is None or verdict
        self._admin_cache.put(key, exempt, now)
        return exempt

    # -- core ----------------------------------------------------------------

    async def _maybe_act(self, event: TelegramObject, data: dict[str, Any]) -> None:
        if not isinstance(event, Message):
            return
        if event.chat.type not in GROUP_TYPES:
            return
        user = event.from_user
        # Anonymous admins / channel posters (sender_chat) and bots cannot
        # be restricted — and service messages carry no flooding intent.
        if user is None or user.is_bot or event.sender_chat is not None:
            return

        now = time.monotonic()
        key = (event.chat.id, user.id)

        # Already muted by us within this window? One notice per mute:
        # Telegram should be dropping their messages anyway, but media
        # races / restrict failures can leak a few — stay silent. The
        # marker is claimed below, before the first await past the
        # threshold decision, so this guard also holds against the rest
        # of the very burst that tripped it (#2014).
        if self._muted_until.get(key, now) is not None:
            return

        cfg = await self._config_for(event.chat.id, now)
        if not cfg.antiflood_enabled:
            return
        if cfg.flood_max_msgs <= 0 or cfg.flood_window_sec <= 0:
            return

        count = self._windows.hit(key, now, float(cfg.flood_window_sec))
        if count <= cfg.flood_max_msgs:
            return

        # Duck-typed on purpose (anything with ``restrict_chat_member`` /
        # ``get_chat_member``) so tests can inject a lightweight fake.
        bot: Bot | None = data.get("bot")
        if bot is None:
            log.warning("antiflood: no Bot in middleware data; skipping")
            return

        # #2014: claim the mute HERE, in the same await-free step as the
        # decision that earned it. aiogram gives every update its own
        # task and a flood is several messages in flight at once, so
        # while this marker was written after ``restrict_chat_member``
        # came back, every burst message arriving during that round trip
        # re-crossed the threshold: one mute, N restrict calls, N
        # notices in the group, and N ``moderation_log`` rows that the
        # ``/groupadmin`` statistics screen sums into the admin-visible
        # mute count. Same claim-then-release shape the rest of the tree
        # uses for single-flight work, and the reason ``TTLLRUCache``
        # carries ``discard``.
        #
        # ``_windows.reset`` moves up with it: the counter and the marker
        # are one decision, and leaving the window full would have the
        # release path below re-trip on the next message.
        self._mark_muted(key, now, max(1, cfg.flood_mute_minutes))
        self._windows.reset(key)

        if await self._is_exempt_admin(bot, event.chat.id, user.id):
            # Not a flooder after all: hand the claim back, or the admin
            # is silently suppressed for the whole mute duration.
            self._muted_until.discard(key)
            return

        await self._mute_and_notify(event, data, bot, cfg)

    async def _mute_and_notify(
        self,
        event: Message,
        data: dict[str, Any],
        bot: Bot,
        cfg: GroupModConfigView,
    ) -> None:
        """Restrict, log, notify. The mute window is already claimed.

        Nothing here writes ``_muted_until``: the caller claimed it
        before the first await (#2014), and computing the same deadline
        in two places is how the two drift apart. That also makes the
        restrict-failure path below a plain early return — the claim it
        used to make on its own way out is already in place, so a chat
        where the bot has no rights is still not re-tried per message.
        """
        assert event.from_user is not None
        minutes = max(1, cfg.flood_mute_minutes)
        try:
            await bot.restrict_chat_member(
                chat_id=event.chat.id,
                user_id=event.from_user.id,
                permissions=MUTED_PERMS,
                until_date=timedelta(minutes=minutes),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort restrict
            log.bind(chat_id=event.chat.id, user_id=event.from_user.id, exc=repr(exc)).info(
                "antiflood: restrict failed (no rights / left chat)"
            )
            return

        log.bind(
            chat_id=event.chat.id,
            user_id=event.from_user.id,
            minutes=minutes,
            max_msgs=cfg.flood_max_msgs,
            window_sec=cfg.flood_window_sec,
        ).info("antiflood: muted flooding user")
        await self._record_automute(bot, event.chat.id, event.from_user.id, cfg, minutes)

        # LanguageMiddleware (root outer, registered before us) has
        # already stamped the effective language for this update.
        lang = data.get("lang") or "ru"
        name = html.escape(event.from_user.full_name or str(event.from_user.id))
        mention = f'<a href="tg://user?id={event.from_user.id}">{name}</a>'
        try:
            await event.answer(t("h_af_muted_notice", lang, name=mention, minutes=minutes))
        except Exception as exc:  # noqa: BLE001 — best-effort notice
            log.bind(chat_id=event.chat.id, exc=repr(exc)).info(
                "antiflood: notice send failed",
            )

    async def _record_automute(
        self,
        bot: Bot,
        chat_id: int,
        user_id: int,
        cfg: GroupModConfigView,
        minutes: int,
    ) -> None:
        """Write the auto-mute to ``moderation_log``. Best-effort.

        Recorded as a plain ``"mute"`` — the same convention
        ``handlers.wordfilter`` uses for its automod ban (``action="ban"``
        with ``admin_id`` set to the bot's own id and the machine-readable
        marker in ``details``). A distinct ``"automute"`` action would
        read as more honest data and be worse in practice: the mute
        counter on ``/groupadmin → Статистика`` only sums
        ``_COUNTED_ACTIONS``, so every automatic mute would vanish from
        the number an admin actually reads.

        Until #253 nothing was written at all: an antiflood mute left a
        log line and no trace an admin could see anywhere.

        ``bot.id`` is read inside the guard on purpose: ``bot`` is
        duck-typed here (see ``_maybe_act``), and a fake without an
        ``id`` must cost the audit row, never the mute or the notice.
        """
        try:
            admin_id = bot.id
            async with session_for(self._registry, DBName.MODERATION) as session:
                await ModerationRepo(session).record_action(
                    action="mute",
                    user_id=user_id,
                    admin_id=admin_id,
                    chat_id=chat_id,
                    reason="Антифлуд",
                    details=(
                        f"antiflood msgs>{cfg.flood_max_msgs}"
                        f"/{cfg.flood_window_sec}s mute={minutes}m"
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — the audit must not break the mute
            log.bind(chat_id=chat_id, user_id=user_id, exc=repr(exc)).warning(
                "antiflood: audit write failed",
            )

    def _mark_muted(self, key: tuple[int, int], now: float, minutes: int) -> None:
        """Claim the mute window (explicit expiry, not the cache's TTL).

        Called before the restrict round trip, not after it — see #2014
        in :meth:`_maybe_act`. Released with ``_muted_until.discard`` on
        the one path that turns out not to want it.
        """
        self._muted_until.put_until(key, True, now + minutes * 60.0)
