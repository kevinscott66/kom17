"""``/admin_telegram_api`` — live Telegram API reachability probe.

Complements /admin_bot_session (static read of the configured Bot
session: API base, timeout, parse_mode) and /admin_dns (resolver-side
reachability) by **actually calling Telegram** with two cheap methods
and reporting back what came back.

The two questions an operator can only answer by issuing a real call:

* ``getMe`` — does the bot token still work? A revoked / rotated
  token, an expired test-instance, or a regional API-key block all
  surface here. The bot's username + id come back, which doubles
  as confirmation that the running process IS the bot the operator
  thinks it is (cross-checking against deploy-time expectations
  catches the "we deployed the wrong env to the right host" class
  of bug).
* ``getWebhookInfo`` — is the webhook still set, and is Telegram's
  side happy with it? The interesting fields are ``url`` (verifies
  what's registered), ``pending_update_count`` (backlog — anything
  above zero with no observable handler activity = aiogram-side
  consumer is stuck), ``last_error_date`` + ``last_error_message``
  (Telegram couldn't deliver — surfaces the exact reason without
  having to scroll Sentry), and ``max_connections`` (the throttle
  Telegram applies; if we lowered it during an incident and forgot
  to raise it back, this is the card that catches it).

Why this is the missing layer:

* /admin_bot_session shows what the Bot OBJECT is configured to
  do — not what Telegram thinks of us.
* /admin_dns proves the host can find api.telegram.org — not that
  api.telegram.org will accept our token.
* /admin_ssl proves TLS is plumbed — not that the TLS handshake
  succeeds against the production endpoint with our trust store.

This card actually round-trips a request and shows the result; it's
the closest thing the diagnostic surface has to a synthetic
end-to-end probe.

Cost: two HTTPS calls. Each is cheap (≤ ~150 ms in steady state)
but they go out over the network, so the card has a hard timeout
per call. Cumulative budget is still under a second on a healthy
deploy — well under any reasonable operator-attention threshold.

Silent-drop for non-devs, private-only at the router level.
"""

from __future__ import annotations

import asyncio
import html
import time
from typing import TYPE_CHECKING

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.telegram_api")


# Hard per-call timeout. Telegram's Bot API typically responds in
# 50-200 ms; 5 s is decisive for a wedged call without being so
# short that a transient hiccup on a slow link triggers a false
# positive. Matches the aiogram default but is enforced
# independently here so the card doesn't inherit a longer override.
_CALL_TIMEOUT_S = 5.0


# Pending-update backlog past which we mark ⚠. Telegram queues
# updates while the webhook can't accept them; a small backlog is
# normal during a restart, but anything above ~10 with no actively
# draining handler is a stuck-consumer signal that the operator
# wants to see immediately.
_PENDING_CONCERNING = 10


class _ApiSnapshot:
    """Captured results of the two probe calls.

    Each field has an ``_error`` companion that holds the exception
    class name when the corresponding call failed; routing-hint
    posture mirrors /admin_dns and /admin_tempdir. Keeping both
    "result" and "error" fields separate (rather than collapsing
    to ``Result | Exception``) makes the render branches readable
    and keeps mypy happy without a TypeGuard dance.
    """

    __slots__ = (
        "get_me_error",
        "get_me_latency_ms",
        "get_me_username",
        "get_me_user_id",
        "webhook_error",
        "webhook_latency_ms",
        "webhook_last_error_date",
        "webhook_last_error_message",
        "webhook_max_connections",
        "webhook_pending",
        "webhook_url",
    )

    def __init__(
        self,
        *,
        get_me_user_id: int | None,
        get_me_username: str | None,
        get_me_latency_ms: float,
        get_me_error: str | None,
        webhook_url: str | None,
        webhook_pending: int | None,
        webhook_max_connections: int | None,
        webhook_last_error_date: int | None,
        webhook_last_error_message: str | None,
        webhook_latency_ms: float,
        webhook_error: str | None,
    ) -> None:
        self.get_me_user_id = get_me_user_id
        self.get_me_username = get_me_username
        self.get_me_latency_ms = get_me_latency_ms
        self.get_me_error = get_me_error
        self.webhook_url = webhook_url
        self.webhook_pending = webhook_pending
        self.webhook_max_connections = webhook_max_connections
        self.webhook_last_error_date = webhook_last_error_date
        self.webhook_last_error_message = webhook_last_error_message
        self.webhook_latency_ms = webhook_latency_ms
        self.webhook_error = webhook_error


async def _probe_get_me(
    bot: Bot, timeout_s: float = _CALL_TIMEOUT_S
) -> tuple[int | None, str | None, float, str | None]:
    """Return ``(user_id, username, latency_ms, error_class_name)``.

    ``username`` is the bot's @handle (without the @). On any
    failure we capture the exception class name and return
    None / 0.0 for the data fields — the render layer sees those
    as "data not available" without having to distinguish
    success-with-zero from failure.
    """
    start = time.monotonic()
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=timeout_s)
    except TimeoutError:
        return None, None, (time.monotonic() - start) * 1000.0, "TimeoutError"
    except Exception as exc:  # noqa: BLE001 - classify any API-side failure
        return None, None, (time.monotonic() - start) * 1000.0, type(exc).__name__
    return (
        me.id,
        me.username,
        (time.monotonic() - start) * 1000.0,
        None,
    )


async def _probe_webhook_info(
    bot: Bot, timeout_s: float = _CALL_TIMEOUT_S
) -> tuple[
    str | None,
    int | None,
    int | None,
    int | None,
    str | None,
    float,
    str | None,
]:
    """Return webhook fields + latency + optional error-class name.

    Tuple shape (in order): url, pending_update_count,
    max_connections, last_error_date, last_error_message,
    latency_ms, error_class_name. Defensive ``getattr`` on each
    field — aiogram exposes optional fields as ``None``, which we
    forward as-is so the render can distinguish "field absent" from
    "field present and empty".
    """
    start = time.monotonic()
    try:
        info = await asyncio.wait_for(bot.get_webhook_info(), timeout=timeout_s)
    except TimeoutError:
        return None, None, None, None, None, (time.monotonic() - start) * 1000.0, "TimeoutError"
    except Exception as exc:  # noqa: BLE001
        return (
            None,
            None,
            None,
            None,
            None,
            (time.monotonic() - start) * 1000.0,
            type(exc).__name__,
        )
    return (
        getattr(info, "url", None) or None,
        getattr(info, "pending_update_count", None),
        getattr(info, "max_connections", None),
        getattr(info, "last_error_date", None),
        getattr(info, "last_error_message", None),
        (time.monotonic() - start) * 1000.0,
        None,
    )


async def _capture(bot: Bot) -> _ApiSnapshot:
    """Run both probes concurrently.

    They don't depend on each other and they each go out on the
    network, so :func:`asyncio.gather` halves the total wait.
    Concurrent issue against the same Bot session is safe — aiogram
    uses an aiohttp pool under the hood, not a shared mutable
    cursor.
    """
    (me_id, me_username, me_latency, me_error), webhook = await asyncio.gather(
        _probe_get_me(bot),
        _probe_webhook_info(bot),
    )
    (
        url,
        pending,
        max_conn,
        last_err_date,
        last_err_msg,
        wh_latency,
        wh_error,
    ) = webhook
    return _ApiSnapshot(
        get_me_user_id=me_id,
        get_me_username=me_username,
        get_me_latency_ms=me_latency,
        get_me_error=me_error,
        webhook_url=url,
        webhook_pending=pending,
        webhook_max_connections=max_conn,
        webhook_last_error_date=last_err_date,
        webhook_last_error_message=last_err_msg,
        webhook_latency_ms=wh_latency,
        webhook_error=wh_error,
    )


def _pending_concerning(snap: _ApiSnapshot) -> bool:
    """``True`` if the backlog exceeds threshold AND the call succeeded.

    A failed getWebhookInfo carries its own ⚠; flagging the
    (missing) backlog on top would double-mark the same condition.
    Same suppression posture as /admin_dns.
    """
    if snap.webhook_error is not None or snap.webhook_pending is None:
        return False
    return snap.webhook_pending > _PENDING_CONCERNING


def _render(snap: _ApiSnapshot) -> str:
    lines = ["📡 <b>Telegram API probe</b>", ""]

    # getMe section.
    lines.append("  <b>getMe:</b>")
    if snap.get_me_error is not None:
        lines.append(
            f"    • <code>failed</code> "
            f"<i>({snap.get_me_error}, "
            f"{snap.get_me_latency_ms:.1f} ms)</i> ⚠"
        )
    else:
        # Both halves are Telegram-provided strings: escape them, and
        # keep the "none" hint OUTSIDE <code> so it reads as a hint
        # rather than as a monospace value the bot actually has (the
        # webhook branch below already does it that way).
        if snap.get_me_username:
            lines.append(
                f"    • <b>username:</b> <code>@{html.escape(snap.get_me_username)}</code>"
            )
        else:
            lines.append("    • <b>username:</b> <i>none</i>")
        lines.append(f"    • <b>id:</b> <code>{snap.get_me_user_id}</code>")
        lines.append(f"    • <b>latency:</b> <code>{snap.get_me_latency_ms:.1f} ms</code>")

    lines.append("")
    lines.append("  <b>getWebhookInfo:</b>")
    if snap.webhook_error is not None:
        lines.append(
            f"    • <code>failed</code> "
            f"<i>({snap.webhook_error}, "
            f"{snap.webhook_latency_ms:.1f} ms)</i> ⚠"
        )
    else:
        # Empty webhook url means the bot is on long-polling — that's
        # a deploy-mode signal, not a problem. Surface as a hint
        # rather than a warning.
        if snap.webhook_url:
            lines.append(f"    • <b>url:</b> <code>{html.escape(snap.webhook_url)}</code>")
        else:
            lines.append("    • <b>url:</b> <i>none (long-polling mode)</i>")
        pending_marker = " ⚠" if _pending_concerning(snap) else ""
        lines.append(
            f"    • <b>pending:</b> "
            f"<code>{snap.webhook_pending if snap.webhook_pending is not None else '?'}</code>"
            f"{pending_marker}"
        )
        if snap.webhook_max_connections is not None:
            lines.append(
                f"    • <b>max_connections:</b> <code>{snap.webhook_max_connections}</code>"
            )
        if snap.webhook_last_error_message:
            # Truncate to keep one bad-day Telegram error from blowing
            # the 4096-char card budget. Operators who need the full
            # message can pull it from Sentry; here we want the head.
            tail = html.escape(snap.webhook_last_error_message[:200])
            lines.append(f"    • <b>last_error:</b> <code>{tail}</code> ⚠")
        lines.append(f"    • <b>latency:</b> <code>{snap.webhook_latency_ms:.1f} ms</code>")

    lines.append("")
    lines.append(
        f"<i>⚠ markers: a call to Telegram failed (token revoked? "
        f"region-blocked? — class name routes the diagnosis), pending "
        f"backlog above {_PENDING_CONCERNING} (stuck consumer — check "
        f"/admin_tasks for a wedged coroutine, /admin_routes for "
        f"unwired handlers), or Telegram reported a delivery error "
        f"to us in last_error_message.</i>"
    )
    return "\n".join(lines)


async def handle_admin_telegram_api(message: Message, settings: Settings, bot: Bot) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_telegram_api; silently dropped"
        )
        return
    snap = await _capture(bot)
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        get_me_error=snap.get_me_error,
        webhook_error=snap.webhook_error,
        pending=snap.webhook_pending,
    ).info("/admin_telegram_api rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.telegram_api")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message, bot: Bot) -> None:
        await handle_admin_telegram_api(message, settings, bot)

    router.message.register(_entry, Command("admin_telegram_api", ignore_case=True))
    return router
