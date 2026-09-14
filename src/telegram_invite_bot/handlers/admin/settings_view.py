"""``/admin_settings`` — redacted readout of resolved configuration.

Operator question after a deploy that touched ``.env``: "did the new
values land, and which ones are still defaults?". The legacy
``settings.json`` was operator-edited in-place, so this question
never came up — the file was the source of truth and a quick
``cat`` answered it. The new pipeline reads from environment +
pydantic defaults, which makes the *effective* value harder to
introspect from the host (it depends on env var precedence, the
order ``.env`` is loaded, and whether systemd's ``Environment=``
won over the ``.env`` file). Surfacing the resolved view inside
Telegram closes that gap.

The card renders one block per :class:`*Config` aggregate so the
operator sees the same grouping pydantic uses, and renders each
field as ``key: value``. Two redaction concerns:

* Secrets — ``BOT_TOKEN``, ``WEBHOOK_SECRET_TOKEN``, ``SENTRY_DSN``,
  any API keys. Pydantic uses :class:`SecretStr` for these; we
  render them as ``<set>`` or ``<unset>`` (never the value) so
  the card is safe to share in operator-only DMs. The
  :class:`SecretStr` repr already redacts on str-coercion, but
  that fallback prints ``**********`` which is information-leaky
  (length-leaking) — explicit set/unset is cleaner.
* Long URLs — the full ``WEBHOOK_URL`` IS printed, on its own row
  above ``WEBHOOK_PATH`` and ``host:port``, because it is the
  direct answer to "is this pointing at staging or prod?" and the
  three rows disagreeing is itself a finding. (#1594: this bullet
  used to promise the opposite — that the URL was withheld against
  Telegram's 4096-char limit — describing a measure the code never
  had. One URL row cannot carry this card past 4096; the length
  posture is the curated field subset below, not per-field
  omission.) The value is operator-set, so it is not an injection
  surface, but it is the only free-text interpolation on the card
  and the card is HTML: a query string with an ``&`` in it would
  break the parse and cost the operator the whole readout, so it
  is escaped.

We surface a curated subset of fields, not every pydantic field
on every config — same rationale as ``/admin_modules``: the
operator wants a scannable checklist of "the dozen knobs that
actually change behaviour", not a 30-block dump. New fields land
here in the same PR that adds the env var, so the surface stays
honest.

Same posture as every other ``/admin_*``: silent-drop for
non-devs, private-only at the router level. The redaction is
defence-in-depth on top of the private-only filter — if the
filter ever broke, the secrets still wouldn't leak.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from pydantic import SecretStr

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.settings_view")


def _redact(value: SecretStr | str | None) -> str:
    """Render a secret as set/unset.

    Never returns the underlying value — even partially. Length-
    revealing renders (``****``) are still information leaks for
    short tokens; this collapses to a binary signal so the operator
    sees configured-vs-not without seeing any character of the
    secret.
    """
    if value is None:
        return "&lt;unset&gt;"
    # SecretStr counts as "set" when it wraps any string; truly-empty
    # configured secrets ("BOT_TOKEN= ") should report unset because
    # they will fail at runtime anyway and the operator wants the
    # diagnostic NOW, not when the next update fires.
    if isinstance(value, SecretStr):
        return "&lt;set&gt;" if value.get_secret_value() else "&lt;unset&gt;"
    return "&lt;set&gt;" if value else "&lt;unset&gt;"


def _yn(value: bool | None) -> str:
    if value is None:
        return "—"
    return "yes" if value else "no"


def _render(settings: Settings) -> str:
    bot = settings.bot
    wh = settings.webhook
    lg = settings.logging
    obs = settings.observability
    paths = settings.paths
    throttle = settings.throttling

    lines = ["⚙️ <b>Effective settings</b>", ""]
    lines.append(f"<i>app_env: <code>{settings.app_env.value}</code></i>")
    lines.append("")

    lines.append("<b>bot</b>")
    lines.append(f"  • BOT_TOKEN: <code>{_redact(bot.token)}</code>")
    lines.append(f"  • ADMIN_CHAT_ID: <code>{bot.admin_chat_id}</code>")
    lines.append(f"  • CHAT_ID: <code>{bot.main_chat_id}</code>")
    # developer_ids is the resolved set — the operator wants to
    # confirm "which IDs does this deploy treat as dev?" without
    # re-deriving the 1..4 + ADMIN_CHAT_ID fallback in their head.
    dev_ids = sorted(bot.developer_ids)
    dev_str = ", ".join(str(i) for i in dev_ids) if dev_ids else "—"
    lines.append(f"  • developer_ids: <code>{dev_str}</code>")
    lines.append("")

    lines.append("<b>webhook</b>")
    # An empty WEBHOOK_URL is the "we run polling, not webhook" mode,
    # so the row IS the diagnostic. Escaped — see module docstring.
    url_render = html.escape(wh.url) if wh.url else "—"
    lines.append(f"  • WEBHOOK_URL: <code>{url_render}</code>")
    lines.append(f"  • WEBHOOK_PATH: <code>{wh.path}</code>")
    lines.append(f"  • host:port: <code>{wh.host}:{wh.port}</code>")
    lines.append(f"  • WEBHOOK_SECRET_TOKEN: <code>{_redact(wh.secret_token)}</code>")
    ssl_pair = wh.ssl_cert is not None and wh.ssl_key is not None
    lines.append(f"  • SSL pair configured: <code>{_yn(ssl_pair)}</code>")
    lines.append("")

    lines.append("<b>logging</b>")
    lines.append(f"  • LOG_LEVEL: <code>{lg.level.value}</code>")
    lines.append(f"  • LOG_JSON: <code>{_yn(lg.json_format)}</code>")
    lines.append("")

    lines.append("<b>observability</b>")
    lines.append(f"  • SENTRY_DSN: <code>{_redact(obs.sentry_dsn)}</code>")
    lines.append("")

    lines.append("<b>paths</b>")
    lines.append(f"  • DATABASE_DIR: <code>{paths.database_dir}</code>")
    lines.append(f"  • LOGS_DIR: <code>{paths.logs_dir}</code>")
    lines.append("")

    lines.append("<b>throttling</b>")
    lines.append(f"  • enabled: <code>{_yn(throttle.enabled)}</code>")
    lines.append(
        f"  • capacity / refill: "
        f"<code>{throttle.capacity}</code> / "
        f"<code>{throttle.refill_per_second}</code>/s"
    )

    return "\n".join(lines)


async def handle_admin_settings(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_settings; silently dropped"
        )
        return
    await message.answer(_render(settings))
    log.bind(user_id=user.id).info("/admin_settings rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.settings")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_settings(message, settings)

    router.message.register(_entry, Command("admin_settings", ignore_case=True))
    return router
