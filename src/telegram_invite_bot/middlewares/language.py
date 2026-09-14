"""Single source of truth for a user's effective bot language.

Before this middleware, ~30 handlers each resolved the language locally
from ``event.from_user.language_code`` — the Telegram *client* locale.
That ignored the user's stored preference (``user_settings.language``,
set via ``/lang`` and shown in ``/profile``): a Russian user whose
Telegram app is in English saw ~10 commands rendered in English, and
because ``language_code`` is sometimes absent the language was even
non-deterministic between requests.

:class:`LanguageMiddleware` is a ROOT-level OUTER middleware (attached on
both ``root.message`` and ``root.callback_query`` in ``main_router``) that
resolves the effective language ONCE per update and stamps it into
``data["lang"]``. Resolution order (matching
:pyattr:`telegram_invite_bot.core.entities.user.User.language`):

1. stored ``user.language`` — the override-applied preference read via a
   short ``users.db`` session (``UsersRepo.get(uid).language``);
2. else derived from the Telegram ``language_code``
   (``startswith("en")`` → ``en``, else ``ru``);
3. else ``"ru"``.

A small per-user in-process TTL cache (default 300s) keeps this off the
DB on every group message — a busy group would otherwise pay one extra
``users.db`` round-trip per chatter. ``/lang`` invalidates a single
user's entry via :func:`invalidate_language_cache` so a freshly-saved
preference takes effect immediately instead of after the TTL.

The cache is ALSO capped (:data:`_MAX_CACHED_USERS`). The TTL alone
does not bound it: expiry is checked when an entry is read, so a user
who never sends a second message leaves their tuple behind forever.
This middleware is attached at the root on both ``message`` and
``callback_query``, so it sees every update from every member of every
group the bot sits in — of all the in-process tables this is the one
with the widest key space. The cap makes it a fixed ceiling; evicting
an entry costs one extra ``users.db`` read the next time that user
speaks, and nothing else.

The session is opened the same way :class:`BaseSessionMiddleware` does —
``EngineRegistry.session(DBName.USERS)`` — but this middleware is
deliberately NOT a ``BaseSessionMiddleware`` subclass: it owns a tiny
read-only lookup with its own cache + commit-free lifecycle, not the
bind-repos / commit-on-success contract that base encodes.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.utils.cache_generation import CacheGeneration
from telegram_invite_bot.utils.language import lang_from_code

if TYPE_CHECKING:
    from aiogram.types import TelegramObject
    from aiogram.types import User as TelegramUser

    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="middlewares.language")

DEFAULT_TTL_SECONDS = 300.0

# Ceiling on the number of cached users (see module docstring). Sized so
# that every member of every group the bot is plausibly in stays resident
# — the entry is ~100 bytes, so the whole table is well under a megabyte
# — while a one-off visitor cannot pin memory forever.
_MAX_CACHED_USERS = 10_000

# Module-level per-user cache: user_id -> (effective_lang, monotonic_expiry).
# Module-level (not instance-level) so ``invalidate_language_cache`` can be
# called from the ``/lang`` handler without threading the middleware
# instance through DI — there is exactly one middleware instance per
# process anyway. ``time.monotonic`` is used for expiry so a wall-clock
# adjustment can't wedge an entry as permanently fresh / stale. Ordered so
# eviction can drop the least-recently-used user in O(1).
_CACHE: OrderedDict[int, tuple[str, float]] = OrderedDict()

# #1942: ``_resolve`` fills this after an ``await``, and ``/lang``
# invalidates the moment it has persisted the new preference. A fill
# landing inside that window re-served the language the user had just
# changed away from, for the whole TTL. See ``utils.cache_generation``.
_GENERATION = CacheGeneration()


def invalidate_language_cache(user_id: int) -> None:
    """Drop the cached effective language for one user.

    Called by the ``/lang`` callback after it persists a new preference
    so the next update re-reads the stored value instead of serving a
    stale cache entry for up to the TTL. A no-op when the user was never
    cached.
    """
    _CACHE.pop(user_id, None)
    _GENERATION.bump()


def clear_language_cache() -> None:
    """Drop the entire per-user language cache.

    Module-level state, so the test suite must reset it between cases to
    keep them isolated (a fixed test ``user_id`` reused across cases with
    different expected languages would otherwise serve a stale entry).
    A no-op in production; the TTL + per-user invalidation cover prod.
    """
    _CACHE.clear()
    _GENERATION.bump()


async def language_for_user(
    user_id: int,
    *,
    users_repo: UsersRepo,
    settings_repo: UserSettingsRepo,
    fallback: str = "ru",
) -> str:
    """Effective bot language of an ARBITRARY user, on an open session.

    :class:`LanguageMiddleware` resolves the language of the user who
    SENT the update. Handlers that message a *third party* — the ticket
    reply DM, a notification to the other half of a couple — need the
    recipient's language instead, and the recipient never went through
    the middleware for this update.

    Same resolution order as :meth:`LanguageMiddleware._stored_or`: the
    explicit ``/lang`` choice in ``user_settings.language`` wins, then
    the ``users`` entity, then ``fallback``. Unlike the middleware there
    is no client-locale guess available (we have no ``TelegramUser`` for
    a third party), so ``fallback`` carries that role — pass the
    sender's language to keep the reply in the language the operator is
    already reading if the recipient has no stored preference.

    Deliberately NOT cached: this runs once per admin action, not on the
    hot path, and reusing ``_CACHE`` here would let a stale third-party
    entry outlive a ``/lang`` change the middleware never saw.
    """
    override = await settings_repo.get_language(user_id)
    if override in ("ru", "en"):
        return override
    user = await users_repo.get(user_id)
    return user.language if user is not None else fallback


async def best_effort_language_for_user(
    user_id: int,
    *,
    users_repo: UsersRepo,
    settings_repo: UserSettingsRepo,
    fallback: str = "ru",
) -> str:
    """:func:`language_for_user`, but it never raises.

    The strict variant is right for a resolution that happens BEFORE the
    work: if the read faults the handler has done nothing yet and the
    error path is honest. It is wrong for the courtesy DMs that follow a
    committed money move (``/give``, withdrawal approve/reject) — there
    the coins have already landed, and a ``database is locked`` hiccup
    on the (memory-tight, I/O-fragile) prod host would surface to the
    operator as the generic error reply for an action that in fact
    succeeded, inviting a retry that double-pays.

    Same split as :meth:`LanguageMiddleware._stored_or`: the resolution
    lives in :func:`language_for_user`, and the never-raise guarantee is
    added by the caller-facing wrapper, so neither behaviour is hidden
    inside the other.
    """
    try:
        return await language_for_user(
            user_id,
            users_repo=users_repo,
            settings_repo=settings_repo,
            fallback=fallback,
        )
    except Exception:  # noqa: BLE001 - a DM's wording must not undo a commit
        log.opt(exception=True).warning(
            "language lookup failed for {uid}; falling back to {fallback}",
            uid=user_id,
            fallback=fallback,
        )
        return fallback


class LanguageMiddleware(BaseMiddleware):
    """Stamp ``data["lang"]`` with the user's effective bot language.

    Attach as an OUTER middleware on both ``root.message`` and
    ``root.callback_query`` so every update — including plain group
    chatter no command handler claims — carries the resolved language for
    any downstream handler that injects ``lang: str``.
    """

    def __init__(
        self, registry: EngineRegistry, *, ttl_seconds: float = DEFAULT_TTL_SECONDS
    ) -> None:
        self._registry = registry
        self._ttl = ttl_seconds

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TelegramUser | None = data.get("event_from_user")
        data["lang"] = await self._resolve(tg_user)
        return await handler(event, data)

    async def _resolve(self, tg_user: TelegramUser | None) -> str:
        # No author (channel post / service message) — fall back to RU.
        if tg_user is None:
            return "ru"

        code_fallback = lang_from_code(tg_user.language_code)

        now = time.monotonic()
        cached = _CACHE.get(tg_user.id)
        if cached is not None and cached[1] > now:
            # A hit is "recently used" too — otherwise a chatty user whose
            # entry never expires would age out of the table and pay a DB
            # read on their next message, which is the cost this cache
            # exists to avoid.
            _CACHE.move_to_end(tg_user.id)
            return cached[0]

        generation = _GENERATION.snapshot()
        lang = await self._stored_or(tg_user.id, code_fallback)
        if not _GENERATION.unchanged(generation):
            # A ``/lang`` landed while the read was suspended: serve
            # what we read, but do not speak for the next TTL on it.
            return lang
        _CACHE[tg_user.id] = (lang, now + self._ttl)
        _CACHE.move_to_end(tg_user.id)
        while len(_CACHE) > _MAX_CACHED_USERS:
            _CACHE.popitem(last=False)
        return lang

    async def _stored_or(self, user_id: int, code_fallback: str) -> str:
        """Read the stored language preference for ``user_id``.

        The explicit ``/lang`` choice lives in ``user_settings.language``
        — a bare ``UsersRepo.get()`` does NOT apply it (only
        ``UserService.touch`` composes the override), and the ``users``
        table itself has no ``language`` column, so reading the entity
        alone silently degrades to the ``language_code``-derived guess.
        Query the override first; only fall back to the entity (and then
        to ``code_fallback``) when the user never picked a language.
        A lookup failure (DB hiccup) degrades to ``code_fallback`` rather
        than raising — language resolution must never break an update.

        The resolution itself lives in :func:`language_for_user` so the
        third-party callers (the ticket-reply DM and friends) cannot
        drift away from what the middleware does; this method only adds
        the session and the never-raise guarantee.
        """
        try:
            sessionmaker = self._registry.session(DBName.USERS)
            async with sessionmaker() as session:
                return await language_for_user(
                    user_id,
                    users_repo=UsersRepo(session),
                    settings_repo=UserSettingsRepo(session),
                    fallback=code_fallback,
                )
        except Exception:  # noqa: BLE001 — never let a read fault break the update
            log.opt(exception=True).warning(
                "language lookup failed for {uid}; falling back to client locale",
                uid=user_id,
            )
            return code_fallback
