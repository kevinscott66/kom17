"""Telegram ``setWebhook`` / ``deleteWebhook`` orchestration.

Separated from :mod:`webhook.server` so unit tests can mount the routes
without performing network I/O. The production runner enables it by
passing ``manage_telegram_webhook=True`` to :func:`create_app`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

from loguru import logger

log = logger.bind(component="webhook.lifespan")

# M-I-1: retry policy for ``setWebhook``. The tuple is the wait
# BETWEEN attempts, so three attempts back off 1s then 2s. It used
# to carry a third element and double as the attempt count, which
# made the last delay dead weight and — worse — made the retry log
# promise a sleep that never happened (#819). Attempts and delays
# are separate names now so neither can drift into the other.
#
# The schedule stays tight because the most common transient
# Telegram API failure recovers within seconds, and a tighter
# schedule keeps deploy windows short. After exhausting retries we
# consult ``getWebhookInfo``: if the current registration already
# matches BOTH the URL and the update set we'd register (i.e. a prior
# deploy's webhook is still live and still complete), we log a
# warning and continue startup rather than abort
# the process. Aborting would put systemd into a restart loop
# during a Telegram blip even though the existing webhook is
# perfectly serving traffic.
#
# #1394: this fallback only has something to match against because
# ``webhook/server.py`` skips ``teardown_webhook`` when startup
# raised. Deleting the registration on a FAILED start would blank
# ``info.url`` and make the comparison below unreachable, which is
# what production actually did until then.
_SETUP_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0)
_SETUP_ATTEMPTS: Final[int] = len(_SETUP_RETRY_DELAYS_SECONDS) + 1

if TYPE_CHECKING:
    from telegram_invite_bot.app import Application


def _allowed_updates(application: Application) -> list[str] | None:
    """The update types this dispatcher actually has handlers for.

    Without ``allowed_updates`` Telegram sends its default set — which is
    "everything except ``chat_member`` and the reaction/boost types".
    That is wrong in both directions for this bot:

    * too much — the dispatcher registers ``message``,
      ``callback_query``, ``pre_checkout_query``, ``my_chat_member`` and
      ``chat_member``, so ``edited_message``, ``channel_post``, the
      ``business_*`` family, ``inline_query`` and ``poll_answer`` are
      delivered only to be routed nowhere, one webhook POST each, and
      prod logged aiogram's "Detected unknown update type" warning for
      them;
    * too little — ``chat_member`` is NOT in Telegram's default set, and
      ``handlers/group_events.py`` registers ``_member_changed`` for
      it, so a hand-written list would have to remember to add it or the
      handler would silently never fire. Resolving from the dispatcher
      keeps the subscription honest by construction instead of by
      remembering to edit this file.

    ``start_polling`` resolves this same set on its own, so the webhook
    path was the only one subscribing to the wrong thing. Safe to
    compute here: every router is mounted inside the dispatcher provider
    (``di/providers.py``), long before startup reaches the lifespan.

    Returns ``None`` (⇒ omit the argument, keep Telegram's default) when
    resolution yields nothing: an empty list is not "no preference" to
    Telegram, it is "deliver nothing", which would silently take the bot
    off the air.
    """
    resolved = application.dispatcher.resolve_used_update_types()
    if not resolved:
        log.warning("resolve_used_update_types() came back empty — leaving allowed_updates unset")
        return None
    return resolved


def _allowed_updates_match(desired: list[str] | None, registered: object) -> bool:
    """Does Telegram's live registration cover the update set we resolved?

    ``desired is None`` means we deliberately omitted the argument, so
    whatever Telegram already has is what we would have asked for.

    Otherwise the two must be the same set. ``getWebhookInfo`` omits
    ``allowed_updates`` entirely when the live registration uses
    Telegram's DEFAULT set — and that default is not our resolved list:
    it withholds ``chat_member`` while delivering ``edited_message`` and
    the other types nothing here handles. Reading "absent" as a match is
    how a deploy that adds a handler for a new update type keeps running
    against a registration that will never deliver it. Order is
    Telegram's to choose, so compare as sets; anything that is not a list
    is not proof of a match and must not be read as one.
    """
    if desired is None:
        return True
    if not isinstance(registered, list):
        return False
    return set(registered) == set(desired)


async def setup_webhook(application: Application) -> None:
    """Register the webhook URL with Telegram (idempotent, retried)."""
    settings = application.settings
    url = settings.webhook.url
    if not url:
        log.warning("WEBHOOK_URL is empty — skipping setWebhook (dev/test mode)")
        return

    secret = (
        settings.webhook.secret_token.get_secret_value()
        if settings.webhook.secret_token is not None
        else None
    )
    if secret is None:
        # #2023: the one deployment state that is dangerous AND silent.
        # ``verify_secret_token`` no-ops without a configured secret —
        # deliberately, because dev and staging run without one — and
        # Telegram, told no secret, sends no header. So every forged
        # POST to this now-public URL is dispatched as a genuine update
        # from whatever ``from.id`` the sender wrote, and nothing
        # anywhere says a word: the bot is healthy, the metrics are
        # clean, the journal is quiet. An operator has no way to tell
        # this deployment from a secured one.
        #
        # ``Settings`` already refuses to start with ``APP_ENV=prod``
        # and no secret, so this line is the safety net for the case
        # that check cannot see: a public server whose ``.env`` was
        # seeded from ``.env.example`` (which ships ``APP_ENV=dev``,
        # correctly — it documents the code default) and then filled in
        # with a token and a URL but never an environment. That
        # deployment works flawlessly, which is the problem.
        #
        # Checked here rather than in ``verify_secret_token`` on purpose:
        # once per startup, past the empty-URL guard, so it fires
        # exactly when a public endpoint is being registered and never
        # on a developer's laptop or on every inbound update.
        #
        # An error in the log was the first version of this guard and it
        # was not enough: the deployment it describes still came up and
        # still worked, so the warning only reached an operator who
        # happened to read the journal of a bot that looked healthy.
        # Refusing to start converts the dangerous state from the
        # default into a choice — ``ALLOW_INSECURE_WEBHOOK=1`` — which
        # is the whole difference between "insecure" and "insecure
        # without knowing it".
        public_url = url.rstrip("/") + settings.webhook.path
        if not settings.webhook.allow_insecure:
            raise RuntimeError(
                f"refusing to register {public_url} with no secret token: this "
                "webhook would accept forged updates from anyone, including "
                "updates naming an administrator. Set WEBHOOK_SECRET_TOKEN "
                "(and APP_ENV=prod, which makes it mandatory), or set "
                "ALLOW_INSECURE_WEBHOOK=1 if this URL is a tunnel to a "
                "development machine."
            )
        log.error(
            "registering {url} with NO secret token, because "
            "ALLOW_INSECURE_WEBHOOK is set: this webhook accepts forged "
            "updates from anyone.",
            url=public_url,
        )

    full_url = url.rstrip("/") + settings.webhook.path
    allowed = _allowed_updates(application)

    last_exc: BaseException | None = None
    for attempt in range(1, _SETUP_ATTEMPTS + 1):
        try:
            await application.bot.set_webhook(
                url=full_url,
                secret_token=secret,
                drop_pending_updates=False,
                allowed_updates=allowed,
            )
        except Exception as exc:  # noqa: BLE001 — retry on any transient error
            last_exc = exc
            # The log line has to know whether a retry follows it: an
            # operator who reads "sleeping 4.0s before retry" and then
            # sees "exhausted" a moment later loses time working out
            # which line lied.
            if attempt < _SETUP_ATTEMPTS:
                delay = _SETUP_RETRY_DELAYS_SECONDS[attempt - 1]
                log.warning(
                    "setWebhook attempt {attempt}/{total} failed: {exc!r}; "
                    "sleeping {delay}s before retry",
                    attempt=attempt,
                    total=_SETUP_ATTEMPTS,
                    exc=exc,
                    delay=delay,
                )
                await asyncio.sleep(delay)
            else:
                log.warning(
                    "setWebhook attempt {attempt}/{total} failed: {exc!r}; no retries left",
                    attempt=attempt,
                    total=_SETUP_ATTEMPTS,
                    exc=exc,
                )
            continue
        log.info("webhook registered: {url} (updates={updates})", url=full_url, updates=allowed)
        return

    # All retries exhausted — check if existing registration already
    # matches. If so, the previous deploy's webhook is live and we can
    # safely continue. Otherwise raise the original error.
    log.error(
        "setWebhook failed on all {n} attempts; checking existing registration",
        n=_SETUP_ATTEMPTS,
    )
    try:
        info = await application.bot.get_webhook_info()
    except Exception as info_exc:
        log.error(
            "getWebhookInfo also failed: {exc!r}; cannot verify existing webhook",
            exc=info_exc,
        )
        if last_exc is not None:
            raise last_exc from info_exc
        raise

    current_url = getattr(info, "url", None)
    current_allowed = getattr(info, "allowed_updates", None)
    if current_url == full_url and _allowed_updates_match(allowed, current_allowed):
        log.warning(
            "setWebhook failed but the existing registration matches {url} "
            "(updates={updates}) — continuing startup on the previous "
            "deploy's webhook. It carries the PREVIOUS secret token, and "
            "getWebhookInfo does not return one, so a rotated "
            "WEBHOOK_SECRET_TOKEN cannot be verified here: if it was "
            "rotated, every update is answered 403 and counted as "
            'UPDATES_TOTAL outcome="forbidden" while the bot looks healthy',
            url=full_url,
            updates=allowed,
        )
        return

    log.error(
        "setWebhook failed and the existing registration differs: url "
        "{current!r} (expected {expected!r}), allowed_updates "
        "{current_updates!r} (expected {expected_updates!r})",
        current=current_url,
        expected=full_url,
        current_updates=current_allowed,
        expected_updates=allowed,
    )
    if last_exc is not None:
        raise last_exc


async def teardown_webhook(application: Application) -> None:
    """Unregister on shutdown. Best-effort: failures are logged, not raised."""
    if not application.settings.webhook.url:
        return
    try:
        await application.bot.delete_webhook(drop_pending_updates=False)
        log.info("webhook deleted")
    except Exception:
        log.exception("delete_webhook failed; continuing shutdown")
