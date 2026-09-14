"""The single-worker tripwire (#1990).

``_assert_single_worker`` is the only thing standing between this
deployment and a class of bug that leaves no trace: fork the process and
the per-match game locks, the marriage locks and the ``seen_updates``
dedup cache each become one-per-worker, so two workers happily grant the
same reward twice. Nothing crashes; the ledger just quietly disagrees
with itself.

These pin the three branches of the tripwire itself. The wiring — that
it fires from ``create_app`` rather than from the runner, which is the
whole point of #1990 — is pinned in
``tests/integration/webhook/test_server.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from telegram_invite_bot.webhook.server import _assert_single_worker


def _capture(level: str = "WARNING") -> tuple[list[str], int]:
    lines: list[str] = []

    def sink(message: Any) -> None:  # noqa: ANN401 - loguru's Message
        lines.append(str(message))

    return lines, logger.add(sink, level=level)


def _run(monkeypatch: pytest.MonkeyPatch, value: str | None) -> list[str]:
    if value is None:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", value)
    lines, sink_id = _capture()
    try:
        _assert_single_worker()
    finally:
        logger.remove(sink_id)
    return lines


def test_the_default_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No variable at all is the normal deployment — say nothing.

    A tripwire that speaks on every boot gets filtered out of the
    journal, and then it is not a tripwire.
    """
    assert _run(monkeypatch, None) == []
    assert _run(monkeypatch, "1") == []


def test_more_than_one_worker_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = _run(monkeypatch, "4")

    assert len(lines) == 1, lines
    assert "ERROR" in lines[0]
    assert "WEB_CONCURRENCY=4" in lines[0]


def test_a_garbage_value_warns_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``int()`` on operator input must not take the process down.

    This runs inside ``create_app``, i.e. before the server binds; an
    unhandled ``ValueError`` here would turn a typo in a unit file into
    a boot loop that says nothing about its cause.
    """
    lines = _run(monkeypatch, "two")

    assert len(lines) == 1, lines
    assert "not an int" in lines[0]
    assert "ERROR" not in lines[0]
