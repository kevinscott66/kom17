"""Whoever gives up first decides whether the cache gets written (#1614).

A money path reads the USD/RUB fix through
:func:`~telegram_invite_bot.services.payments.fx.resolve_usd_to_rub`,
which waits at most ``FX_TIMEOUT_SECONDS`` and then prices at the offline
anchor. Underneath, ``CurrencyService`` runs its own httpx timeout. Two
deadlines, one race, and the outcomes are not symmetric:

* the service's client fires first — the service falls back, **caches**
  the offline table, and the next caller answers from memory;
* the caller's ``wait_for`` fires first — it cancels the fetch mid-flight,
  nothing is cached, and every following request pays the same wait again.

So the cap has to stay strictly under the ceiling, and both processes that
build an FX-serving ``CurrencyService`` have to apply it: the webhook app
credits rouble payments, and the bot process prices the ``/topup`` screen
off the instance ``build_main_router`` makes.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import telegram_invite_bot
from telegram_invite_bot.services.payments.fx import (
    FX_TIMEOUT_SECONDS,
    FX_UPSTREAM_TIMEOUT_SECONDS,
)
from telegram_invite_bot.webhook.server import _build_fx_service

if TYPE_CHECKING:
    from telegram_invite_bot.app import Application

_SRC = pathlib.Path(telegram_invite_bot.__file__).parent

#: Every module that builds a ``CurrencyService`` a money path reads
#: through. ``handlers/currency.py`` is deliberately absent: its
#: default-constructed fallback only ever answers ``/rate``.
_BUILDERS = ("webhook/server.py", "routers/main_router.py")


def _currency_service_timeouts(relative: str) -> list[str]:
    """Source of the ``timeout=`` argument at each construction site."""
    tree = ast.parse((_SRC / relative).read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "CurrencyService":
            continue
        for kw in node.keywords:
            if kw.arg == "timeout":
                found.append(ast.unparse(kw.value))
    return found


def test_the_cap_sits_below_the_ceiling() -> None:
    assert 0.0 < FX_UPSTREAM_TIMEOUT_SECONDS < FX_TIMEOUT_SECONDS


def test_the_webhook_app_caps_a_generous_configured_timeout() -> None:
    application = SimpleNamespace(
        settings=SimpleNamespace(
            currency=SimpleNamespace(api_key=None, timeout_seconds=60.0, cache_ttl_seconds=3600.0),
            withdraw=SimpleNamespace(coins_per_usdt=1000.0),
        )
    )

    service = _build_fx_service(cast("Application", application))

    assert service._timeout <= FX_UPSTREAM_TIMEOUT_SECONDS  # noqa: SLF001


def test_the_webhook_app_does_not_lengthen_a_short_configured_timeout() -> None:
    application = SimpleNamespace(
        settings=SimpleNamespace(
            currency=SimpleNamespace(api_key=None, timeout_seconds=1.0, cache_ttl_seconds=3600.0),
            withdraw=SimpleNamespace(coins_per_usdt=1000.0),
        )
    )

    service = _build_fx_service(cast("Application", application))

    assert service._timeout == 1.0  # noqa: SLF001


def test_every_money_path_builder_caps_its_upstream_timeout() -> None:
    """Source-level because ``build_main_router`` cannot be built cheaply.

    Reading the call as written is enough for what is being defended:
    somebody adding a third builder, or dropping the ``min`` back to a
    bare setting, fails here rather than in production a quarter later.
    """
    seen = 0
    for relative in _BUILDERS:
        timeouts = _currency_service_timeouts(relative)
        assert timeouts, f"{relative} no longer builds a CurrencyService"
        for expr in timeouts:
            assert expr.startswith("min("), f"{relative}: uncapped timeout {expr!r}"
            assert "FX_UPSTREAM_TIMEOUT_SECONDS" in expr, f"{relative}: capped at {expr!r}"
            seen += 1
    assert seen == len(_BUILDERS)


def _lifespan_warm_up_is_bounded() -> bool:
    """True if the webhook lifespan's FX warm-up waits behind a ceiling.

    Source-level because the call itself is unreachable from a test: it
    is gated on ``manage_telegram_webhook``, i.e. exactly the flag that
    means "this run is allowed to touch the network".
    """
    tree = ast.parse((_SRC / "webhook/server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "usd_to_rub"):
            continue
        # The warm-up is the only ``usd_to_rub`` call in the module; it
        # must sit inside an ``asyncio.wait_for(...)`` argument.
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.Call):
                continue
            outer_func = outer.func
            name = (
                outer_func.attr
                if isinstance(outer_func, ast.Attribute)
                else getattr(outer_func, "id", "")
            )
            if name == "wait_for" and any(node is arg for arg in ast.walk(outer)):
                return True
        return False
    return False


def test_the_lifespan_warm_up_cannot_hang_the_boot() -> None:
    """#1929: an unbounded warm-up never opens the listening socket.

    uvicorn creates its sockets only after ASGI lifespan startup returns,
    so a warm-up that never returns leaves the process alive under
    systemd with the port shut: nginx 502s every webhook POST and no
    health endpoint exists to say why. ``contextlib.suppress`` answers
    the failure case and not the hang, and httpx's per-phase timeout
    does not answer a peer that trickles the body — see
    ``utils/http_read``.
    """
    assert _lifespan_warm_up_is_bounded()
