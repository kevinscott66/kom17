"""``unix_ts`` — the aware-datetime guard on the grant-expiry readers.

The bug this pins: ``datetime.timestamp()`` on a *naive* value reads the
wall clock in the HOST's local zone. Production runs Europe/Moscow, so a
naive-UTC ``now`` (``db_now()``) produced an epoch 10 800 s in the past
and an expired xp_boost / VIP grant kept paying out for three more hours.

These tests deliberately force a non-UTC local zone via ``TZ`` so the
naive-vs-aware difference is observable on a UTC CI box too.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from loguru import logger

from telegram_invite_bot.utils.time import db_now, unix_ts


@pytest.fixture
def moscow_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the process' local zone to MSK, like the production host.

    ``time.tzset`` is what makes ``datetime.timestamp()`` on a naive
    value pick up the change; without it ``TZ`` is inert.
    """
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def _capture() -> tuple[list[str], int]:
    records: list[str] = []

    def sink(message: Any) -> None:  # noqa: ANN401
        records.append(str(message))

    return records, logger.add(sink, level="ERROR", format="{message}")


def test_aware_input_is_passed_through_unchanged(moscow_tz: None) -> None:
    """An aware datetime is already unambiguous — no coercion, no log."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    records, handler_id = _capture()
    try:
        assert unix_ts(now, where="test") == now.timestamp()
    finally:
        logger.remove(handler_id)
    assert records == []


def test_naive_input_is_coerced_to_utc_not_local(moscow_tz: None) -> None:
    """The whole point: a naive value must NOT be read as local time.

    Without the guard this returns ``now.timestamp()``, which on MSK is
    10 800 s *behind* the correct epoch — the three free hours the bug
    handed to every expired grant.
    """
    now = datetime(2026, 8, 29, 12, 0)  # noqa: DTZ001 - naive on purpose
    records, handler_id = _capture()
    try:
        guarded = unix_ts(now, where="PrivilegesRepo.get_active")
    finally:
        logger.remove(handler_id)

    assert guarded == now.replace(tzinfo=UTC).timestamp()
    assert guarded - now.timestamp() == 3 * 3600.0
    assert len(records) == 1, records
    assert "PrivilegesRepo.get_active" in records[0]


def test_db_now_through_the_guard_matches_wall_clock(moscow_tz: None) -> None:
    """``db_now()`` is the exact value that used to leak three hours."""
    naive = db_now()
    assert naive.timestamp() - time.time() == pytest.approx(-3 * 3600.0, abs=2.0)

    records, handler_id = _capture()
    try:
        assert unix_ts(naive, where="x") - time.time() == pytest.approx(0.0, abs=2.0)
    finally:
        logger.remove(handler_id)
    assert len(records) == 1


def test_guarded_deadline_does_not_extend_a_lapsed_grant(moscow_tz: None) -> None:
    """A grant that expired one second ago reads as expired, not active."""
    aware = datetime.now(UTC)
    expires_at = (aware - timedelta(seconds=1)).timestamp()

    records, handler_id = _capture()
    try:
        naive_deadline = unix_ts(aware.replace(tzinfo=None), where="x")
    finally:
        logger.remove(handler_id)

    assert expires_at <= naive_deadline  # expired, as it should be
    # Unguarded, the same naive value would have kept it "active".
    assert expires_at > aware.replace(tzinfo=None).timestamp()
