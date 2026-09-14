"""``/admin_middlewares`` — dispatcher middleware-chain snapshot.

Complements /admin_routes (handler-registration tree) by surfacing
the **other** half of the dispatch path: the outer + inner
middleware managers attached to each observer (update, message,
callback_query, ...). The diagnostic gap this closes is the
"throttling deployed but not firing" failure mode — without this
card the only way to verify the middleware actually got wired is
reading the import order in ``di/providers.py`` and trusting it
took. An operator can now confirm visually that
``ThrottlingMiddleware`` and ``SessionMiddleware`` are present
in the expected outer chains.

Why an operator wants this:

* Verify a wiring change after deploy. A typo in
  ``dispatcher.message.outer_middleware(...)`` vs
  ``dispatcher.callback_query.outer_middleware(...)`` is a real
  drift this surfaces immediately: the operator scans the
  per-observer column and notices the asymmetry.
* Debug rate-limit not firing. The legacy posture is "buckets
  fill" — but a regression where the throttle middleware was
  unregistered (e.g. a refactor moved it inside a conditional)
  surfaces here as the missing row, before any user notices.
* Audit ordering. Outer middlewares run in registration order;
  if SessionMiddleware ends up before ThrottlingMiddleware, a
  rate-limited update still opens a DB session. The list
  preserves order so the operator sees the chain top-down.

Reads aiogram's :class:`MiddlewareManager._middlewares` list —
no public introspection API exists. The private-attribute touch
is the documented price; the defensive ``getattr`` on each access
is the regression hedge for a future aiogram rename.

Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only at the router level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Dispatcher
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.middlewares")


# The observers we surface. ``update`` is the root (every update
# passes through), ``message`` and ``callback_query`` are the two
# observers we actually attach app-level middlewares to. Other
# observers (chat_member, inline_query, …) exist but in this codebase
# they're unmodified from aiogram defaults — rendering them would
# add noise without a corresponding failure mode worth a column.
#
# A future deploy that attaches a middleware to e.g. inline_query
# should extend this list and pin the diagnostic alongside the
# wiring change.
_OBSERVERS: tuple[str, ...] = ("update", "message", "callback_query")


class _ObserverRow:
    """One observer's outer + inner middleware chain.

    ``outer`` runs before route resolution; ``inner`` runs after.
    Most app-level wiring goes outer (so rejected updates short-
    circuit before any per-handler logic), so the outer list is
    the surface an operator scans first. We render both because a
    middleware accidentally registered on ``inner`` won't see
    short-circuit rejections.
    """

    __slots__ = ("inner", "name", "outer")

    def __init__(
        self,
        *,
        name: str,
        outer: list[str],
        inner: list[str],
    ) -> None:
        self.name = name
        self.outer = outer
        self.inner = inner


def _read_chain(manager: Any) -> list[str]:  # noqa: ANN401
    """Best-effort read of a MiddlewareManager's chain.

    aiogram stores the chain on ``_middlewares`` (a list); if a
    future version renames or restructures this we degrade to an
    empty list rather than crash the diagnostic. The card cares
    about class names — not behaviour — so an empty list on a
    rename is a clear "we lost visibility" signal rather than a
    silent wrong answer.
    """
    if manager is None:
        return []
    chain = getattr(manager, "_middlewares", None)
    if chain is None:
        return []
    return [type(m).__name__ for m in chain]


def _capture(dispatcher: Dispatcher) -> list[_ObserverRow]:
    """Snapshot the per-observer chain for the listed observers.

    Each observer exposes ``.outer_middleware`` and ``.middleware``
    as its outer / inner managers. The attribute name asymmetry
    (``.middleware`` for inner, not ``.inner_middleware``) is
    aiogram's public surface — we follow the upstream convention
    so the card's labels stay aligned with the docs.
    """
    rows: list[_ObserverRow] = []
    for name in _OBSERVERS:
        observer = getattr(dispatcher, name, None)
        outer_mgr = getattr(observer, "outer_middleware", None)
        inner_mgr = getattr(observer, "middleware", None)
        rows.append(
            _ObserverRow(
                name=name,
                outer=_read_chain(outer_mgr),
                inner=_read_chain(inner_mgr),
            )
        )
    return rows


def _render(rows: list[_ObserverRow]) -> str:
    lines = ["🔗 <b>Dispatcher middlewares</b>", ""]
    for row in rows:
        lines.append(f"<b>{row.name}</b>")
        # Outer first — that's where this codebase puts the
        # rate-limit + DI middlewares the operator most cares to
        # verify, and matches the "outer runs first" execution
        # order so the rendered list reads like the runtime
        # control flow.
        if row.outer:
            lines.append("  <i>outer:</i>")
            for cls in row.outer:
                lines.append(f"    • <code>{cls}</code>")
        else:
            lines.append("  <i>outer: none</i>")
        if row.inner:
            lines.append("  <i>inner:</i>")
            for cls in row.inner:
                lines.append(f"    • <code>{cls}</code>")
        else:
            lines.append("  <i>inner: none</i>")
        lines.append("")
    lines.append(
        "<i>Outer runs before route resolution (rejections short-"
        "circuit); inner runs after. Verify ThrottlingMiddleware + "
        "SessionMiddleware are present on the observers your app "
        "wired them onto.</i>"
    )
    return "\n".join(lines)


async def handle_admin_middlewares(
    message: Message, settings: Settings, dispatcher: Dispatcher
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_middlewares; silently dropped"
        )
        return
    rows = _capture(dispatcher)
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_middlewares rendered")


def build_router(get_dispatcher: Callable[[], Dispatcher], settings: Settings) -> Router:
    """Receive a lazy getter for the Dispatcher rather than the
    instance itself — same pattern as /admin_routes' ``get_root``.
    The dispatcher is built around this router (the include_router
    happens after :func:`build_main_router` returns), so capturing
    the instance at router-build time would freeze it pre-include
    and miss any later middleware additions. A getter closure
    resolved at message-handle time always sees the fully-wired
    dispatcher.
    """
    router = Router(name="admin.middlewares")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_middlewares(message, settings, get_dispatcher())

    router.message.register(_entry, Command("admin_middlewares", ignore_case=True))
    return router
