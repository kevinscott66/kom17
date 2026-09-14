"""``/admin_routes`` — introspect what the dispatcher actually serves.

Operator question after a deploy: "every command I expected to be
live — is it?". The legacy code wired commands via decorators
scattered across a 45k-line file; the new pipeline wires them via
``router.message.register(...)`` calls aggregated in
:mod:`telegram_invite_bot.routers.main_router`. The risk in the
new shape is the inverse of the legacy risk: a handler module
that builds a router but is never ``include_router``'d. The build
succeeds, ruff and mypy pass, the test suite passes (because each
handler test wires its own router for the unit under test), and
the command is silently absent in prod.

This card walks the live dispatcher tree at request time and
lists, per router, the commands its message-handlers are bound
to. Compared against an operator's mental model ("these 30
commands should be live"), drift is immediate.

Implementation:

* The handler is given a ``get_root`` closure that returns the
  fully-built root router. Closure (not direct reference) because
  the root must be passed to :func:`build_router` *before* every
  sub-router is included (see :mod:`routers.main_router` — the
  routes router is itself included into the root, and Python
  resolves closure variables at call time).
* Walk via :attr:`Router.sub_routers` recursively; each
  :class:`HandlerObject` exposes its bound :class:`Command` filters
  via ``handler.flags['commands']`` — the same attribute aiogram's
  own ``set_my_commands`` discovery uses.
* Errors-router and routers with no commands (e.g. ``main`` root)
  render as a header-only row so the operator sees the structure
  rather than just a deduplicated command list.

The card is paginated. That estimate above used to read "well
under 2 KiB" and was wrong by a factor of six: the live tree
renders ~11 800 characters, so every ``/admin_routes`` invocation
died on Telegram's 4096-char ceiling and the operator got nothing
back at all. The map grows by one line per router by design, so
no comment asking future authors to keep it short could have held
— it needed the pagination layer that comment asked for.

Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only at the router level.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.utils.render import paginate_lines

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.routes")

# The route map gets its own page ceiling instead of the shared
# ``PAGE_MAX`` of five. That default exists to stop an admin list from
# turning into an unbounded burst of messages, and it is right for
# lists whose length is a user's own doing (500 filter words). This
# card is different in both halves of that trade: its length is a fact
# about the codebase, not about anyone's data, and a truncated answer
# defeats the one question it exists to answer — "is everything I
# expect actually wired?". At 245 routers it needs six pages, so five
# silently cut the tail off the moment #159 added one more router. The
# ceiling is kept (a runaway tree should still not flood the chat) and
# the "map truncated" line still fires when it is reached — this just
# buys room for the tree to roughly double first.
_MAX_PAGES = 12

#: Longest command list a single rendered line may carry, in
#: characters. Paging alone is not enough here: ``paginate_lines``
#: budgets whole lines and deliberately puts an over-budget line on a
#: page of its own rather than splitting it — safe for the lists it
#: was written for, where one line is one word or one alias. This card
#: breaks that assumption: the fallback router binds EVERY command in
#: the bot, so its single row grew past Telegram's 4096-char ceiling
#: and became a page nothing could deliver — the exact failure the
#: pager exists to prevent, one level down. Wrapping the row here
#: keeps every line comfortably inside ``PAGE_BUDGET`` so the pager
#: always has something it can place.
_MAX_LINE_COMMANDS: Final[int] = 1000


def _commands_for_router(router: Router) -> list[str]:
    """Collect the command literals bound to this router's
    message-handlers. ``flags['commands']`` is aiogram's own
    discovery surface (see :meth:`Command.update_handler_flags`);
    using anything else would couple us to private internals.

    Returns a sorted, de-duplicated list — a single handler can be
    registered with multiple aliases (e.g. ``Command('foo', 'bar')``
    yields both), and rendering them all on one row would clutter
    the card without adding signal.
    """
    seen: set[str] = set()
    for handler in router.message.handlers:
        for cmd_filter in handler.flags.get("commands", []):
            if isinstance(cmd_filter, Command):
                # ``Command.commands`` is ``tuple[str | Pattern, ...]``;
                # aiogram accepts both literals and compiled regexes
                # for advanced use. Regexes don't fit the operator's
                # "is /foo wired?" question, so they're filtered out
                # — surfacing ``re.compile(r'^x.*$')`` in the card
                # would be more noise than signal.
                seen.update(c for c in cmd_filter.commands if isinstance(c, str))
    return sorted(seen)


def _walk(router: Router) -> list[tuple[str, list[str]]]:
    """Depth-first walk of the router tree.

    Returns ``(router_name, commands)`` pairs in include-order so
    the operator's mental map of "routers I added top-to-bottom"
    matches the rendered output.
    """
    out: list[tuple[str, list[str]]] = [(router.name, _commands_for_router(router))]
    for child in router.sub_routers:
        out.extend(_walk(child))
    return out


def _command_lines(name: str, cmds: list[str]) -> list[str]:
    """Render one router's row, wrapping over-long command lists.

    Continuation lines carry no ``<b>name</b>`` prefix — they are the
    same row, and repeating the router name would read as a second
    router with the same name. A leading ellipsis marks them instead.
    """
    chunks: list[list[str]] = [[]]
    used = 0
    for cmd in cmds:
        token = f"/{cmd}"
        # +2 for the ", " that joins this token to the previous one.
        cost = len(token) + 2
        if used + cost > _MAX_LINE_COMMANDS and chunks[-1]:
            chunks.append([])
            used = 0
        chunks[-1].append(token)
        used += cost
    lines = [f"<b>{name}</b>: <code>{', '.join(chunks[0])}</code>"]
    lines.extend(f"<code>… {', '.join(chunk)}</code>" for chunk in chunks[1:])
    return lines


def _render_pages(rows: list[tuple[str, list[str]]]) -> list[str]:
    """Render the route map as messages Telegram will actually accept.

    Splitting rather than truncating: the whole point of this card is
    "show me what is actually wired", and a silently-dropped tail is
    worse than the 400 it replaces, because a short answer looks like
    a complete one. ``more_line`` is only reachable if the tree
    outgrows every page — it says so out loud rather than lying.
    """
    total_routers = len(rows)
    total_commands = sum(len(cmds) for _, cmds in rows)
    header = "\n".join(
        [
            "🧭 <b>Dispatcher route map</b>",
            "",
            f"<i>{total_routers} routers · {total_commands} bound commands</i>",
            "",
        ]
    )
    lines: list[str] = []
    for name, cmds in rows:
        if cmds:
            lines.extend(_command_lines(name, cmds))
        else:
            # Empty-commands rows are still informative — they
            # surface routers wired for non-message events
            # (errors, callbacks) or umbrella routers like ``main``.
            lines.append(f"<b>{name}</b>: <i>(no message commands)</i>")
    return paginate_lines(
        header,
        lines,
        # "lines", not "routers": a router whose command list wraps
        # contributes more than one line, so a router count here
        # would understate what was dropped.
        more_line=lambda left: f"<i>… and {left} more lines (map truncated)</i>",
        max_pages=_MAX_PAGES,
    )


async def handle_admin_routes(message: Message, settings: Settings, root: Router) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_routes; silently dropped"
        )
        return
    rows = _walk(root)
    for page in _render_pages(rows):
        await message.answer(page)
    log.bind(user_id=user.id, routers=len(rows)).info("/admin_routes rendered")


def build_router(get_root: Callable[[], Router], settings: Settings) -> Router:
    """Build the ``admin.routes`` router.

    ``get_root`` is a zero-arg closure (not the root directly)
    because the routes router must be included *into* the root
    that we want it to introspect — a chicken-and-egg if we took
    the router itself. The closure resolves at message-time, by
    which point the root is fully assembled.
    """
    router = Router(name="admin.routes")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_routes(message, settings, get_root())

    router.message.register(_entry, Command("admin_routes", ignore_case=True))
    return router
