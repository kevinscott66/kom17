"""Production run mode: FastAPI + uvicorn behind nginx (or direct SSL).

Builds the :class:`Application` graph once, hands it to
:func:`webhook.server.create_app`, and starts uvicorn programmatically
so the build happens inside the asyncio loop uvicorn owns.

SSL: if ``settings.webhook.ssl_cert``/``ssl_key`` are both set, uvicorn
binds HTTPS directly. Normal prod is fronted by nginx and runs HTTP —
leave the cert/key unset.
"""

from __future__ import annotations

import uvicorn
from loguru import logger

from telegram_invite_bot.app import build_app
from telegram_invite_bot.webhook.server import create_app


async def run() -> None:
    application = await build_app()
    # ``build_app`` opened engines and an aiogram session; from here on
    # every exit path has to run ``application.close()``. The ``try``
    # used to start at ``server.serve()``, which left the whole setup
    # block below outside it — a ``create_app`` import-time failure or a
    # ``uvicorn.Config`` rejection (unreadable TLS material, a bad
    # ``forwarded_allow_ips``) propagated with the engines still open
    # and the session unclosed (#821).
    try:
        fastapi_app = create_app(
            application,
            manage_telegram_webhook=True,
            close_application=True,
            # #1568: deliberately NOT unregistering on shutdown. The
            # normal way this process dies is ``systemctl restart``
            # from ``scripts/deploy.sh``, and handing the webhook back
            # in the gap between the two processes is what turns a
            # brief Telegram outage into a failed deploy. Passed
            # explicitly rather than left to the default so flipping
            # that default cannot silently re-open the hole.
            unregister_webhook_on_shutdown=False,
        )

        settings = application.settings
        config = uvicorn.Config(
            fastapi_app,
            host=settings.webhook.host,
            port=settings.webhook.port,
            log_config=None,  # loguru intercepts uvicorn
            access_log=False,
            ssl_certfile=str(settings.webhook.ssl_cert) if settings.webhook.ssl_cert else None,
            ssl_keyfile=str(settings.webhook.ssl_key) if settings.webhook.ssl_key else None,
            # M-I-8: trust X-Forwarded-* only from nginx on loopback. Without
            # ``proxy_headers=True`` uvicorn ignores the header entirely and
            # every connection sees the request as coming from 127.0.0.1 —
            # access logs, rate limits scoped by client IP, abuse reports all
            # collapse onto the proxy. Enabling it without a whitelist would
            # let any client spoof their IP, so we pin the allow-list to
            # 127.0.0.1 by default (nginx-on-same-host) and let ops widen via
            # ``FORWARDED_ALLOW_IPS`` if the topology changes.
            proxy_headers=True,
            forwarded_allow_ips=settings.webhook.forwarded_allow_ips,
            # #1935: bounded on purpose. uvicorn defaults this to ``None``
            # (``config.py:218``) and feeds it straight to
            # ``asyncio.wait_for`` (``server.py:289``), i.e. it waits for
            # in-flight requests *forever*. One update still inside
            # ``feed_update`` on a slow upstream (AI answer, Whisper
            # transcription) is enough: on SIGTERM ``shutdown()`` blocks,
            # ``self.lifespan.shutdown()`` on the line after it never runs,
            # so ``application.close()`` — the #1815 broadcast drain,
            # ``storage.close()``, ``bot.session.close()`` and
            # ``engines.dispose()`` across all five SQLite pools — is
            # skipped entirely, and systemd SIGKILLs us at its 90 s
            # ``DefaultTimeoutStopSec`` with the databases mid-flight.
            # The ``finally`` below does not save us: SIGKILL runs nothing.
            #
            # 20 s is chosen against that 90 s budget, not against request
            # latency: it leaves ~70 s for the teardown that actually
            # matters. A straggler past 20 s gets cancelled and its update
            # is redelivered by Telegram, which is the cheaper loss.
            timeout_graceful_shutdown=20,
        )
        server = uvicorn.Server(config)
        logger.bind(component="runner.webhook").info(
            "uvicorn starting on {host}:{port}",
            host=settings.webhook.host,
            port=settings.webhook.port,
        )
        await server.serve()
    finally:
        # Belt and braces (#279). Under the normal stop path this is a
        # no-op: the ASGI lifespan shutdown already ran
        # ``application.close()`` (``close_application=True`` above),
        # and ``close()`` is idempotent. It is NOT the primary teardown
        # — uvicorn's ``capture_signals`` restores ``SIG_DFL`` and
        # re-raises the captured SIGTERM as ``serve()`` unwinds, so on
        # a ``systemctl stop`` the process is gone before this line is
        # reached. What it does still cover is the paths where no
        # signal is involved: anything in the block above raising before
        # the lifespan ever started — ``create_app``, a ``uvicorn.Config``
        # rejection, or ``serve()`` failing to bind (port in use, bad TLS
        # material).
        await application.close()
