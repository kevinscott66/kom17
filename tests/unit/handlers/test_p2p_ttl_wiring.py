"""#1686 — the D2 pending-trade TTL reaches the handler-side P2pService.

``P2P_PENDING_TTL_MINUTES`` configures how long a ``pending`` trade
holds the seller's COM in escrow before it is reaped. Two places act on
it: :class:`EconomyCleanupSweeper`, which cancels stale trades on its
60-second pass, and :class:`P2pService`, which lazily expires them on
the handler path so a buyer at the D4 ceiling is not refused because of
trades nobody has swept yet.

Only the sweeper was wired. Every ``EconomyMiddleware`` construction in
the P2P routers used the ``p2p_pending_ttl_minutes=30`` default, so the
two halves silently disagreed the moment the setting moved: below 30
the sweeper cancels trades the handler still treats as live, above 30
escrowed COM returns to the book while the buyer is still paying.

These tests pin the wiring itself rather than the divergence it caused,
because the divergence only shows up with a non-default setting — which
is exactly the case nobody exercises until it is already in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from telegram_invite_bot.handlers import p2p, p2p_trade
from telegram_invite_bot.handlers.chat_scope import scoped_worker
from telegram_invite_bot.middlewares.economy import EconomyMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Router

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db.engines import EngineRegistry

# Deliberately not 30: the default would pass even against the bug.
_TTL = 7


def _settings() -> Settings:
    return cast(
        "Settings",
        SimpleNamespace(
            bot=SimpleNamespace(admin_chat_id=0),
            economy=SimpleNamespace(p2p_pending_ttl_minutes=_TTL),
        ),
    )


def _economy_middlewares(router: Router) -> list[EconomyMiddleware]:
    """Every EconomyMiddleware the factory hung on either event type.

    ``handlers/p2p.py`` returns a ``with_chat_type_refusal`` wrapper
    rather than its own router, and the middlewares live on the worker
    inside it — hence the unwrap, which is a no-op for the factories
    that return their router directly.
    """
    worker = scoped_worker(router)
    found: list[EconomyMiddleware] = []
    for observer in (worker.message, worker.callback_query):
        found.extend(
            m for m in observer.middleware._middlewares if isinstance(m, EconomyMiddleware)
        )
    return found


@pytest.mark.parametrize(
    "build",
    [p2p.build_router, p2p_trade.build_router],
    ids=["p2p", "p2p_trade"],
)
def test_p2p_routers_thread_the_pending_ttl_into_the_service(
    build: Callable[[EngineRegistry, Settings], Router],
) -> None:
    # The registry is only stored by BaseSessionMiddleware.__init__, never
    # touched until an update arrives, so a placeholder is enough here.
    router = build(cast("EngineRegistry", None), _settings())

    middlewares = _economy_middlewares(router)
    assert middlewares, "factory hung no EconomyMiddleware at all"
    assert [m._p2p_pending_ttl_minutes for m in middlewares] == [_TTL] * len(middlewares)
