"""Structural invariants of ``rps.build_router`` for group ``/cpc``.

These tests pin the wiring:

* ``cpc`` / ``cpc_cancel`` are each registered exactly once;
* neither carries anything beyond its ``Command`` + ``F.from_user``
  filters — see below;
* the middleware chain is RateLimit → Economy → Session, in that order,
  because the cheap per-user token bucket must reject a flooder before
  the economy/session layers open a DB session for them.

An earlier revision also hung a ``RequireFeature("cpc")`` filter on both
commands and registered a ``FeatureGateMiddleware`` to feed it. That
could never work: aiogram 3 resolves outer middlewares → **filters** →
inner middlewares → handler, and ``router.message.middleware(...)``
registers an *inner* middleware, so the filter ran before the value it
depended on existed. It took its "middleware not installed" branch on
every ``/cpc``, logged a WARNING and returned True — inert, with a false
warning as its only production effect. Wiring the middleware as an outer
one would have been strictly worse: the default policy listed ``cpc``
as disruptive, so every group would have lost the command with no way
back (``set_feature`` had no callers, and the flags lived in
per-instance memory). ``/cmdcfg`` already owns per-command availability,
persisted and operator-facing. Hence the third-filter assertions below:
they fail if a second gate is ever grafted back on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram import F
from aiogram.filters import Command

from telegram_invite_bot.handlers.rps import build_router


@pytest.fixture
def mock_registry():
    """Mock engine registry for testing."""
    return AsyncMock()


def _registered_middlewares(router) -> list:
    """List the BaseMiddleware instances on ``router.message`` in order."""
    return list(router.message.middleware)


def _command_handlers(router, command: str):
    """Return HandlerObjects whose ``Command`` filter matches ``command``."""
    matches = []
    for h in router.message.handlers:
        for f in h.filters:
            cb = f.callback
            if isinstance(cb, Command) and command in cb.commands:
                matches.append(h)
                break
    return matches


# ``F.from_user`` is registered as the BOUND ``MagicFilter.resolve``
# method, not as the MagicFilter object — unwrap ``__self__`` before the
# isinstance check. The class comes from a freshly built ``F`` rather
# than magic_filter's module path, which aiogram subclasses privately.
_MAGIC_FILTER_TYPE = type(F.from_user)


def _non_command_filters(handler) -> list:
    """Filters on ``handler`` that are neither ``Command`` nor a MagicFilter."""
    return [
        f.callback
        for f in handler.filters
        if not isinstance(getattr(f.callback, "__self__", f.callback), Command | _MAGIC_FILTER_TYPE)
    ]


def test_cpc_handler_is_registered_without_a_second_gate(mock_registry):
    """/cpc exists and carries only its Command + F.from_user filters."""
    router = build_router(mock_registry)

    cpc_handlers = _command_handlers(router, "cpc")
    assert len(cpc_handlers) == 1

    assert _non_command_filters(cpc_handlers[0]) == []


def test_cpc_cancel_handler_is_registered_without_a_second_gate(mock_registry):
    """/cpc_cancel is wired the same way as /cpc."""
    router = build_router(mock_registry)

    handlers = _command_handlers(router, "cpc_cancel")
    assert len(handlers) == 1

    assert _non_command_filters(handlers[0]) == []


def test_router_has_middleware_chain(mock_registry):
    """Router should have the correct middleware chain — and nothing else.

    Exact equality, not membership: a middleware that runs on every
    message is expensive enough that adding one should be a deliberate
    edit to this list.
    """
    router = build_router(mock_registry)

    names = [m.__class__.__name__ for m in _registered_middlewares(router)]

    assert names == ["RateLimitMiddleware", "EconomyMiddleware", "SessionMiddleware"]


def test_router_name_and_registration(mock_registry):
    """Router should have correct name and handler registrations."""
    router = build_router(mock_registry)

    assert router.name == "rps"
    assert len(router.message.handlers) >= 2
    assert len(router.callback_query.handlers) >= 3


def test_rate_limiting_middleware_configured(mock_registry):
    """Rate limiting middleware should be configured with appropriate limits.

    This bucket is the whole of ``/cpc``'s flood protection in groups now
    that the feature gate is gone, so its numbers are pinned here.
    """
    router = build_router(mock_registry)

    rate_limit = next(
        (
            m
            for m in _registered_middlewares(router)
            if m.__class__.__name__ == "RateLimitMiddleware"
        ),
        None,
    )
    assert rate_limit is not None

    # Configured for gaming: 5-token bucket, 1 token per 30s refill.
    assert rate_limit._capacity == 5.0
    assert rate_limit._refill_per_second == pytest.approx(1.0 / 30.0)
