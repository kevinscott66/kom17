"""The /transfer_rights cooldown map — the limit AND the table behind it.

Two properties have to hold together, and the obvious fix for either one
breaks the other:

* the 24h per-group window must keep refusing a second transfer, and
* the map holding those windows must not accumulate one float per group
  ever transferred (the #73 growth class).

Dropping the entry on read only reclaims groups somebody asks about
again, which is why the sweep exists; an LRU cap would bound the table
just as well but would hand back the early transfer the window exists to
refuse, which is why there isn't one. Both halves are pinned here.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers import transfer_rights as tr

DAY = tr.TRANSFER_COOLDOWN_SECONDS


@pytest.fixture(autouse=True)
def _clean_cooldowns() -> None:
    tr.clear_transfer_cooldowns()


def test_a_transferred_group_is_refused_for_a_day() -> None:
    tr._mark_transferred(-100, now=0.0)

    assert tr._on_cooldown(-100, now=0.0) is True
    assert tr._on_cooldown(-100, now=DAY - 1) is True
    assert tr._on_cooldown(-100, now=DAY) is False


def test_the_window_is_per_group() -> None:
    tr._mark_transferred(-100, now=0.0)

    assert tr._on_cooldown(-200, now=0.0) is False


def test_expired_windows_are_reclaimed_without_being_asked_about() -> None:
    """The leak: nobody ever queries these groups again.

    Each one is transferred once and forgotten, so the read-path drop
    never fires for it. Only the sweep on insert can free them.
    """
    for gid in range(tr._SWEEP_WATERMARK_MIN):
        tr._mark_transferred(-gid, now=0.0)
    assert len(tr._COOLDOWNS) == tr._SWEEP_WATERMARK_MIN

    tr._mark_transferred(-999_999, now=DAY + 1)

    assert len(tr._COOLDOWNS) == 1


def test_the_sweep_spares_windows_that_are_still_running() -> None:
    """A sweep that ran on total size rather than expiry would be a
    bypass — the group it dropped could transfer again the same hour."""
    for gid in range(tr._SWEEP_WATERMARK_MIN):
        tr._mark_transferred(-gid, now=0.0)

    tr._mark_transferred(-999_999, now=60.0)

    assert len(tr._COOLDOWNS) == tr._SWEEP_WATERMARK_MIN + 1
    assert tr._on_cooldown(-0, now=60.0) is True
    assert tr._on_cooldown(-42, now=60.0) is True


def test_the_watermark_follows_the_live_set() -> None:
    """Otherwise the sweep re-runs on every insert once the map is big
    and nothing in it has expired — O(n) per call forever."""
    for gid in range(tr._SWEEP_WATERMARK_MIN):
        tr._mark_transferred(-gid, now=0.0)

    tr._mark_transferred(-999_999, now=60.0)  # sweeps, frees nothing

    # Doubled off the surviving set (measured before this insert landed),
    # so the table has room to grow again before the next scan.
    assert tr._COOLDOWNS.sweep_at == 2 * tr._SWEEP_WATERMARK_MIN
    assert tr._COOLDOWNS.sweep_at > len(tr._COOLDOWNS)


def test_clearing_resets_the_watermark_too() -> None:
    for gid in range(tr._SWEEP_WATERMARK_MIN):
        tr._mark_transferred(-gid, now=0.0)
    tr._mark_transferred(-999_999, now=60.0)
    assert tr._COOLDOWNS.sweep_at > tr._SWEEP_WATERMARK_MIN

    tr.clear_transfer_cooldowns()

    assert tr._COOLDOWNS.sweep_at == tr._SWEEP_WATERMARK_MIN
    assert len(tr._COOLDOWNS) == 0
