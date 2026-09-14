"""Background scheduler primitives (Stage 35).

First background pipeline in the strangler refactor. Lands the
:class:`FsmTimeoutSweeper` that expires stale aiogram FSM sessions
according to per-state :class:`TimeoutRule` configuration. Future
stages (/tip auto-deletion, /daily reminder, P2P escrow watchdog)
should reuse the same shape: a frozen-dataclass rule, a pure
``sweep_once`` for unit-testability, an async ``run`` loop for
production wiring, and an :class:`Application`-level startup/shutdown
hook (see :mod:`telegram_invite_bot.app`).

The sweeper is intentionally minimal: it depends only on
:class:`BaseStorage` from aiogram and a wall-clock callable injected
at construction time. No DB session, no settings, no DI container —
keeps unit tests pure and lets the scheduler boot before the rest of
the app graph is up.
"""

from telegram_invite_bot.scheduler.fsm_sweeper import (
    FsmTimeoutSweeper,
    SweepReport,
    TimeoutRule,
)

__all__ = ["FsmTimeoutSweeper", "SweepReport", "TimeoutRule"]
