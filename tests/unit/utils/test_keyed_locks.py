"""``KeyedLocks`` — mutual exclusion that also frees its slots.

Two properties, and they pull against each other. The registry must
stop growing (the plain ``dict[user_id, asyncio.Lock]`` it replaced in
``handlers/games.py`` and ``handlers/roulette.py`` kept one Lock per
user who ever played, forever), and it must never drop a slot somebody
is holding or waiting on — a dropped slot means the next caller builds
a fresh lock and runs *concurrently* with the current holder, which is
the double-spend window the lock exists to close.
"""

from __future__ import annotations

import asyncio

import pytest

from telegram_invite_bot.utils.keyed_locks import KeyedLocks


@pytest.mark.asyncio
async def test_slot_is_freed_when_the_last_user_leaves() -> None:
    locks: KeyedLocks[int] = KeyedLocks()

    for uid in range(100):
        async with locks.acquire(uid):
            assert len(locks) == 1  # only the live one is tracked
    assert len(locks) == 0


@pytest.mark.asyncio
async def test_waiter_keeps_the_slot_alive() -> None:
    """The holder's exit must not free a slot a waiter is parked on.

    If it did, the waiter would be handed the old lock while a third
    caller ``acquire``\\ s a brand-new one — two "exclusive" sections
    running at once.
    """
    locks: KeyedLocks[int] = KeyedLocks()
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    seen: list[asyncio.Lock] = []

    async def holder() -> None:
        async with locks.acquire(7) as lock:
            seen.append(lock)
            order.append("holder-in")
            entered.set()
            await release.wait()
            order.append("holder-out")

    async def waiter() -> None:
        async with locks.acquire(7) as lock:
            seen.append(lock)
            order.append("waiter-in")
            # The slot the waiter woke up on is still THE registered
            # one — this is the assertion that fails if the holder's
            # exit frees the slot regardless of who is queued: a third
            # caller arriving here would allocate a second lock for
            # the same key and run alongside us.
            assert len(locks) == 1

    holder_task = asyncio.create_task(holder())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    # Let the waiter reach the lock and park on it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    release.set()
    await asyncio.gather(holder_task, waiter_task)

    # The waiter never overlapped the holder…
    assert order == ["holder-in", "holder-out", "waiter-in"]
    # …both ran on the same lock object…
    assert seen[0] is seen[1]
    # …and the slot is gone now that both are done.
    assert len(locks) == 0


@pytest.mark.asyncio
async def test_same_key_serialises_and_different_keys_do_not() -> None:
    locks: KeyedLocks[int] = KeyedLocks()
    overlap = 0
    peak = 0

    async def section(key: int) -> None:
        nonlocal overlap, peak
        async with locks.acquire(key):
            overlap += 1
            peak = max(peak, overlap)
            await asyncio.sleep(0)
            overlap -= 1

    await asyncio.gather(*(section(1) for _ in range(5)))
    assert peak == 1  # same key — strictly one at a time

    peak = 0
    await asyncio.gather(*(section(k) for k in range(5)))
    assert peak == 5  # distinct keys — fully parallel


@pytest.mark.asyncio
async def test_slot_is_freed_when_the_body_raises() -> None:
    """An exception inside the block must not strand the slot."""
    locks: KeyedLocks[int] = KeyedLocks()

    with pytest.raises(RuntimeError):
        async with locks.acquire(1):
            raise RuntimeError("boom")

    assert len(locks) == 0
    # And the key is reusable — the lock was released, not left held.
    async with locks.acquire(1):
        pass
    assert len(locks) == 0
