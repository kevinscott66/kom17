"""``/admin_status`` — developer-only pipeline health snapshot.

The legacy ``/admin`` opens a password-gated control panel with dozens
of subcommands. We're not porting that whole tree yet — instead, this
first admin handler covers the **one** thing operators reach for first
during an incident: *"is the new pipeline alive, and which subsystems
think they're configured?"*.

Renders, gated on ``settings.bot.is_developer(user.id)``:

* package version (``telegram_invite_bot.__version__``) and, below
  it, the revision the deploy actually shipped — see #170 and
  :mod:`telegram_invite_bot.utils.build_info` for why a static
  version string could not answer that
* per-database probe — literally :func:`~telegram_invite_bot.webhook.
  health.check_databases`, the same call ``/readyz`` makes, but reported
  in the chat so the operator doesn't need shell access during the call.
  NOT ``/healthz``: that route is a pure liveness probe and touches no
  database at all — see the ``healthz`` route in
  :mod:`telegram_invite_bot.webhook.server`, which returns a constant
  200 — so a green ``/healthz`` says nothing about the lines below
* Sentry on/off (DSN configured)
* throttling on/off + capacity / refill
* webhook URL (redacted to host-only — full URL leaks the random path
  suffix that's the security boundary against discovered endpoints)

Non-developer access is silently dropped. We don't reply
"❌ запрещено" because the existence of the command then becomes a
side-channel for enumerating dev IDs: a curious user runs it on every
bot they share with admins and notes which one replies. ``return None``
means the bot looks identical to a user who simply mistyped a slash
command. That is a DELIBERATE DIVERGENCE, not legacy parity: legacy's
``admin_callback_only`` answers the refusal out loud
(``bot.py:26121-26130``, ``answer_callback_query(..., "not_for_you")``).
The reasoning above stands on its own; the parity claim that used to sit
here did not (#408).

The router is PRIVATE-only on top of that gate. The developer check
decides *who* may read this board; the chat filter decides *where* it
may be rendered, and the two are not the same question — the panel
carries the webhook host and every subsystem's configured/not state, so
a developer typing ``/admin_status`` in a shared group during an
incident would publish it to that group. Same posture as
:mod:`handlers.admin.uptime` (uptime.py:107).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot import __version__
from telegram_invite_bot.db.names import ALL_DBS
from telegram_invite_bot.utils.build_info import BuildInfo, read_build_info
from telegram_invite_bot.webhook.health import check_databases

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.status")


async def _probe_databases(registry: EngineRegistry) -> dict[str, bool]:
    """The ``/readyz`` probe, re-keyed by DB name for the chat card.

    This used to be a hand-copied ``SELECT 1`` loop, justified in a
    comment by webhook-specific concerns ``check_databases`` "might"
    grow one day. It never grew them, and the copy drifted exactly the
    way copies do: when #682 made the readiness probe schema-aware, the
    operator-facing board would have kept reporting the old, blind
    green. Delegation is the only shape that cannot drift.

    Only the keys differ — the operator wants ``economy: ❌``, whereas
    the HTTP route must anonymise to ``db2`` so an unauthenticated
    caller can't enumerate the storage layout. That is what
    :func:`check_databases_anonymised` is for; here the names are the
    whole point.
    """
    raw = await check_databases(registry)
    return {db.value: raw[db] for db in ALL_DBS}


def _redact_webhook_url(url: str) -> str:
    """Show host only — the random path suffix is the security boundary.

    Legacy logs the full URL on boot which is fine (logs are operator-
    only) but a chat message can be forwarded; redaction here is
    defence-in-depth against an admin who innocently screenshots
    ``/admin_status`` into a triage channel.
    """
    if not url:
        return "<unset>"
    # Cheap parse — we control the format ourselves; full urllib parse
    # would be overkill for a single grep-out.
    scheme_split = url.split("://", 1)
    if len(scheme_split) != 2:
        return "<malformed>"
    scheme, rest = scheme_split
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/…"


# ``_top_legacy_commands`` and its rendered block existed while the
# strangler bridge was live (a migration-backlog snapshot inside the
# operator's chat). T-011 (2026-05-26) removed the bridge, so the
# counter is gone and the section is too. The status card now stops
# at the throttling line.


def _render_build(info: BuildInfo | None) -> str:
    """One line: what the deploy says it shipped, or that nobody said.

    The unknown branch is the load-bearing one. An operator comparing
    this card against ``git log`` on the Mac needs to be able to tell
    "prod is behind" from "prod cannot say", and only an explicit ⚠️
    does that — a missing line reads as "nothing to report".

    Everything interpolated here came out of a file on the server, so
    it is escaped. ``/admin_*`` cards have been taken down by exactly
    this before (#155): under the bot-wide ``parse_mode=HTML`` a single
    stray ``<`` costs the whole message, and losing the status card is
    worst precisely when someone is reaching for it.
    """
    if info is None:
        return "• build: ⚠️ <i>unknown — deploy did not write BUILD_INFO (see docs/DEPLOY.md)</i>"
    line = f"• build: <code>{html.escape(info.revision)}</code>"
    if info.deployed_at:
        line += f" <i>deployed {html.escape(info.deployed_at)}</i>"
    return line


def _render_status(
    settings: Settings,
    *,
    db_results: dict[str, bool],
    sentry_on: bool,
    build: BuildInfo | None,
) -> str:
    db_lines = "\n".join(
        f"  • <code>{name}</code>: {'✅' if ok else '❌'}" for name, ok in db_results.items()
    )
    throttle = settings.throttling
    throttle_state = (
        f"on (capacity={throttle.capacity}, refill={throttle.refill_per_second}/s)"
        if throttle.enabled
        else "off"
    )
    return (
        f"🩺 <b>Admin status</b>\n\n"
        f"• version: <code>{__version__}</code>\n"
        f"{_render_build(build)}\n"
        f"• env: <code>{settings.app_env.value}</code>\n"
        # ``_redact_webhook_url`` can return the "<unset>" / "<malformed>"
        # sentinels; under the bot-wide parse_mode=HTML an unescaped one
        # makes Telegram reject the whole message with 400 and the
        # developer gets nothing at all.
        f"• webhook: <code>{html.escape(_redact_webhook_url(settings.webhook.url))}</code>\n"
        f"• Sentry: {'on' if sentry_on else 'off'}\n"
        f"• throttle: {throttle_state}\n"
        f"• databases:\n{db_lines}"
    )


async def handle_admin_status(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
) -> None:
    """Render the status card iff the caller is a recognised developer.

    The gate is a defence-in-depth check — middlewares should never
    deliver this to a non-dev, but the handler must not depend on the
    middleware being present (e.g. for tests, or a future refactor
    that moves filters around).
    """
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_status; silently dropped"
        )
        return

    # DSN is a ``SecretStr | None``; treat empty-string DSN as off too —
    # operators sometimes leave the var blank rather than removing it.
    dsn = settings.observability.sentry_dsn
    sentry_on = dsn is not None and bool(dsn.get_secret_value().strip())

    db_results = await _probe_databases(registry)
    text_out = _render_status(
        settings,
        db_results=db_results,
        sentry_on=sentry_on,
        build=read_build_info(),
    )
    await message.answer(text_out)
    log.bind(user_id=user.id).info("/admin_status rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """``settings`` + ``registry`` are app-scoped — captured by closure
    rather than injected per-update so the handler signature stays
    free of DI noise. Tests build a router with stubs and call
    ``feed_update`` directly.
    """
    router = Router(name="admin.status")
    # Chat-type filter after the dev gate in the handler, not instead of
    # it: this one keeps the board out of group chats, that one keeps it
    # away from non-developers (#408). See the module docstring.
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_status(message, settings, registry)

    router.message.register(_entry, Command("admin_status", ignore_case=True))
    return router
