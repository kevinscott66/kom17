"""Unit tests for the ecosystem-wide bet ceiling (T-020/R9).

Six games take a stake. Five capped it at 10 000 and ``/cpc`` capped it
at 100 000 — ten times its siblings, for no reason visible from a
player's seat. R9 collapsed all six onto
:mod:`telegram_invite_bot.games.limits`; these tests are what keeps them
collapsed. A bet ceiling that drifts is not a cosmetic bug: it is one
command quietly paying better than the rest.
"""

from __future__ import annotations

import asyncio

from telegram_invite_bot.games.duel import DuelConfig
from telegram_invite_bot.games.limits import MAX_BET, MIN_BET, PLAY_LOCKS
from telegram_invite_bot.games.pot import PVP_PAYOUT_MULTIPLIER
from telegram_invite_bot.games.rps import RpsConfig
from telegram_invite_bot.handlers import games as games_handler
from telegram_invite_bot.handlers import roulette as roulette_handler
from telegram_invite_bot.services import pvp_service, roulette_service, stake_games_service
from telegram_invite_bot.utils.keyed_locks import KeyedLocks

# ----------------------------------------------------------------------
# The sweep — every game, both bounds
# ----------------------------------------------------------------------


def test_every_game_shares_one_ceiling() -> None:
    """The whole point of R9. Any new game that grows its own ``max_bet``
    literal has to be added here, which is the moment to ask whether it
    deserves a different one."""
    ceilings = {
        "/roll": stake_games_service.DICE_MAX_BET,
        "/flip": stake_games_service.FLIP_MAX_BET,
        "/roulette": roulette_service.MAX_BET,
        "/pvp_coin | /pvp_dice": pvp_service.MAX_BET,
        "/duel": DuelConfig().max_bet,
        "/cpc": RpsConfig().max_bet,
    }
    assert set(ceilings.values()) == {MAX_BET}, f"ceilings drifted: {ceilings}"


def test_every_game_shares_one_floor() -> None:
    floors = {
        "/roll": stake_games_service.DICE_MIN_BET,
        "/flip": stake_games_service.FLIP_MIN_BET,
        "/roulette": roulette_service.MIN_BET,
        "/pvp_coin | /pvp_dice": pvp_service.MIN_BET,
        "/duel": DuelConfig().min_bet,
        "/cpc": RpsConfig().min_bet,
    }
    assert set(floors.values()) == {MIN_BET}, f"floors drifted: {floors}"


def test_cpc_is_no_longer_the_outlier() -> None:
    """The specific regression R9 exists for: legacy's
    ``cpc_max_bet = 100000`` against every sibling's 10 000. ``/cpc`` and
    ``/duel`` are the same two-seat pot with different skins, so a player
    choosing between them should be choosing a theme, not a limit."""
    assert RpsConfig().max_bet == DuelConfig().max_bet
    assert RpsConfig().max_bet < 100_000


# ----------------------------------------------------------------------
# The values themselves
# ----------------------------------------------------------------------


def test_the_bounds_are_the_reviewed_values() -> None:
    """A value pin, so moving the ceiling is a deliberate edit with this
    test in the diff rather than a one-character slip."""
    assert (MIN_BET, MAX_BET) == (10, 10_000)


# ----------------------------------------------------------------------
# What R9 actually buys: bounded single-event variance
# ----------------------------------------------------------------------

#: Largest gross payout any single play in the bot can produce, in COM.
#: ~63 USDT at the 900 COM/USDT withdrawal rate. Stated as a ceiling
#: rather than an equality so re-tuning one multiplier downward does not
#: fail an unrelated test — only an INCREASE past the reviewed bound is
#: meant to trip this.
_MAX_SINGLE_PAYOUT = 60_000


def test_no_single_play_can_swing_past_the_reviewed_bound() -> None:
    """Bet ceilings only bound variance together with the multipliers
    they feed. Raising either one alone is the easy mistake; this
    multiplies them back out so the pair is what gets reviewed."""
    gross = {
        "/roll": stake_games_service.DICE_MULTIPLIER * stake_games_service.DICE_MAX_BET,
        "/flip": stake_games_service.FLIP_MULTIPLIER * stake_games_service.FLIP_MAX_BET,
        "/roulette": roulette_service.MULTIPLIER * roulette_service.MAX_BET,
        "/duel | /cpc | /pvp_*": PVP_PAYOUT_MULTIPLIER * MAX_BET,
    }
    worst = max(gross.values())
    assert worst <= _MAX_SINGLE_PAYOUT, f"single-event payout grew: {gross}"


def test_the_biggest_swing_is_the_dice_guess() -> None:
    """Documents WHERE the worst case lives, so a future multiplier
    change lands next to the reason this number is what it is: ``/roll``
    pays 5.7× on a 1-in-6 guess, the longest odds the bot offers."""
    assert (
        max(
            stake_games_service.DICE_MULTIPLIER,
            stake_games_service.FLIP_MULTIPLIER,
            roulette_service.MULTIPLIER,
            PVP_PAYOUT_MULTIPLIER,
        )
        == stake_games_service.DICE_MULTIPLIER
    )


# ----------------------------------------------------------------------
# The play-rate lock — #222-A
# ----------------------------------------------------------------------


async def test_two_different_games_queue_behind_one_player() -> None:
    """The bug #222-A fixed, stated as behaviour.

    The anti-abuse caps count one player's plays across *every* game, so
    the lock that serialises ``check -> play -> record`` has to span
    every game too. It did not: ``/roll`` and ``/flip`` queued in one
    registry and ``/roulette`` in another, so a player firing one of
    each got two locks and no queue — both passed ``check`` before
    either ``record``.

    Two registries interleave here (``roll-in``, ``roulette-in``, ...);
    one registry serialises. Nothing about this asserts on identity, so
    it stays honest if the sharing is ever arranged some other way.
    """
    order: list[str] = []

    async def hold(locks: KeyedLocks[int], name: str) -> None:
        async with locks.acquire(4242):
            order.append(f"{name}-in")
            # Two yields, so an unlocked sibling has every chance to
            # slip in between this coroutine's own two appends.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            order.append(f"{name}-out")

    await asyncio.gather(
        hold(games_handler._stake_locks, "roll"),
        hold(roulette_handler._play_locks, "roulette"),
    )

    assert order == ["roll-in", "roll-out", "roulette-in", "roulette-out"]


async def test_two_players_do_not_queue_behind_each_other() -> None:
    """The other half: sharing the registry must not serialise the bot.

    :class:`KeyedLocks` is keyed by ``user_id``, so one player's plays
    queue and everyone else's stay parallel. A registry that degenerated
    into a global lock would pass the test above and quietly turn every
    game in the bot into a single-file queue.
    """
    order: list[str] = []

    async def hold(user_id: int, name: str) -> None:
        async with PLAY_LOCKS.acquire(user_id):
            order.append(f"{name}-in")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            order.append(f"{name}-out")

    await asyncio.gather(hold(4243, "ann"), hold(4244, "bob"))

    assert order == ["ann-in", "bob-in", "ann-out", "bob-out"]


def test_the_registry_is_left_empty() -> None:
    """No slot outlives the plays above — the leak regression, module-wide.

    ``handlers.roulette`` already pins this for its own name; the shared
    object makes it everyone's invariant.
    """
    assert len(PLAY_LOCKS) == 0
