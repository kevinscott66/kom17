"""``/admin_loguru`` — configured loguru sink snapshot.

Complements /admin_test_log (which proves the log chain *works* end-
to-end by emitting a synthetic ERROR) by listing **what's actually
configured**: every sink registered with loguru, its minimum level,
and a short identity. The two cards together let an operator answer
both halves of a post-deploy log audit:

* /admin_test_log: does the chain transport an event from emit to sink?
* /admin_loguru: which sinks are subscribed, and at what level?

Why an operator wants this:

* Verify a sink was added by the deploy. A new file sink in
  ``config/logging.py`` should appear here after restart; if it
  doesn't, the import path or the conditional that gates it
  didn't land. Without this card the only way to tell is grepping
  the disk for the expected file.
* Verify a level filter. If an operator bumped a sink from INFO
  to DEBUG to capture a transient, this card surfaces the change
  reliably — without it, they'd have to read the source and
  guess what the running process actually has.
* Spot duplicate sinks. ``configure_logging`` is idempotent in
  this codebase but a future refactor could regress; two stderr
  sinks at INFO would silently double every record's render
  cost. The count + identity render makes the duplicate visible.

Reads :data:`loguru._core.Core.handlers` — a dict of
``{handler_id: Handler}``. We touch the leading-underscore
attribute deliberately: loguru exposes no public introspection
API, but the internal structure has been stable across releases
since 0.5 (we pin a recent version). The defensive ``getattr``
pattern in :func:`_describe_handler` is the regression-safe
hedge: if a future loguru bump renames ``_levelno`` or ``_sink``,
the card degrades to "unknown" rather than crashes the handler.

Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.loguru")


# Standard loguru levels — we ship the mapping rather than touching
# ``logger._core.levels`` because that one is keyed by name, not
# levelno, and reverse-lookup would be one more private-API touch
# for marginal benefit. Any custom level a future deploy adds
# falls through to the numeric render, which is correct surface
# behaviour: the operator sees the levelno and can grep config.
_STANDARD_LEVEL_NAMES: dict[int, str] = {
    5: "TRACE",
    10: "DEBUG",
    20: "INFO",
    25: "SUCCESS",
    30: "WARNING",
    40: "ERROR",
    50: "CRITICAL",
}


class _SinkRow:
    """One loguru handler's identifying surface.

    ``handler_id`` is loguru's internal sequence — useful for
    correlating with ``logger.remove(id)`` calls. ``level_name``
    is the resolved string (INFO, DEBUG, …) if the levelno
    matches a standard level; numeric fallback otherwise.
    ``sink_kind`` is the sink object's class name (StreamSink,
    FileSink, AsyncSink) — sufficient identity for the operator
    without leaking sink internals (a FileSink path could be a
    real privacy issue if the card is rendered in the wrong
    chat, which the router-level filter already prevents but we
    don't double-up the risk here).
    """

    __slots__ = ("handler_id", "level_name", "levelno", "sink_kind")

    def __init__(
        self,
        *,
        handler_id: int,
        levelno: int,
        level_name: str,
        sink_kind: str,
    ) -> None:
        self.handler_id = handler_id
        self.levelno = levelno
        self.level_name = level_name
        self.sink_kind = sink_kind


def _describe_handler(handler: Any) -> _SinkRow:  # noqa: ANN401
    """Best-effort identity read.

    The defensive ``getattr`` is the regression-safe hedge against
    a future loguru rename of ``_levelno`` or ``_sink``. The
    handler-id we read off ``_id`` is the same int loguru's
    ``logger.add`` returns, so the operator can map a row back
    to its call site.
    """
    handler_id = int(getattr(handler, "_id", -1))
    levelno = int(getattr(handler, "_levelno", 0))
    sink = getattr(handler, "_sink", None)
    sink_kind = type(sink).__name__ if sink is not None else "unknown"
    level_name = _STANDARD_LEVEL_NAMES.get(levelno, f"level_{levelno}")
    return _SinkRow(
        handler_id=handler_id,
        levelno=levelno,
        level_name=level_name,
        sink_kind=sink_kind,
    )


def _capture() -> list[_SinkRow]:
    """Snapshot all currently-registered loguru handlers.

    Sorted by handler_id ascending — loguru hands them out in
    registration order, so this gives the operator the same view
    they'd get from reading the configure sequence top-down.
    """
    # ``logger._core`` is loguru's only introspection seam — no public
    # API surfaces the configured handler set. The ``type: ignore`` is
    # the deliberate price of touching the private attribute; the
    # defensive ``getattr`` in :func:`_describe_handler` is the
    # regression hedge that keeps this card from crashing if loguru
    # ever renames or restructures the internal layout.
    core = logger._core  # type: ignore[attr-defined]  # noqa: SLF001
    rows = [_describe_handler(h) for h in core.handlers.values()]
    rows.sort(key=lambda r: r.handler_id)
    return rows


def _render(rows: list[_SinkRow]) -> str:
    lines = ["📜 <b>Loguru sinks</b>", ""]
    lines.append(f"<i>Configured sinks: <b>{len(rows)}</b></i>")
    lines.append("")
    if not rows:
        # Zero sinks is a real failure mode — every log call would
        # vanish into the void. Surface it explicitly rather than
        # rendering an empty bullet list that an operator could
        # skim past.
        lines.append("<i>No sinks configured — every log call is dropped.</i>")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            f"• id <code>{row.handler_id}</code>: "
            f"<code>{row.sink_kind}</code> @ "
            f"<code>{row.level_name}</code>"
        )
    lines.append("")
    lines.append(
        "<i>Use with /admin_test_log: this card lists what's "
        "subscribed; /admin_test_log proves the chain transports. "
        "Duplicate sinks at the same level silently double render "
        "cost — count them here.</i>"
    )
    return "\n".join(lines)


async def handle_admin_loguru(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_loguru; silently dropped"
        )
        return
    rows = _capture()
    await message.answer(_render(rows))
    log.bind(user_id=user.id, sink_count=len(rows)).info("/admin_loguru rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.loguru")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_loguru(message, settings)

    router.message.register(_entry, Command("admin_loguru", ignore_case=True))
    return router
