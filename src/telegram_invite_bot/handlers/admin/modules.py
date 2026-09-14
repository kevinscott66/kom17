"""``/admin_modules`` — installed-version readout for load-bearing deps.

Operator question after a deploy: "is this the version I think it
is, and are the libraries underneath what I expect?". Python's
``importlib.metadata.version()`` answers it without needing shell
access. ``/admin_status`` already shows the package's own
``__version__``; this card extends that to the dep tree.

Surfaced versions are the ones whose behaviour changes are most
likely to cause silent regressions:

* aiogram — dispatcher / Router internals; minor versions have
  broken middleware ordering before.
* sqlalchemy — async engine + 2.0-style API; minors have moved
  ``func`` / ``select`` semantics in past.
* aiosqlite — pool + pragma plumbing.
* alembic — migration runner; version mismatch with online migrations
  is a recurring footgun on rollback.
* fastapi + uvicorn — webhook server; bug-for-bug behaviour of
  TLS termination changes between minors.
* pydantic + pydantic-settings — config validation; the 1→2 jump
  changed defaulting and we want loud surfacing of any future
  major drift.
* dishka — DI container scope behaviour.
* loguru — log-sink configuration semantics.
* prometheus-client — metric registry idempotency rules.

The list is curated, not introspected from ``site-packages``,
because the operator wants to scan it like a checklist: any name
they don't recognise is noise, any missing name is the regression.
Introspection would also surface transitive deps the operator
doesn't care about and would push the card past Telegram's
4096-char limit.

A package not found in ``importlib.metadata`` (vendored, removed,
or just typo'd here) renders as <code>—</code> rather than
crashing — the row is the diagnostic. Single-glyph ⚠ on missing,
✅ on found, so an operator can scan for drift without reading
every version string.

Same posture as every other ``/admin_*``: silent-drop for
non-devs (no enumeration via existence), private-only at the
router level (deployment-shape info that's operator-only context).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot import __version__

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.modules")


# Hand-curated, ordered roughly by "operator impact if mismatched".
# Order is editorial — top items are the ones that most-often produce
# silent-regression symptoms when their minor versions drift.
_PACKAGES: tuple[str, ...] = (
    "aiogram",
    "sqlalchemy",
    "aiosqlite",
    "alembic",
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
    "dishka",
    "loguru",
    "prometheus-client",
)


def _lookup(pkg: str) -> str | None:
    """Return the installed version or ``None`` if the package isn't
    discoverable. ``PackageNotFoundError`` is the only failure mode
    we care about here — a package vendored at a path metadata can't
    see, or a typo'd name in :data:`_PACKAGES` — and either is exactly
    the regression the card exists to surface.
    """
    try:
        return version(pkg)
    except PackageNotFoundError:
        return None


def _render(rows: list[tuple[str, str | None]]) -> str:
    lines = ["📦 <b>Installed modules</b>", ""]
    lines.append(f"<b>telegram_invite_bot</b>: <code>{__version__}</code>")
    lines.append("")
    for pkg, ver in rows:
        if ver is None:
            lines.append(f"  • {pkg}: <code>—</code> ⚠")
        else:
            lines.append(f"  • {pkg}: <code>{ver}</code> ✅")
    return "\n".join(lines)


async def handle_admin_modules(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_modules; silently dropped"
        )
        return
    rows = [(pkg, _lookup(pkg)) for pkg in _PACKAGES]
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_modules rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.modules")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_modules(message, settings)

    router.message.register(_entry, Command("admin_modules", ignore_case=True))
    return router
