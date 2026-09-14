"""Application bootstrap: build container, configure logging, expose lifecycle.

Stage 1 only assembles the container and hands back a ready ``Dispatcher``
and ``Bot``. Stage 3 adds the FastAPI webhook server; Stage 4+ registers
real routers from ``handlers/``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.fsm.state import State
from dishka import AsyncContainer
from loguru import logger

from telegram_invite_bot.config.logging import configure_logging
from telegram_invite_bot.config.settings import Settings
from telegram_invite_bot.db import EngineRegistry
from telegram_invite_bot.di.container import make_container
from telegram_invite_bot.fsm.ads import AdsStates
from telegram_invite_bot.fsm.custom_title import CustomTitleStates
from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.fsm.group_staff import GroupStaffStates
from telegram_invite_bot.fsm.group_words import GroupWordsStates
from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.fsm.support import SupportStates
from telegram_invite_bot.fsm.withdraw import WithdrawStates
from telegram_invite_bot.handlers.ads import on_expire_ads
from telegram_invite_bot.handlers.broadcast import (
    BroadcastStates,
    cancel_inflight,
    on_expire_broadcast,
)
from telegram_invite_bot.handlers.checks import (
    CheckCreateStates,
    on_expire_check_create,
)
from telegram_invite_bot.handlers.custom_title import on_expire_custom_title
from telegram_invite_bot.handlers.duel import (
    expiry_guard as duel_expiry_guard,
)
from telegram_invite_bot.handlers.duel import (
    on_expire_duel_acceptance,
    on_expire_duel_rolls,
)
from telegram_invite_bot.handlers.groupadmin import (
    on_expire_group_staff,
    on_expire_group_words,
)
from telegram_invite_bot.handlers.p2p import P2pSellStates, on_expire_p2p_sell
from telegram_invite_bot.handlers.p2p_trade import P2pBuyStates, on_expire_p2p_buy
from telegram_invite_bot.handlers.rps import (
    expiry_guard as rps_expiry_guard,
)
from telegram_invite_bot.handlers.rps import (
    on_expire_awaiting_acceptance,
    on_expire_awaiting_moves,
)
from telegram_invite_bot.handlers.support import on_expire_support
from telegram_invite_bot.handlers.transfer_rights import (
    TransferStates,
    on_expire_transfer,
)
from telegram_invite_bot.handlers.withdraw import on_expire_withdraw
from telegram_invite_bot.observability import init_sentry
from telegram_invite_bot.scheduler import FsmTimeoutSweeper, TimeoutRule
from telegram_invite_bot.webhook.payments import (
    reset_reverify_throttle,
    shutdown_reverify_pool,
)


# Per-state timeout configuration (Stage 36, M-G-5). Values now match
# legacy's ``ACCEPT_TIMEOUT_SEC = 60`` + ``CHOOSE_TIMEOUT_SEC = 30``
# parity. M-G-5: each state registers its OWN rule object with a single
# ``timeout_seconds`` field — no more accept/moves dual-field shape that
# embedded state-name literals inside ``TimeoutRule.get_timeout_for_state``.
def _rps_timeout_rules() -> dict[State, TimeoutRule]:
    return {
        RpsStates.awaiting_acceptance: TimeoutRule(
            timeout_seconds=60,
            on_expire=on_expire_awaiting_acceptance,
            guard=rps_expiry_guard,
        ),
        RpsStates.awaiting_moves: TimeoutRule(
            timeout_seconds=30,
            on_expire=on_expire_awaiting_moves,
            guard=rps_expiry_guard,
        ),
    }


# T-018: /duel sweeper rules. Both windows were five minutes in
# legacy, but for two unrelated reasons — the anchor this comment
# used to carry (#1495) pointed at neither, and the claim it made
# was wrong on top of that.
#
# * The ACCEPT window was a hardcoded ``timedelta(minutes=5)`` in
#   ``Duel.__init__`` (``expires_at``), read back by
#   ``Duel.is_expired``. It was never configurable.
# * The PER-ROLL window is the only consumer of
#   ``DUEL_ROUND_TIMEOUT_MINUTES``, in
#   ``DuelManager.cleanup_round_timeouts``, which skips anything
#   whose status is not ``active`` — so the setting never touched
#   the accept window at all.
#
# The new pipeline keeps a single 300s deadline for both states:
# same numbers, one knob instead of one knob and one constant.
def _duel_timeout_rules() -> dict[State, TimeoutRule]:
    return {
        DuelStates.awaiting_acceptance: TimeoutRule(
            timeout_seconds=300,
            on_expire=on_expire_duel_acceptance,
            guard=duel_expiry_guard,
        ),
        DuelStates.awaiting_rolls: TimeoutRule(
            timeout_seconds=300,
            on_expire=on_expire_duel_rolls,
            guard=duel_expiry_guard,
        ),
    }


# GAP-1: the /withdraw interview and the /support session also set FSM
# state but had no sweeper rule, so an abandoned flow lingered forever
# (the withdraw busy-gate then refused re-entry until the user remembered
# /cancel). No coins are escrowed during either interview, so the on_expire
# callbacks only DM the user; the sweeper clears the state afterwards. A
# generous 600s window allows slow typers.
def _withdraw_timeout_rules() -> dict[State, TimeoutRule]:
    return {
        WithdrawStates.awaiting_amount: TimeoutRule(
            timeout_seconds=600,
            on_expire=on_expire_withdraw,
        ),
        WithdrawStates.awaiting_confirm: TimeoutRule(
            timeout_seconds=600,
            on_expire=on_expire_withdraw,
        ),
    }


def _support_timeout_rules() -> dict[State, TimeoutRule]:
    return {
        SupportStates.awaiting_text: TimeoutRule(
            timeout_seconds=600,
            on_expire=on_expire_support,
        ),
    }


def _ads_timeout_rules() -> dict[State, TimeoutRule]:
    # L-61: abandoned /ad form — clear the FSM after 10 minutes (same
    # window as /support) and DM the user the session expired.
    return {
        AdsStates.awaiting_text: TimeoutRule(
            timeout_seconds=600,
            on_expire=on_expire_ads,
        ),
    }


def _p2p_timeout_rules() -> dict[State, TimeoutRule]:
    """EPIC P2P: abandoned sell-FSM steps expire after 10 minutes."""
    rule = TimeoutRule(timeout_seconds=600, on_expire=on_expire_p2p_sell)
    return {
        P2pSellStates.awaiting_amount: rule,
        P2pSellStates.awaiting_currency: rule,
        P2pSellStates.awaiting_price: rule,
        P2pSellStates.awaiting_limits: rule,
    }


def _broadcast_timeout_rules() -> dict[State, TimeoutRule]:
    """Phase A: abandoned /broadcast drafts expire after 10 minutes."""
    return {
        BroadcastStates.awaiting_content: TimeoutRule(
            timeout_seconds=600, on_expire=on_expire_broadcast
        ),
    }


def _custom_title_timeout_rules() -> dict[State, TimeoutRule]:
    """L-21 custom_title item: abandoned title-input flow expires after 10
    minutes. The inventory entry is consumed only inside the title step, so
    an abandoned flow loses nothing — the DM just tells the user to retry."""
    return {
        CustomTitleStates.awaiting_title: TimeoutRule(
            timeout_seconds=600, on_expire=on_expire_custom_title
        ),
    }


def _group_staff_timeout_rules() -> dict[State, TimeoutRule]:
    # RR-4 #38: while this state is set, the admin's plain messages in
    # the group are being read as staff input — 10 minutes is the same
    # window every other prompt-and-wait flow here uses.
    return {
        GroupStaffStates.awaiting_grant: TimeoutRule(
            timeout_seconds=600, on_expire=on_expire_group_staff
        ),
    }


def _group_words_timeout_rules() -> dict[State, TimeoutRule]:
    # RR-4 #43: same reasoning as the staff prompt — while this state is
    # set, the admin's plain messages in the group are being read as
    # filter input, so the window has to close on its own.
    return {
        GroupWordsStates.awaiting_word: TimeoutRule(
            timeout_seconds=600, on_expire=on_expire_group_words
        ),
    }


def _checks_timeout_rules() -> dict[State, TimeoutRule]:
    """L-30 /check_create: abandoned interview expires after 10 minutes.

    No coins are escrowed pre-confirm; without this the busy-lock never
    clears and the user is locked out of /check_create until /cancel.
    """
    rule = TimeoutRule(timeout_seconds=600, on_expire=on_expire_check_create)
    return {
        CheckCreateStates.awaiting_amount: rule,
        CheckCreateStates.awaiting_count: rule,
        CheckCreateStates.awaiting_confirm: rule,
    }


def _transfer_rights_timeout_rules() -> dict[State, TimeoutRule]:
    """L-49 /transfer_rights: abandoned flow expires after 10 minutes.

    Nothing irreversible happens pre-confirm; without this the busy-gate
    never clears and the owner is locked out until /cancel.
    """
    rule = TimeoutRule(timeout_seconds=600, on_expire=on_expire_transfer)
    return {
        TransferStates.awaiting_target: rule,
        TransferStates.awaiting_confirm: rule,
    }


def _p2p_buy_timeout_rules() -> dict[State, TimeoutRule]:
    """EPIC P2P: abandoned buy-side inputs expire after 10 minutes.

    The callback already existed (handlers/p2p_trade.on_expire_p2p_buy)
    but its rule was never registered — so a lingering buy state leaked
    forever and, since FSM state is global per (chat,user), cross-blocked
    the other busy-gated flows (/check_create, /transfer_rights).
    """
    rule = TimeoutRule(timeout_seconds=600, on_expire=on_expire_p2p_buy)
    return {
        P2pBuyStates.awaiting_buy_amount: rule,
        P2pBuyStates.awaiting_express_fiat: rule,
    }


def _app_timeout_rules() -> dict[State, TimeoutRule]:
    """Merge per-domain rule dicts into the single sweeper config.

    Keeping the per-domain helpers small lets each new FSM-driven
    handler land its rules without touching unrelated entries.
    """
    rules: dict[State, TimeoutRule] = {}
    rules.update(_rps_timeout_rules())
    rules.update(_duel_timeout_rules())
    rules.update(_withdraw_timeout_rules())
    rules.update(_support_timeout_rules())
    rules.update(_ads_timeout_rules())
    rules.update(_p2p_timeout_rules())
    rules.update(_broadcast_timeout_rules())
    rules.update(_custom_title_timeout_rules())
    rules.update(_group_staff_timeout_rules())
    rules.update(_group_words_timeout_rules())
    rules.update(_checks_timeout_rules())
    rules.update(_transfer_rights_timeout_rules())
    rules.update(_p2p_buy_timeout_rules())
    return rules


@dataclass(slots=True)
class Application:
    container: AsyncContainer
    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    engines: EngineRegistry
    # ``@username`` from the startup ``get_me()``, without the ``@``.
    # Carried here so consumers that need it outside a handler — the
    # guide site's "open the bot in Telegram" link — don't have to make
    # their own API round trip from a synchronous factory. ``None`` when
    # the Application was built without a live bot (tests); the guide
    # link degrades to a bare ``https://t.me`` in that case.
    bot_username: str | None = None
    _background_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    # Latched by :meth:`close`. Since #279 the graph has two
    # teardown callers in webhook mode — the ASGI lifespan (the one
    # that actually runs under a signal) and the runner's own
    # ``finally`` (the fallback for a ``serve()`` that raised before
    # startup) — and whichever gets there first must make the other
    # a no-op. ``container.close()`` in particular is not safe to
    # call twice.
    _closed: bool = False

    async def start_background(self) -> None:
        """Spawn long-running background tasks (Stage 35: FSM sweeper).

        Called by BOTH the polling runner (``runner/polling.py``) and
        the webhook lifespan (``webhook/server.py``). aiogram's
        ``start_polling`` emits its own startup hooks, but those fire
        *inside* the polling loop — we want the sweeper available
        even in webhook mode (where there is no start_polling), so we
        own the task spawn here rather than using
        ``dispatcher.startup()``. Symmetry between the two runners is
        a goal: same lifecycle in both, no surprise where one
        background task only runs on dev (polling) and not prod
        (webhook).
        """
        storage = self.dispatcher.storage
        sweeper = FsmTimeoutSweeper(
            storage=storage,
            bot=self.bot,
            rules=_app_timeout_rules(),
        )
        task = asyncio.create_task(sweeper.run(), name="fsm_timeout_sweeper")
        self._track_background(task)
        # L-26: hourly economy cleanup — reap expired inventory rows and
        # prune stale game_plays anti-abuse stamps. Opens its own economy
        # sessions via the registry; one CancelledError exits cleanly so
        # the close() cancel loop tears it down with the FSM sweeper.
        # Imported lazily (background-only dependency) so code paths that
        # never start background tasks — e.g. the unmanaged-lifespan
        # webhook path/tests — don't pull the sweeper's module graph in.
        from telegram_invite_bot.scheduler.economy_cleanup import (
            EconomyCleanupSweeper,
        )

        cleaner = EconomyCleanupSweeper(
            self.engines,
            bot=self.bot,
            p2p_pending_ttl_minutes=self.settings.economy.p2p_pending_ttl_minutes,
            # #267: PvP challenges get their own (much shorter) TTL —
            # they were riding the P2P knob.
            pvp_offer_ttl_minutes=self.settings.economy.pvp_offer_ttl_minutes,
            # #169: recipient of the stale-withdrawal alert. Unset
            # (``0``) disables that step only — every reaping job still
            # runs, same degraded-not-broken posture as the bot-less
            # construction above.
            admin_chat_id=self.settings.bot.admin_chat_id,
        )
        cleanup_task = asyncio.create_task(cleaner.run(), name="economy_cleanup")
        self._track_background(cleanup_task)
        # #482: end marriages and relationships whose owner left the group
        # more than a week ago. Legacy ran this off bond READ paths
        # (``bot.py:21586``), so the rule only applied where somebody
        # happened to look; the port never had that hook and the rule has
        # simply not existed since the cutover. Same lazy import and the
        # same bot-less posture as the economy sweeper above — without a
        # bot there is no membership probe, and the pass declines to
        # dissolve anything it cannot verify.
        from telegram_invite_bot.scheduler.left_bonds_cleanup import (
            LeftBondsCleanupSweeper,
        )

        left_bonds_task = asyncio.create_task(
            LeftBondsCleanupSweeper(self.engines, bot=self.bot).run(),
            name="left_bonds_cleanup",
        )
        self._track_background(left_bonds_task)
        logger.bind(component="bootstrap").info(
            "background tasks started: {n}", n=len(self._background_tasks)
        )

    def _track_background(self, task: asyncio.Task[None]) -> None:
        """Keep the task alive and make its death audible.

        The list is a strong reference, which is the point — without
        one the loop can collect a running task mid-sweep. But it also
        means ``asyncio`` never prints "Task exception was never
        retrieved", because that line is emitted from ``__del__``. A
        sweeper that raised on its first pass would be dead for hours
        with nothing in the journal, and the first sign of it would be
        expired FSM states piling up (#1445).
        """
        task.add_done_callback(self._report_background_death)
        self._background_tasks.append(task)

    @staticmethod
    def _report_background_death(task: asyncio.Task[None]) -> None:
        """Log why a background task stopped. Called by the loop."""
        if task.cancelled():
            # The ordinary path: :meth:`close` cancelled it.
            return
        exc = task.exception()
        if exc is None:
            # Both runs are ``while True``, so a clean return means the
            # loop exited by a route nobody designed. Nothing restarts
            # it, so say so rather than letting the silence read as
            # health.
            logger.bind(component="bootstrap").warning(
                "background task {name} returned; nothing will restart it",
                name=task.get_name(),
            )
            return
        logger.bind(component="bootstrap").opt(exception=exc).error(
            "background task {name} died", name=task.get_name()
        )

    async def close(self) -> None:
        """Tear the graph down. Idempotent — see ``_closed``."""
        if self._closed:
            logger.bind(component="bootstrap").debug("application already closed — skipping")
            return
        # Latched BEFORE the first await: two concurrent callers would
        # otherwise both pass the check and race into ``container.close``.
        self._closed = True
        logger.bind(component="bootstrap").info("shutting down application")
        # Cancel background tasks BEFORE closing the bot session — a
        # sweep pass mid-shutdown would otherwise hit a closed
        # aiohttp session and throw spurious errors into the log.
        for task in self._background_tasks:
            task.cancel()
        for task in self._background_tasks:
            # Suppressing every ordinary exception, not just
            # ``CancelledError`` (#1445). ``cancel()`` on a task that
            # already finished is a no-op, and awaiting that task
            # re-raises whatever killed it — out of ``close()``, past
            # every step below. ``_closed`` is latched by then, so the
            # second teardown caller (#279) returns at the guard and
            # the five aiosqlite pools are never disposed at all. A
            # task that died hours ago does not get to decide that.
            # The deaths themselves are not logged here:
            # ``_report_background_death`` already did it, at the
            # moment they happened rather than at shutdown.
            #
            # Awaited one at a time rather than through
            # ``asyncio.gather(..., return_exceptions=True)``, which
            # reads better and does the same thing but is wrong here:
            # ``gather`` binds every argument to the RUNNING loop and
            # raises ``ValueError`` for a task that belongs to another
            # one. Tests build an ``Application`` under one loop and
            # close it under the next; by then these tasks are already
            # cancelled and done, and ``await`` on a finished task
            # hands back its outcome without consulting a loop at all.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background_tasks.clear()
        # The /broadcast fan-out is NOT in the list above: it lives in a
        # module-global set that doubles as its single-flight marker
        # (#1496), so it cannot be handed to this object. Draining it
        # here rather than leaving it to die on a closed session is
        # #1815 — and it has to happen BEFORE ``bot.session.close``,
        # because what the drain buys is the abort report the loop
        # already knows how to send, and that report needs the session
        # this line is about to close.
        await cancel_inflight()
        # Same shape, one layer down: the YooKassa reverify pool is a
        # module-global too (#1439), because OS threads are worth having
        # one set of. Dropping it here discards the deliveries that
        # never got a thread; the ones that did are joined at
        # interpreter exit, and nothing here waits on them.
        shutdown_reverify_pool()
        reset_reverify_throttle()
        # Close the FSM storage AFTER the sweeper task is gone but
        # BEFORE the bot session — the sweeper is the only consumer
        # that might still hold a cursor, and a still-open
        # :class:`SQLiteStorage` connection would otherwise leak the
        # SQLite file handle past process shutdown on platforms that
        # don't auto-clean on interpreter exit (Windows). For
        # :class:`MemoryStorage` the call is a no-op, so this is
        # safe regardless of which backend the operator picked.
        try:
            with contextlib.suppress(Exception):  # noqa: BLE001 — best-effort cleanup
                await self.dispatcher.storage.close()
            await self.bot.session.close()
        finally:
            # Engines must be disposed BEFORE the container — closing the
            # container doesn't await ``dispose`` on APP-scoped resources
            # that aren't context-managed. Both in ``finally`` because
            # ``_closed`` is already latched (#1445): whatever went wrong
            # above, this is the last chance anybody gets to close the
            # pools, and a leaked WAL is a worse outcome than a second
            # exception in the log.
            try:
                await self.engines.dispose()
            finally:
                await self.container.close()


async def build_app() -> Application:
    """Build the application graph and return resolved core dependencies."""

    container = make_container()
    settings = await container.get(Settings)
    configure_logging(settings)
    # Sentry after logging so its own startup line ("SENTRY_DSN not
    # set" / "Sentry initialised") flows through the configured sink
    # rather than the loguru bootstrap default. Before bot/get_me()
    # so an authentication failure (invalid token, network blip) is
    # captured as the *first* event a fresh deploy emits.
    init_sentry(settings)

    bot = await container.get(Bot)
    dispatcher = await container.get(Dispatcher)
    engines = await container.get(EngineRegistry)

    me = await bot.get_me()
    logger.bind(component="bootstrap").info(
        "bot authenticated: username=@{u} id={id} env={env}",
        u=me.username,
        id=me.id,
        env=settings.app_env.value,
    )
    return Application(
        container=container,
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        engines=engines,
        bot_username=me.username,
    )
