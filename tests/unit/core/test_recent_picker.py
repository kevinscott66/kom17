"""Unit tests for :mod:`core.recent_picker` (RR-6 #71/#72).

The picker is a cosmetic device, so the properties that matter are the
ones a user would notice as a bug: an immediate repeat, a pick from
outside the pool, or a pick that has become a fixed rotation (which is
just as obviously not-random as the repeats it replaced).
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.recent_picker import RecentPicker, _memory_for

_POOL = tuple(f"item-{i}" for i in range(7))


def test_empty_pool_raises() -> None:
    """A caller with no content has a bug; returning "" would surface as
    an empty message the Telegram API rejects anyway.
    """
    with pytest.raises(IndexError):
        RecentPicker().pick("k", ())


def test_pick_always_comes_from_the_pool() -> None:
    picker = RecentPicker()
    for _ in range(50):
        assert picker.pick("k", _POOL) in _POOL


def test_consecutive_picks_do_not_repeat() -> None:
    picker = RecentPicker()
    picks = [picker.pick("k", _POOL) for _ in range(30)]
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))


def test_keys_do_not_shrink_each_others_pools() -> None:
    """Two groups running ``/joke`` must not narrow each other — the
    history is per-key for exactly this reason.
    """
    picker = RecentPicker()
    first = picker.pick("chat-a", _POOL)
    # The same value must still be reachable for another key.
    assert any(picker.pick("chat-b", _POOL) == first for _ in range(60))


def test_pool_never_becomes_a_fixed_rotation() -> None:
    """At most half the pool is remembered, so the sequence stays random
    rather than turning into a deterministic cycle.
    """
    picker = RecentPicker()
    runs = ["".join(picker.pick("k", _POOL) for _ in range(len(_POOL))) for _ in range(8)]
    assert len(set(runs)) > 1


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, 0), (1, 0), (2, 0), (3, 1), (4, 2), (7, 3), (100, 50)],
)
def test_memory_leaves_at_least_two_candidates(size: int, expected: int) -> None:
    assert _memory_for(size) == expected


def test_two_entry_pool_still_alternates_freely() -> None:
    """With no memory possible the picker degrades to plain choice rather
    than to a forced A-B-A-B rotation.
    """
    picker = RecentPicker()
    picks = {picker.pick("k", ("a", "b")) for _ in range(40)}
    assert picks == {"a", "b"}


def test_a_shrinking_pool_does_not_wedge_the_ring() -> None:
    """A pool edited between deploys leaves a ring of the wrong length;
    it must be rebuilt, not trusted.
    """
    picker = RecentPicker()
    for _ in range(5):
        picker.pick("k", _POOL)
    for _ in range(5):
        assert picker.pick("k", _POOL[:3]) in _POOL[:3]


def test_key_table_is_bounded() -> None:
    """A bot in more chats than the cap must not hold a ring per chat
    forever — the least-recently-used one is dropped.
    """
    picker = RecentPicker(max_keys=4)
    for key in range(20):
        picker.pick(key, _POOL)
    assert len(picker._recent) == 4


def test_clear_forgets_history() -> None:
    picker = RecentPicker()
    picker.pick("k", _POOL)
    picker.clear()
    assert not picker._recent
