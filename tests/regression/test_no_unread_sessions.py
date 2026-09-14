"""#2025 — a router must not open a database session nobody reads.

Every :class:`~telegram_invite_bot.middlewares.base.BaseSessionMiddleware`
mounted on a router checks a connection out of that database's pool on
**every** update the router sees, binds its repos into ``data``, and
commits or rolls back on the way out. That is the correct amount of work
when a handler asks for one of those repos. When none does, it is a
per-command round-trip bought and thrown away.

Waste is the small half. The real reason this is a guard:
``handlers/wordfilter.py`` mounted :class:`ModerationMiddleware`
alongside its own ``_WordFilterRepoMiddleware``, so every
``/filter_add`` held **two independent sessions on the same
``moderation.db`` file**, committed separately by ``base.py``'s
unordered exit. Nothing was wrong while one of them stayed untouched —
``Checkpoint`` skips a session with no open transaction — but the day a
word-filter handler writes through ``moderation_repo``, that router has
two ``BEGIN IMMEDIATE`` writers on one file in one update, and the
failure will read as a mysterious lock timeout rather than as the
mounting mistake it is.

The router's own docstring had already drifted to describe the
arrangement it does not have ("we attach ``word_filter_repo`` on the
same ``moderation.db`` session" — it does not; ``_WordFilterRepoMiddleware``
opens its own, and says so). Prose cannot be the thing that notices.

What counts as "read": a handler on the router **or any of its
sub-routers** naming the bound key as a parameter, since aiogram
propagates inner middlewares down the tree. A handler with ``**kwargs``
could consume anything, so its router is exempt — there are none today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from telegram_invite_bot.middlewares.base import BaseSessionMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aiogram import Dispatcher, Router
    from sqlalchemy.ext.asyncio import AsyncSession

#: The two event types the session middlewares are ever mounted on.
_EVENTS = ("message", "callback_query")


def _tree(router: Router) -> Iterator[Router]:
    yield router
    for child in router.sub_routers:
        yield from _tree(child)


def _bound_keys(middleware: BaseSessionMiddleware) -> set[str]:
    """What this middleware stamps into ``data``, asked of the real thing.

    ``_bind`` is handed ``None`` for the session: every implementation
    in the tree only wraps it in a repo constructor, and a repo built
    over ``None`` is never used here — the keys are the whole question.
    Asking the middleware beats listing the keys, which is the mistake
    this test exists to catch one level up.
    """
    data: dict[str, Any] = {}
    middleware._bind(cast("AsyncSession", None), data)  # noqa: SLF001
    return set(data)


def test_no_router_opens_a_session_no_handler_asks_for(
    production_dispatcher: Dispatcher,
) -> None:
    offenders: list[str] = []
    checked = 0
    for router in _tree(cast("Router", production_dispatcher)):
        for event in _EVENTS:
            consumed: set[str] = set()
            open_ended = False
            for descendant in _tree(router):
                for handler in getattr(descendant, event).handlers:
                    consumed |= set(handler.params)
                    open_ended = open_ended or bool(handler.varkw)
            observer = getattr(router, event)
            for kind in ("middleware", "outer_middleware"):
                for middleware in getattr(observer, kind):
                    if not isinstance(middleware, BaseSessionMiddleware):
                        continue
                    keys = _bound_keys(middleware)
                    if not keys:
                        continue
                    checked += 1
                    if open_ended or keys & consumed:
                        continue
                    offenders.append(
                        f"{router.name}.{event}: {type(middleware).__name__} opens "
                        f"{middleware._db_name.value} and binds "  # noqa: SLF001
                        f"{sorted(keys)}, which no handler on this router or below "
                        f"asks for"
                    )

    assert not offenders, (
        "session opened per update and never read — drop the middleware, or "
        "bind the repo the handlers actually take:\n  " + "\n  ".join(offenders)
    )
    assert checked >= 70, f"only {checked} session mounts inspected — the walk lost the tree"
