"""Reading the live USD/RUB fix on a money path, safely.

:mod:`~telegram_invite_bot.services.payments.rates` owns the *formula*
that turns roubles into coins; this module owns the one input that
formula cannot compute — the fix itself — and the discipline for
fetching it when a payment is waiting on the answer.

It exists as its own module because two call sites need identical
behaviour and used to have only one copy between them: the payment
webhooks credit a rouble payment (``webhook/payments.py``), and the
``/topup`` RollyPay screen quotes one (``handlers/topup.py``). A quote
computed at a different rate than the credit is exactly the drift
``rates`` was written to prevent, one layer up.

The discipline in one line: **never fail, never block**. Every failure
mode — no service wired, upstream down, upstream slow, garbage payload —
lands on :data:`~telegram_invite_bot.services.payments.rates.
FALLBACK_USD_TO_RUB`, which reproduces the historic 10-coins-per-rouble
price exactly. A top-up must not be refused because an FX endpoint had a
bad minute.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.services.payments.rates import FALLBACK_USD_TO_RUB

if TYPE_CHECKING:
    from telegram_invite_bot.services.currency_service import CurrencyService

log = logger.bind(component="services.payments.fx")

#: Hard ceiling on an FX lookup that is pricing money (R11).
#:
#: :class:`~telegram_invite_bot.services.currency_service.CurrencyService`
#: caches for an hour and is warmed at startup, so the money paths
#: virtually always hit memory. The one time they do not — first request
#: after a cold start, or a TTL expiry landing on this one — we would
#: otherwise inherit the service's own 10s upstream timeout *inside a
#: payment webhook*. Three seconds, then fall back to the offline
#: anchor: a slightly stale rate credited promptly beats a correct rate
#: credited after the provider gave up and retried.
FX_TIMEOUT_SECONDS = 3.0

#: Upper bound on the httpx timeout a CurrencyService that feeds a
#: money path may be built with — deliberately BELOW
#: :data:`FX_TIMEOUT_SECONDS` (#1614).
#:
#: It matters which of the two fires first. When the service's own
#: client gives up, the service falls back to its offline table and
#: caches it, so the next caller answers from memory. When the
#: caller's ``wait_for`` wins instead, it cancels the fetch before
#: anything is cached, and EVERY subsequent call repeats the same
#: three-second wait — a slow-but-alive upstream then prices at the
#: offline anchor forever, with no cache entry to expire and no way
#: back. The cap must therefore stay strictly below the ceiling;
#: ``tests/regression/test_fx_timeout_cap.py`` pins that, along with
#: the rule that every money-path builder applies the cap.
FX_UPSTREAM_TIMEOUT_SECONDS = 2.5


async def resolve_usd_to_rub(service: CurrencyService | None) -> float:
    """Live USD/RUB for the rouble leg, or the offline anchor.

    ``service`` is optional because both callers can legitimately run
    without one (a test router, a webhook app built before the currency
    service is wired) and neither should degrade to a crash when the
    honest degradation is a known-good constant.
    """
    if service is None:
        return FALLBACK_USD_TO_RUB
    try:
        return await asyncio.wait_for(service.usd_to_rub(), FX_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — pricing must degrade, not fail
        # The TYPE, not just the message. ``asyncio.wait_for`` raises a
        # ``TimeoutError`` whose ``str()`` is empty, so interpolating
        # the exception alone logged "FX lookup failed ()" — which is
        # the one failure mode this module most needs to distinguish
        # (see :data:`FX_UPSTREAM_TIMEOUT_SECONDS`: our timeout winning
        # the race leaves nothing cached and repeats forever, the
        # service's own timeout does not). Prod carried three such
        # blank lines before anyone noticed they said nothing.
        detail = str(exc).strip()
        log.warning(
            "FX lookup failed ({kind}{detail}) — pricing at the offline anchor {rate}",
            kind=type(exc).__name__,
            detail=f": {detail}" if detail else "",
            rate=FALLBACK_USD_TO_RUB,
        )
        return FALLBACK_USD_TO_RUB
