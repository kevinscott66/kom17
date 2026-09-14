"""The rank gate consumes updates, so it must be the LAST message gate.

``CommandAccessMiddleware`` is not a filter that annotates and steps
aside: both of its refusal branches — the ``/cmdcfg … 6`` kill switch and
the plain below-rank denial — answer the user and ``return`` without
calling ``handler``. In aiogram an outer middleware that returns without
calling the next one *ends the update*: the chain is built by
``MiddlewareManager.wrap_middlewares`` with ``reversed(middlewares)``
(aiogram/dispatcher/middlewares/manager.py:66), so the FIRST registered
outer middleware is the outermost and everything registered after it
lives inside its call.

Until #1918 the gate was registered ahead of both automods, which made a
rank refusal a way to *skip* them:

* ``WordFilterAutomodMiddleware`` never scanned the text, so
  ``/dev <banned word>`` from a below-rank caller kept its banned word in
  the chat — the refusal notice was the only thing that happened.
* ``AntifloodMiddleware`` never counted the message, so a denied command
  was free: hammering a disabled command could not move the sliding
  window that mutes a flooder.

The ordering used to be justified by "a denied command must not earn
coins". That justification was void the whole time:
``MessageActivityMiddleware`` returns before both stats and earning for
any text starting with ``/`` (middlewares/message_activity.py:364 and
:472) — pinned by
``tests/e2e/handlers/test_message_activity.py::test_command_message_neither_counts_nor_earns``.
No command has ever earned coins at any position in the chain, so moving
the gate to the end costs nothing.

That the refusal really does consume the update — the premise this whole
file rests on — is proved by
``tests/e2e/handlers/test_command_access.py::test_below_min_rank_denied_and_consumed``.

The guard is structural, read off the live router tree rather than from
a remembered list, because the failure mode is a silent one: reordering
the registrations back leaves every per-handler suite green.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram_invite_bot.handlers.antiflood import AntifloodMiddleware
from telegram_invite_bot.handlers.command_access import CommandAccessMiddleware
from telegram_invite_bot.handlers.wordfilter import WordFilterAutomodMiddleware

if TYPE_CHECKING:
    from aiogram import Dispatcher, Router


def _main_router(dispatcher: Dispatcher) -> Router:
    for router in dispatcher.sub_routers:
        if router.name == "main":
            return router
    raise AssertionError("the production tree has no router named 'main'")


def _outer_index(router: Router, cls: type) -> int:
    for index, middleware in enumerate(router.message.outer_middleware):
        if isinstance(middleware, cls):
            return index
    raise AssertionError(f"{cls.__name__} is not a message outer middleware")


def test_both_automods_run_before_the_rank_gate(
    production_dispatcher: Dispatcher,
) -> None:
    """#1918: a refused command must still be screened and still counted."""
    root = _main_router(production_dispatcher)
    gate = _outer_index(root, CommandAccessMiddleware)

    assert _outer_index(root, WordFilterAutomodMiddleware) < gate
    assert _outer_index(root, AntifloodMiddleware) < gate


def test_the_rank_gate_is_the_last_message_outer_middleware(
    production_dispatcher: Dispatcher,
) -> None:
    """The general rule, so the next consumer added after it is caught.

    Anything registered after the gate inherits the #1918 bug for free:
    it would run for every message except the ones the bot refused. New
    outer middlewares therefore belong before it — and if one genuinely
    has to run last, it has to answer for the denied-command case first.
    """
    root = _main_router(production_dispatcher)
    outer = list(root.message.outer_middleware)

    assert isinstance(outer[-1], CommandAccessMiddleware)


def test_the_callback_gate_is_the_same_instance_as_the_message_gate(
    production_dispatcher: Dispatcher,
) -> None:
    """#1428's invariant, re-asserted here because #1918 moved the pair.

    The stale-override snapshot that keeps the kill switch alive through
    a ``moderation.db`` failure lives on the instance, so two instances
    would mean a button that stays live after the typed command dies.
    """
    root = _main_router(production_dispatcher)
    message_gate = next(
        m for m in root.message.outer_middleware if isinstance(m, CommandAccessMiddleware)
    )
    callback_gate = next(
        m for m in root.callback_query.outer_middleware if isinstance(m, CommandAccessMiddleware)
    )

    assert message_gate is callback_gate
