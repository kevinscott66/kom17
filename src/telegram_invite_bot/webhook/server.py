"""FastAPI app factory — production webhook endpoint.

Routes declared here:

* ``POST {settings.webhook.path}`` — Telegram delivery endpoint. Verifies
  the secret header, then hands the update straight to the aiogram
  ``Dispatcher``. T-011 (2026-05-26) removed the legacy ``telebot``
  strangler bridge; every update is now resolved by the new pipeline.
* ``GET/HEAD /healthz`` — pure liveness. Answers 200 whenever the event
  loop can answer and never touches a database (M-I-7 moved the engine
  probe out of it precisely so a transient SQLite lock stopped causing
  kill-restarts).
* ``GET/HEAD /readyz`` — readiness. Probes every SQLite engine and
  answers 503 unless all of them do.
* ``GET  /metrics`` — Prometheus exposition. A plain route function, not
  a mounted ASGI sub-app.

The site itself (guide, legal, home, contact, discovery) and the payment
callbacks arrive as ``include_router`` calls further down, so the list
above is the routes this module declares, not every route the app serves.

The factory takes a fully-built :class:`Application`. The runner
(``runner/webhook.py``) builds it inside an asyncio context and starts
uvicorn.

Delivery semantics: Telegram's webhook contract is *at-least-once*. It
resends an update whenever a 2xx does not come back in time — a lost
response packet, a restart mid-request, or simply a handler that took
longer than Telegram was willing to wait. Since this endpoint awaits
``feed_update`` before answering, "how long we take to answer" is "how
long the handler runs", and one ``/voice`` is allowed sixty seconds of
speech synthesis before the audio upload even starts. A resend re-runs
the handler from scratch, so without a guard a single user message can
be billed, credited or refunded twice. :func:`create_app` therefore
claims each ``update_id`` before dispatching it — see
``_UPDATE_DEDUP_TTL_SECONDS``.

#1976: that claim is per-process, so of the three causes above it
covers the lost packet and the slow handler and NOT the restart. The
ledger is a ``TTLLRUCache`` built inside :func:`create_app`, so it
dies with the process, and the redelivery that follows a restart
arrives at an empty one and is dispatched as new. That path is
routine, not exotic: ``runner/webhook.py`` sets
``timeout_graceful_shutdown=20`` *so that* a straggler is cancelled
and resent, while a ``/voice`` or ``/ask`` is allowed sixty seconds
upstream. Handlers that spend before a slow await therefore cannot
lean on this cache — ``handlers/ai.py`` (#1965) and
``handlers/voice_transcribe.py`` (#1966) each handle the cancellation
themselves, and ``webhook/payments.py`` keeps its idempotency row in
the database, where a restart cannot reach it. Note what the first
two do and do not buy: they make the cancelled attempt account for
what it already spent; they do not stop the redelivered copy from
running. ``services/transfer_service.py`` carries no such key, which
is written down here as a known limit rather than left to read as
covered. ``tests/regression/test_webhook_update_dedup.py`` pins both
halves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache
from telegram_invite_bot.webhook.health import check_databases_anonymised
from telegram_invite_bot.webhook.http_headers import SecurityHeadersMiddleware
from telegram_invite_bot.webhook.metrics import HEALTH_CHECK_FAILURES, UPDATES_TOTAL
from telegram_invite_bot.webhook.payments import build_router as build_payments_router
from telegram_invite_bot.webhook.security import verify_secret_token

log = logger.bind(component="webhook")

# Our webhook payload ceiling. Telegram publishes no such limit — an
# earlier version of this comment called it "Telegram-documented", and
# nothing in the Bot API docs or in this repo backs that. Real updates
# are tiny (< 100KB even with media metadata); 1MB leaves multi-x
# headroom while bounding the JSON-parser's worst case, and it happens
# to equal nginx's own ``client_max_body_size`` default, which neither
# deployed vhost overrides (``deploy/nginx/bot-vhost.conf``,
# ``deploy/nginx/bot-vhost-shared-host.conf``) — so in production the
# proxy rejects an oversize body before it reaches this process, and the
# checks below are what protects a direct-to-uvicorn deployment.
# Defense in depth on top of ``verify_secret_token`` — protects the
# parser if the secret leaks.
_MAX_BODY_BYTES = 1024 * 1024

# How long a delivered ``update_id`` keeps its claim, and how many
# claims we hold at once.
#
# Telegram retries a webhook delivery over a window of minutes, so an
# hour is many times the interval that actually needs covering; the
# generosity is free because the entry is an int and a bool. The
# capacity is the real bound: past ten thousand updates the LRU drops
# the oldest claims first, which shortens the effective window under
# load but only ever expires claims that are already far older than any
# retry Telegram would still be attempting.
#
# The cache is process-local — which also means restart-local; the
# module docstring above says what that costs (#1976). Within one
# process it is exactly as strong as the rest of this deployment:
# ``runner/webhook.py`` starts uvicorn programmatically
# via ``Server.serve()``, which never forks workers, and
# :func:`_assert_single_worker` below additionally logs an error if
# ``WEB_CONCURRENCY != 1``. Note the invariant holds STRUCTURALLY, not
# because anything is pinned — that error does not stop startup. Switch
# the runner to ``uvicorn.run(...)`` or front it with gunicorn and this
# cache, plus the per-match game locks that have the same requirement,
# quietly stop being correct.
_UPDATE_DEDUP_TTL_SECONDS = 3600.0
_UPDATE_DEDUP_CAPACITY = 10_000

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from telegram_invite_bot.app import Application
    from telegram_invite_bot.services.currency_service import CurrencyService


def _build_fx_service(application: Application) -> CurrencyService:
    """FX source for the R11 rouble top-up price.

    Anchored on ``WITHDRAW_COINS_PER_USDT`` for the same reason
    ``build_main_router`` does it: a coin must never be *sold* off a
    different anchor than it is *bought back* at. Imported lazily so the
    HTTP app graph doesn't pull ``httpx`` + the currency tables at module
    import time.

    The upstream timeout is capped at
    :data:`~telegram_invite_bot.services.payments.fx.FX_UPSTREAM_TIMEOUT_SECONDS`,
    which sits below the ``FX_TIMEOUT_SECONDS`` ceiling every money path
    waits behind — the payment webhooks and the ``/topup`` RollyPay quote
    alike. The reason the cap has to win that race is written where the
    two constants live, next to each other, in
    :mod:`~telegram_invite_bot.services.payments.fx`. The bot process
    caps the same way (``build_main_router``), because ``/topup`` prices
    a real payment off the instance built there.
    """
    from telegram_invite_bot.services.currency_service import CurrencyService
    from telegram_invite_bot.services.payments.fx import FX_UPSTREAM_TIMEOUT_SECONDS

    settings = application.settings
    return CurrencyService(
        api_key=(
            settings.currency.api_key.get_secret_value()
            if settings.currency.api_key is not None
            else None
        ),
        timeout=min(settings.currency.timeout_seconds, FX_UPSTREAM_TIMEOUT_SECONDS),
        cache_ttl_seconds=settings.currency.cache_ttl_seconds,
        coins_per_usdt=settings.withdraw.coins_per_usdt,
    )


def _assert_single_worker() -> None:
    """R-FIX-009-fp: pin the single-worker invariant at app-build time.

    Several correctness guarantees in this process are process-local
    and silently stop holding once a supervisor forks workers. The full
    set of module-level ones, so a reader can check rather than take
    this on trust:

    * ``telegram_invite_bot.handlers.rps._match_locks`` — R-FIX-009's
      TOCTOU fix — and its twin
      ``telegram_invite_bot.handlers.duel._match_locks``.
    * The single-flight confirm gates:
      ``telegram_invite_bot.handlers.checks._confirm_locks``,
      ``telegram_invite_bot.handlers.withdraw._confirm_locks`` and
      ``telegram_invite_bot.handlers.p2p._create_locks``.
    * ``telegram_invite_bot.games.limits.PLAY_LOCKS``, shared by the
      games and roulette stake paths, and
      ``telegram_invite_bot.handlers.voice_transcribe._gate_locks``.
    * ``telegram_invite_bot.handlers.report._COOLDOWN``, the antiflood
      middleware's ``_muted_until`` and ``/donate``'s per-router
      cooldown table: all three are *claims* rather than caches since
      #2014-#2016, so forking would let two workers claim the same one.
    * The ``seen_updates`` dedup cache built a few lines below.

    An earlier version of this list named "the marriage locks with the
    same shape". There are none, and there never were — the marriage
    handlers hold no lock at all. The claim is repaired here rather
    than deleted because the argument needs a list to stand on, and
    :mod:`tests.regression.test_prose_names_real_things` now checks
    that every name above still resolves.

    Why the check lives HERE and not in the runner (#1990). It used to
    be called from :func:`telegram_invite_bot.runner.webhook.run`,
    which is the one entry point that structurally CANNOT fork: it
    builds a :class:`uvicorn.Config` and drives it through
    ``Server.serve()``, and ``serve()`` never forks — uvicorn's forking
    supervisor is ``uvicorn.supervisors.Multiprocess``, reachable only
    from ``uvicorn.run`` and the CLI, neither of which ``src/`` calls.
    So the guard was mounted where it could only ever raise false
    alarms, while the shapes that really do fork — gunicorn with the
    uvicorn worker class, or ``uvicorn --workers N`` — never reach
    ``run`` at all and went unchecked. ``create_app`` is the chokepoint
    every ASGI deployment must pass, forking or not.

    An earlier version of the runner docstring justified all this by
    saying ``uvicorn.Config`` "has no ``workers`` parameter at all".
    That is false as installed (uvicorn 0.46): ``Config.__init__``
    takes ``workers`` and defaults it from ``WEB_CONCURRENCY``. The
    conclusion survived, the stated mechanism did not.

    Known blind spot: a supervisor told ``--workers N`` on the command
    line without exporting ``WEB_CONCURRENCY`` is invisible here. The
    check is a tripwire, not a lock — it never stops startup, because
    on the runner path the variable is inert and refusing to boot over
    an inert variable would be worse than the warning.
    """
    raw = os.environ.get("WEB_CONCURRENCY", "1")
    try:
        workers = int(raw)
    except ValueError:
        log.warning("WEB_CONCURRENCY={raw!r} is not an int; assuming 1", raw=raw)
        return
    if workers != 1:
        log.error(
            "WEB_CONCURRENCY={workers} != 1 — per-match locks and the update"
            " dedup cache are process-local and are not shared between"
            " workers. This process does not fork on its own, so nothing is"
            " broken by the variable alone; a supervisor that DOES fork would"
            " break them. Set 1, or land a cross-process lock backend.",
            workers=workers,
        )


def create_app(
    application: Application,
    *,
    manage_telegram_webhook: bool = False,
    close_application: bool = False,
    unregister_webhook_on_shutdown: bool = False,
) -> FastAPI:
    """Build a FastAPI instance bound to a pre-built ``Application``.

    Parameters
    ----------
    application:
        Fully-constructed Application graph (container, bot, dispatcher,
        engines).
    manage_telegram_webhook:
        When ``True``, the FastAPI lifespan calls Telegram ``setWebhook``
        on startup. Off by default so unit tests don't hit the network.
        It no longer implies the matching ``deleteWebhook`` on shutdown —
        see ``unregister_webhook_on_shutdown``.
    unregister_webhook_on_shutdown:
        When ``True``, a fully-started lifespan also calls Telegram
        ``deleteWebhook`` on the way out. Off by default, and the
        production runner leaves it off on purpose (#1568): a restart
        is the normal way this process dies, and handing the
        registration back in between is what breaks the deploy — the
        teardown branch below spells the sequence out.
    close_application:
        When ``True``, the lifespan owns ``application.close()`` and
        calls it on shutdown. Off by default: the factory has 20+ test
        call sites that build a throwaway app and tear the graph down
        themselves, and closing it from under them would break their
        assertions.

        The *runner* must pass ``True`` (#279). Its own
        ``finally: await application.close()`` cannot be relied on
        under a signal: uvicorn's ``capture_signals`` restores
        ``SIG_DFL`` and re-raises the captured SIGTERM as ``serve()``
        unwinds (``uvicorn/server.py``), so the process dies before
        the ``finally`` body executes. The ASGI lifespan shutdown
        event, by contrast, is driven by uvicorn *inside* ``serve()``,
        while the handler is still installed — which is why the
        teardown has to live here and not in the caller.
    """

    _assert_single_worker()

    fx_service = _build_fx_service(application)

    # Claim ledger for :func:`telegram_webhook`. Values are ignored —
    # only membership matters — but the cache is generic, so ``True``
    # stands in for "claimed".
    seen_updates: TTLLRUCache[int, bool] = TTLLRUCache(
        ttl=_UPDATE_DEDUP_TTL_SECONDS,
        capacity=_UPDATE_DEDUP_CAPACITY,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # #1394: only a startup that got all the way through may
        # unregister the webhook on the way out — see the teardown
        # branch below for why. Bound out here rather than at the point
        # it first matters (#1468), because the ``finally`` reads this
        # name unconditionally: a step that raises before the binding —
        # ``start_background`` opens with a lazy import, which is
        # exactly the kind of step that can — used to raise
        # ``UnboundLocalError`` out of the teardown and bury the real
        # cause. systemd restarts ten seconds later, so the journal
        # would have shown the wrong error for as long as the fault
        # lasted.
        startup_complete = False
        # The whole body is inside the ``try`` on purpose: a startup
        # step that raises half-way through still has to release what
        # the steps before it acquired. Before #279 the teardown lived
        # in a ``try`` opened *after* setWebhook, so a failed startup
        # leaked the background tasks, the bot session and every
        # engine.
        try:
            # Spawn background tasks (Stage 35: FSM timeout sweeper)
            # unconditionally — they're independent of whether we manage
            # Telegram's webhook this run. Mirrors the polling runner's
            # ``application.start_background()`` call so the lifecycle is
            # symmetric between the two run modes. ``application.close()``
            # owns the cancellation; ``manage_telegram_webhook=False`` in
            # tests still benefits from a live sweeper, but its 30s tick
            # means tests rarely observe it unless they wait.
            await application.start_background()
            if manage_telegram_webhook:
                # R11: warm the FX cache before the first payment can
                # land, so a YooKassa webhook never pays the cold-fetch
                # latency inline. Best-effort — a failed warm-up just
                # means the first credit prices at the offline anchor,
                # which is what the bot did for its whole life before
                # R11. Gated on ``manage_telegram_webhook`` for the same
                # reason the setWebhook call is: it is the flag that
                # means "this is a real run", and tests must not touch
                # the network.
                #
                # #1929: the wait is not optional. This runs INSIDE the
                # ASGI lifespan, and uvicorn creates the listening
                # sockets only after startup returns — so a warm-up that
                # never returns is a process that is ``active (running)``
                # under systemd with the port never opened: every webhook
                # POST 502s at nginx and /healthz, /readyz and /metrics
                # are all unreachable, so no probe can tell it from a
                # healthy boot. ``suppress`` answers the failure case,
                # not the hang. And httpx's timeout is per phase, not
                # per call: ``send_capped`` streams the body chunk by
                # chunk and every chunk re-arms the read deadline, so a
                # peer that answers 200 and then trickles bytes below the
                # size cap is never timed out by the client at all —
                # ``utils/http_read`` documents that peer and bounds only
                # the memory half of it. This was the one FX call in the
                # tree not behind ``FX_TIMEOUT_SECONDS``; the ceiling
                # sits above the 2.5s cap ``_build_fx_service`` applies,
                # so on any ordinary slow answer the client still wins
                # and still caches (#1614) — this fires only for the
                # trickle.
                from telegram_invite_bot.services.payments.fx import FX_TIMEOUT_SECONDS

                with contextlib.suppress(Exception):  # noqa: BLE001 — best effort
                    rate = await asyncio.wait_for(fx_service.usd_to_rub(), FX_TIMEOUT_SECONDS)
                    log.info("FX warm: USD/RUB={rate:.4f}", rate=rate)
                from telegram_invite_bot.webhook.lifespan import setup_webhook

                await setup_webhook(application)
            startup_complete = True
            yield
        finally:
            # Order matters: deleteWebhook is an outgoing Bot API call
            # and ``application.close()`` closes the aiohttp session it
            # would travel on. The nested ``try`` guarantees the second
            # step runs even when the first one raises — losing the
            # graph teardown because Telegram was unreachable would be
            # the worse of the two failures.
            try:
                # #1394: gated on ``startup_complete``, so a lifespan
                # unwinding because ``setup_webhook`` RAISED leaves the
                # previous deploy's registration alone. Without the gate
                # the two halves fought each other: setup's documented
                # anti-restart-loop fallback (``lifespan.py:25-33``,
                # ``:174-192``) continues startup when ``getWebhookInfo``
                # shows a live, matching registration — but this branch
                # had already deleted it, so ``info.url`` came back empty,
                # the comparison could never match, ``raise last_exc``
                # aborted, and systemd restarted 10 s later into exactly
                # the loop the fallback exists to prevent. The retry
                # budget is three attempts over 3 s, so a Telegram blip
                # longer than that used to cost the whole deployment.
                #
                # #1568 closes the other half of the same hole. The gate
                # above only covers a startup that FAILED, and that is
                # not how this process usually dies: ``scripts/deploy.sh``
                # ends in ``systemctl restart``, where the outgoing
                # process had ``startup_complete = True`` and therefore
                # deleted the registration on its way out — blanking
                # ``info.url`` before the incoming process ever reached
                # ``setup_webhook``, and disarming the fallback for
                # exactly the deploy it was written to protect. So the
                # unregister is now opt-in and the runner opts out: a
                # registration nobody deletes is re-asserted idempotently
                # by the next ``setWebhook``, and if that call is what
                # fails, the fallback can finally see the live URL it
                # needs to continue on. The cost of leaving it up is that
                # Telegram posts into a closed port for the seconds the
                # restart takes and backs off; the cost of taking it down
                # was the whole deployment.
                if manage_telegram_webhook and startup_complete and unregister_webhook_on_shutdown:
                    # Imported here, not at module scope, so tests can
                    # monkeypatch the module attribute.
                    from telegram_invite_bot.webhook.lifespan import teardown_webhook

                    await teardown_webhook(application)
            finally:
                if close_application:
                    await application.close()

    app = FastAPI(
        title="telegram-invite-bot",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.application = application

    # Security headers on every response. Added before any router so it
    # sits outermost and cannot be bypassed by a route that returns
    # early — including the ones that raise HTTPException.
    app.add_middleware(SecurityHeadersMiddleware)

    # Both flags are read by the shared navigation row that every public
    # page renders (:mod:`telegram_invite_bot.cms.nav`), so they are
    # resolved once, here, ahead of the first router that needs them.
    #
    # The contact form has exactly one recipient, and it is the chat the
    # bot already reports to. With no ADMIN_CHAT_ID there is nowhere to
    # deliver, so the page is not mounted and — because the same flag
    # feeds every context — the documents stop naming it and the nav
    # stops linking to it. A "contact us" page that silently drops every
    # message is worse on this domain than no page at all.
    guide_enabled = application.settings.guide_site.enabled
    contact_enabled = application.settings.bot.admin_chat_id > 0

    # Mount read-only guide sub-app BEFORE the webhook route so its
    # GETs short-circuit cleanly and don't pass through the
    # webhook-secret machinery (different surface, different auth
    # model). The router is built once over a frozen context — no
    # runtime mutation.
    if guide_enabled:
        from telegram_invite_bot.cms.guide_site import (
            GuideSiteContext,
        )
        from telegram_invite_bot.cms.guide_site import (
            build_router as build_guide_router,
        )
        from telegram_invite_bot.cms.guide_site.editor import (
            JsonFileEditorBridge,
        )

        gs_settings = application.settings.guide_site
        guide_ctx = GuideSiteContext(
            guide_file_ru=gs_settings.sources_dir / "telegraph_guide_ru.md",
            guide_file_en=gs_settings.sources_dir / "telegraph_guide_en.md",
            site_title=gs_settings.site_title,
            version=app.version,
            # From the startup ``get_me()`` the bootstrap already made
            # — the page's primary call to action is this link, and a
            # bare ``https://t.me`` is a dead end for every visitor who
            # taps it. ``None`` (an Application built without a live
            # bot) still degrades to that fallback rather than raising.
            bot_username=application.bot_username,
            url_prefix=application.settings.webhook.url,
            contact_enabled=contact_enabled,
            # Editor bridge: writes the same JSON file the legacy
            # ``bot.py`` reads (``$SETTINGS_FILE`` /
            # ``database/settings.json``). T-026 cut the dependency
            # on the legacy module — see ADR 0015 for the migration
            # rationale.
            editor_bridge=JsonFileEditorBridge(application.settings.paths.resolved_settings_file()),
        )
        app.include_router(build_guide_router(guide_ctx))

    # Legal documents (/privacy, /terms, /support, + /en). Mounted
    # UNCONDITIONALLY, unlike the guide: an acquiring bank's condition is
    # that the documents stay permanently reachable, and a page that an
    # operator can switch off with one env var does not satisfy "always
    # available". They also cost nothing when nobody asks — no file
    # reads, no database, no scheduler.
    from telegram_invite_bot.cms.legal.context import LegalContext
    from telegram_invite_bot.cms.legal.router import build_router as build_legal_router

    legal_cfg = application.settings.legal
    legal_ctx = LegalContext(
        site_title=application.settings.guide_site.site_title,
        operator=legal_cfg.operator(application.settings.guide_site.site_title),
        operator_details=legal_cfg.operator_details,
        support_url=legal_cfg.support_url,
        support_email=legal_cfg.support_email,
        bot_username=application.bot_username,
        url_prefix=application.settings.webhook.url,
        contact_enabled=contact_enabled,
        guide_enabled=guide_enabled,
    )
    app.include_router(build_legal_router(legal_ctx))

    # Front page ("/" and "/en"). Also unconditional, and for the same
    # reason as the documents: the bare domain is what a reviewer types
    # by hand and what anyone gets by trimming a shared link, and until
    # this router existed it returned a 404. It reuses the legal context
    # rather than building a second one — a duplicate copy of the site
    # title, the bot username and the origin is only a second place for
    # them to disagree. The guide *section* of the page is conditional so
    # the front page never advertises a ``/commands`` that is switched
    # off.
    from telegram_invite_bot.cms.home import build_router as build_home_router

    app.include_router(build_home_router(legal_ctx))

    # Contact form ("/contact" and "/contact/en"). The documents have to
    # name a channel that works without installing the bot first — a
    # regulator, an acquiring bank or someone asking about their own data
    # all arrive with no Telegram account — and the alternative was
    # publishing the owner's personal mailbox on a public page.
    if contact_enabled:
        from telegram_invite_bot.cms.contact import build_router as build_contact_router

        admin_chat_id = application.settings.bot.admin_chat_id

        async def deliver_contact(text: str) -> None:
            """Hand one submission to the operator's chat.

            Deliberately does **not** swallow the aiogram error: the
            router turns a raised exception into an honest "it did not
            arrive, try again" for the sender. A caught-and-logged
            failure here would show them a success page for a message
            nobody will ever read.
            """
            await application.bot.send_message(admin_chat_id, text, parse_mode="HTML")

        app.include_router(build_contact_router(legal_ctx, deliver=deliver_contact))

    # ``/robots.txt`` and ``/sitemap.xml``. Mounted last of the site's
    # routers because it is the only one that has to know what the
    # others decided: the sitemap lists exactly the pages this
    # deployment serves, so it reads the same two flags they were
    # mounted under. Cloudflare answers ``/robots.txt`` at the edge
    # today with its managed content-signals block — that block has no
    # ``User-agent`` line and no ``Sitemap:``, so until this router
    # existed the first two requests every crawler makes were answered
    # by something that says nothing about this site.
    from telegram_invite_bot.cms.discovery import build_router as build_discovery_router

    app.include_router(build_discovery_router(legal_ctx))

    # The page for every address that is none of the above. An exception
    # handler rather than a catch-all route, so it cannot shadow a real
    # page however it is ordered; installed after the site's routers all
    # the same, because that is the order it runs in. Given the same
    # context they are, and for the same reason: its navigation row has
    # to list what this deployment actually serves, or the apology hands
    # the reader a second dead link.
    from telegram_invite_bot.cms import notfound

    notfound.install(app, legal_ctx)

    # Payment provider callbacks (Crypto Pay, YooKassa, Stripe). Mounted
    # unconditionally because the legacy ``main.py`` always exposed these
    # three routes; gating them behind a flag would change the cutover
    # surface mid-migration. The handlers themselves no-op (403/400) when
    # their provider secrets aren't configured in env.
    #
    # T-020 (R11): the YooKassa route prices roubles off the live USD/RUB
    # fix, so it needs an FX source. This is a *second* CurrencyService
    # instance — ``build_main_router`` keeps its own for /rate and the
    # profile card — because that one lives in a closure the webhook
    # process can't reach, and lifting it would mean threading a bot-side
    # service through the HTTP app graph. The duplication costs one extra
    # upstream call per hour per process and buys full independence
    # between the two surfaces; both read the same endpoint with the same
    # withdraw anchor, so they cannot quote different prices.
    app.include_router(build_payments_router(fx_service))

    webhook_path = application.settings.webhook.path

    @app.post(webhook_path)
    async def telegram_webhook(request: Request) -> Response:
        # Every other refusal in this route bumps ``UPDATES_TOTAL``; this
        # one did not, so the single failure that stops the bot dead was
        # the one failure Prometheus could not see (#820). It reads two
        # ways and both matter: a sustained rate means either someone
        # who learned the webhook URL is forging updates, or a secret
        # rotation has left Telegram 403-ing every real update — a
        # failure whose only other symptom is "the bot went quiet".
        try:
            verify_secret_token(request, application.settings)
        except HTTPException:
            UPDATES_TOTAL.labels(outcome="forbidden").inc()
            raise

        # Cheap pre-parse size gate on the header. Catches the honest
        # large-CL case before we even touch the stream. NOTE: this is
        # NOT sufficient on its own — a client using
        # ``Transfer-Encoding: chunked`` omits Content-Length entirely,
        # so the streaming check below is the real enforcement.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    UPDATES_TOTAL.labels(outcome="error").inc()
                    # Starlette renamed the constant in 0.40; the old
                    # spelling is deprecated. ``filterwarnings = error``
                    # would promote that to a CI failure if we used the
                    # old name.
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="payload exceeds maximum allowed size",
                    )
            except ValueError as exc:
                UPDATES_TOTAL.labels(outcome="error").inc()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid content-length header",
                ) from exc

        # Streaming size enforcement. ``request.body()`` /
        # ``request.json()`` accumulate the entire payload into memory
        # without a built-in cap, so a chunked-encoded attacker who
        # omits Content-Length can OOM the process before parsing
        # would ever fail. Reading the stream ourselves lets us bail
        # the instant the running total exceeds the limit.
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > _MAX_BODY_BYTES:
                    UPDATES_TOTAL.labels(outcome="error").inc()
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="payload exceeds maximum allowed size",
                    )
        except HTTPException:
            raise
        except Exception as exc:
            UPDATES_TOTAL.labels(outcome="error").inc()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="failed to read request body",
            ) from exc

        try:
            raw_payload: Any = json.loads(body) if body else None
        except Exception as exc:
            UPDATES_TOTAL.labels(outcome="error").inc()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid json body",
            ) from exc

        # Telegram always sends a JSON object. A list/scalar would crash
        # downstream at ``payload.get(...)`` with a 500; reject explicitly
        # with 400 instead so the metric and the operator see the right
        # category. Note: this is NOT defensive overreach — Starlette's
        # ``request.json()`` happily returns ``list``, ``int``, etc.
        if not isinstance(raw_payload, dict):
            UPDATES_TOTAL.labels(outcome="error").inc()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="payload must be a JSON object",
            )
        payload: dict[str, Any] = raw_payload

        # Tracing context. ``request_id`` is generated per webhook call
        # so a single Telegram update can be correlated across the
        # router → middleware → repo log lines (loguru threads bound
        # context into ``logger.bind`` calls inside ``contextualize``).
        # ``update_id`` comes from Telegram and is what users / ops will
        # quote when reporting issues.
        request_id = uuid.uuid4().hex[:12]
        update_id = payload.get("update_id")
        with logger.contextualize(request_id=request_id, update_id=update_id):
            try:
                update = Update.model_validate(payload, context={"bot": application.bot})
            except Exception:
                # Malformed JSON / unknown update type. A retry won't
                # help — the payload itself is wrong — so we ack 200
                # to prevent Telegram from looping the poison forever.
                # This is the deliberate at-most-once branch.
                UPDATES_TOTAL.labels(outcome="parse_error").inc()
                log.exception("update parse failed")
                return Response(status_code=status.HTTP_200_OK)

            # SEC: claim BEFORE dispatching, not after. The resend
            # this guards against is the one Telegram sends *because*
            # the first delivery is still running — recording the id on
            # completion would let the duplicate in through the very
            # window it needs to be kept out of. Between the read and
            # the write there is no ``await``, so on the single event
            # loop this pair is atomic and two simultaneous copies
            # cannot both find the slot empty.
            now = time.monotonic()
            if seen_updates.get(update.update_id, now) is not None:
                UPDATES_TOTAL.labels(outcome="duplicate").inc()
                log.warning("duplicate update suppressed — not dispatched")
                return Response(status_code=status.HTTP_200_OK)
            seen_updates.put(update.update_id, True, now)

            try:
                await application.dispatcher.feed_update(application.bot, update)
            except Exception:
                # Dispatch failure is treated as transient (DB lock,
                # router corruption, middleware bug). Note: the
                # ``errors`` router (handlers/errors.py) catches handler
                # exceptions and returns ``True``, so anything reaching
                # this ``except`` is an aiogram-internal failure that
                # the per-handler error path could NOT consume. Return
                # 500 so Telegram retries with exponential backoff —
                # that is exactly the at-least-once contract we want.
                #
                # Release the claim: this is the one branch that *asks*
                # Telegram to send the update again, and a claim left
                # standing would make the dedup guard swallow the retry
                # we just requested — turning a transient failure into
                # a silently dropped update.
                seen_updates.discard(update.update_id)
                UPDATES_TOTAL.labels(outcome="dispatch_error").inc()
                log.exception("update dispatch failed")
                return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            UPDATES_TOTAL.labels(outcome="new").inc()
            log.debug("update dispatched")
            return Response(status_code=status.HTTP_200_OK)

    # GET and HEAD: an uptime monitor that only needs "is it up" opens
    # with HEAD, and a 405 there reads as an outage.
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    async def healthz() -> JSONResponse:
        # M-I-7: pure liveness probe. Returns 200 as long as the event
        # loop can answer; never touches the DB. A transient DB lock
        # used to flip ``/healthz`` to 503 which made Kubernetes /
        # systemd kill-restart the process — bouncing the bot when the
        # right answer was to take it out of the load balancer until
        # the DB recovered. Readiness moved to ``/readyz`` below.
        return JSONResponse({"status": "ok"}, status_code=status.HTTP_200_OK)

    @app.api_route("/readyz", methods=["GET", "HEAD"])
    async def readyz() -> JSONResponse:
        # M-I-7: readiness — every DB engine must answer. Body uses the
        # anonymised ``db1`` .. ``dbN`` slot names so the response can't
        # be used to enumerate the deployment's storage layout. Failure
        # bumps ``tib_health_check_failures_total{probe="readyz"}`` so
        # ops alerts can fire on a sustained readiness drop without
        # needing to parse the JSON body.
        db_results = await check_databases_anonymised(application.engines)
        ready_count = sum(1 for v in db_results.values() if v)
        total = len(db_results)
        ok = ready_count == total
        payload = {
            "status": "ok" if ok else "degraded",
            "databases": db_results,
            "ready": ready_count,
            "total": total,
        }
        code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
        if not ok:
            HEALTH_CHECK_FAILURES.labels(probe="readyz").inc()
        return JSONResponse(payload, status_code=code)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
