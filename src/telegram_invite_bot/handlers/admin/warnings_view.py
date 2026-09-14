"""``/admin_warnings`` — installed warning-filter snapshot.

The Python warnings system is a stealth channel: third-party libraries
emit ``DeprecationWarning`` years before the breaking change lands,
and the only reason an operator hears the alarm is because the
default filter routes those warnings to stderr. A
``warnings.simplefilter("ignore")`` somewhere in the import chain
silences every alarm; a refactor that left it behind is the kind of
regression no test catches because the warnings system isn't on the
hot path.

Why an operator wants this:

* Pre-upgrade audit. Before bumping a major dep (aiogram, SQLAlchemy,
  pydantic), an operator wants to confirm the bot is hearing
  DeprecationWarning's from the current version — otherwise the
  upgrade lands and the "deprecated since X" notes are surprises.
  The card surfaces the filter list so an operator can verify the
  default ``("default", None, DeprecationWarning, "__main__", 0)``
  hasn't been overridden with ``("ignore", ...)``.
* Catch a library that called ``simplefilter("ignore")`` in its
  module-init. This is rarer than it used to be, but legacy
  scientific-Python packages still do it on import. The card pins
  the active filter list so a ``simplefilter`` call from any
  module is visible.
* Confirm an explicit silence is still narrow. Sometimes we
  intentionally silence a specific warning ("DeprecationWarning
  from pkg X module Y") because the migration is tracked
  elsewhere. The card lets an operator audit that the silence
  is still scoped and hasn't broadened over time.

Reads :data:`warnings.filters` once. The list is ordered — first
match wins on a raised warning, so render order matters. We do NOT
sort. Same posture as every other ``/admin_*``: silent-drop for
non-devs, private-only at the router level.
"""

from __future__ import annotations

import html
import warnings
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.warnings")


# Rendered-row cap. A typical Python process has 5-12 filter
# entries (CPython defaults + a handful from third-party imports).
# Past 20 the operator should be looking at the raw list via REPL;
# the truncation tail keeps the card under Telegram's 4096-char
# limit on the pathological case.
_MAX_FILTERS = 20


class _FilterRow:
    """One ``warnings.filters`` tuple, rendered safely.

    The raw filter is a 5-tuple ``(action, message, category, module,
    lineno)`` where ``message`` and ``module`` are :class:`re.Pattern`
    or ``None`` and ``category`` is a class. Stringifying piece by
    piece (rather than printing the tuple) gives the operator a
    readable row and avoids leaking pattern internals like
    ``re.compile(...)`` repr noise.
    """

    __slots__ = ("action", "category", "lineno", "message", "module")

    def __init__(
        self,
        *,
        action: str,
        message: str,
        category: str,
        module: str,
        lineno: int,
    ) -> None:
        self.action = action
        self.message = message
        self.category = category
        self.module = module
        self.lineno = lineno


def _describe_pattern(p: object) -> str:
    """Render a ``re.Pattern`` or ``None`` as a readable string.

    The default CPython entries use ``None`` (match-anything); a
    user-supplied filter via :func:`warnings.filterwarnings` carries
    a compiled regex. Surface the regex source rather than the
    pattern object's repr — operators read regexes more easily than
    ``re.compile('...', re.IGNORECASE)`` noise.
    """
    if p is None:
        return "<any>"
    pattern = getattr(p, "pattern", None)
    if pattern is None:
        return str(p)
    return str(pattern) if pattern else "<any>"


def _describe_category(c: object) -> str:
    """Render a warning class as ``module.ClassName``.

    Builtin warnings live in the ``__builtins__`` module which is
    confusing to render — collapse to bare class name for those,
    qualify with module for the rest. The qualification matters when
    a third-party library subclasses ``DeprecationWarning`` and we
    want to know which library's warnings the filter rule actually
    targets.
    """
    name = getattr(c, "__name__", None) or "<unknown>"
    module = getattr(c, "__module__", None)
    if not module or module == "builtins":
        return name
    return f"{module}.{name}"


def _capture() -> list[_FilterRow]:
    """Snapshot the active warning-filter list.

    Reading :data:`warnings.filters` directly is fine — the list is
    mutated by :func:`warnings.filterwarnings` and
    :func:`warnings.simplefilter`, neither of which we call here.
    No copy needed; the renderer iterates synchronously before any
    other code can rearrange the list.
    """
    rows: list[_FilterRow] = []
    for entry in warnings.filters:
        # Defensive shape check: CPython has always used a 5-tuple
        # but a future change could extend it. Slice + pad rather
        # than raise so the diagnostic stays usable past a future
        # version bump (an empty cell beats no card at all).
        action = entry[0] if len(entry) > 0 else "<unknown>"
        message = _describe_pattern(entry[1] if len(entry) > 1 else None)
        category = _describe_category(entry[2] if len(entry) > 2 else Warning)
        module = _describe_pattern(entry[3] if len(entry) > 3 else None)
        lineno = int(entry[4]) if len(entry) > 4 else 0
        rows.append(
            _FilterRow(
                action=str(action),
                message=message,
                category=category,
                module=module,
                lineno=lineno,
            )
        )
    return rows


def _render(rows: list[_FilterRow]) -> str:
    lines = ["⚠️ <b>Warning filters</b>", ""]
    lines.append(f"<b>entries:</b> <code>{len(rows)}</code>")
    lines.append("")
    if not rows:
        # An empty filter list means EVERY warning falls through to
        # the default action ("default" for most categories) — which
        # is fine, just unusual. Surface explicitly rather than
        # render a blank card.
        lines.append("<i>(no filters installed — every warning uses default action)</i>")
        return "\n".join(lines)
    for row in rows[:_MAX_FILTERS]:
        # ``ignore`` is the action that silences a warning — the
        # operationally-loaded value. ``default``, ``always``,
        # ``module``, ``once`` all surface the warning somehow;
        # ``error`` upgrades it to an exception (also worth a
        # visual marker for the opposite reason — operators chasing
        # "the bot crashed on an unfamiliar exception" want to
        # spot a filter promoting a warning to a fatal). Two
        # markers, different glyphs so triage doesn't conflate
        # them.
        if row.action == "ignore":
            marker = " 🔇 (silenced)"
        elif row.action == "error":
            marker = " 💥 (raises)"
        else:
            marker = ""
        lines.append(
            f"  • <code>{html.escape(row.action)}</code> "
            f"<i>cat=</i><code>{html.escape(row.category)}</code> "
            f"<i>msg=</i><code>{html.escape(row.message)}</code> "
            f"<i>mod=</i><code>{html.escape(row.module)}</code> "
            f"<i>line=</i><code>{row.lineno}</code>"
            f"{marker}"
        )
    if len(rows) > _MAX_FILTERS:
        remaining = len(rows) - _MAX_FILTERS
        lines.append(f"  <i>… and {remaining} more</i>")
    lines.append("")
    lines.append(
        "<i>First match wins. <code>ignore</code> silences; "
        "<code>error</code> promotes to exception. CPython "
        "defaults include <code>default</code> for "
        "<code>DeprecationWarning</code> in __main__ — verify "
        "before a major-dep upgrade.</i>"
    )
    return "\n".join(lines)


async def handle_admin_warnings(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_warnings; silently dropped"
        )
        return
    rows = _capture()
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_warnings rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.warnings")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_warnings(message, settings)

    router.message.register(_entry, Command("admin_warnings", ignore_case=True))
    return router
