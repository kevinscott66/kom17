"""Destructive-write guard: blocks in prod, warns in dev, honors bypass.

The second half of the file is #1494: what the guard does with a
statement that hides its head behind a comment, with a ``WHERE`` that
belongs to somebody else, and with a head that is destructive without
being ``DELETE``/``UPDATE`` at all. None of those are reachable from a
Telegram user — parameters reach the driver as ``?`` — so these tests
guard future code rather than a live hole.
"""

from __future__ import annotations

import pytest
from loguru import logger

from telegram_invite_bot.config.settings import AppEnv
from telegram_invite_bot.db.safety import (
    UnboundedWriteError,
    allow_unbounded_writes,
    install,
)


def _invoke(listener, sql: str) -> None:  # type: ignore[no-untyped-def]
    listener(None, None, sql, None, None, False)


def test_prod_raises_on_unbounded_delete() -> None:
    listener = install(AppEnv.PROD)
    with pytest.raises(UnboundedWriteError):
        _invoke(listener, "DELETE FROM pending_notifications")


def test_prod_raises_on_unbounded_update() -> None:
    listener = install(AppEnv.PROD)
    with pytest.raises(UnboundedWriteError):
        _invoke(listener, "UPDATE users SET balance = 0")


def test_prod_allows_delete_with_where() -> None:
    listener = install(AppEnv.PROD)
    _invoke(listener, "DELETE FROM users WHERE id = 1")


def test_prod_allows_update_with_where() -> None:
    listener = install(AppEnv.PROD)
    _invoke(listener, "UPDATE users SET balance = 0 WHERE id = 1")


def test_dev_warns_but_does_not_raise(caplog) -> None:  # type: ignore[no-untyped-def]
    listener = install(AppEnv.DEV)
    # No exception — dev tests need to wipe tables.
    _invoke(listener, "DELETE FROM pending_notifications")


def test_bypass_context_allows_in_prod() -> None:
    listener = install(AppEnv.PROD)
    with allow_unbounded_writes():
        _invoke(listener, "DELETE FROM pending_notifications")


def test_select_is_ignored() -> None:
    listener = install(AppEnv.PROD)
    _invoke(listener, "SELECT * FROM users")


def test_insert_is_ignored() -> None:
    listener = install(AppEnv.PROD)
    _invoke(listener, "INSERT INTO users (id) VALUES (1)")


def test_multiline_unbounded_update_still_blocked() -> None:
    listener = install(AppEnv.PROD)
    sql = "UPDATE\n  users\nSET\n  balance = 0"
    with pytest.raises(UnboundedWriteError):
        _invoke(listener, sql)


def test_a_where_that_belongs_to_somebody_else_no_longer_counts() -> None:
    """#1045 recorded this as an accepted hole; #1494 closed it.

    The old search ran over the whole statement, so the word counted
    wherever it landed — inside a string literal, inside a quoted
    identifier, inside a subquery. All three statements below update
    every row of the table, and all three used to pass. The search now
    runs over the top-level projection, where a quoted span and a
    bracketed span are both blanked out.
    """
    listener = install(AppEnv.PROD)

    with pytest.raises(UnboundedWriteError):
        _invoke(listener, "UPDATE users SET note = 'where you are'")
    with pytest.raises(UnboundedWriteError):
        _invoke(listener, 'UPDATE "where" SET a=1')
    with pytest.raises(UnboundedWriteError):
        # The ORM emits this shape unaided, from a correlated subquery
        # in ``.values()`` — the call site looks harmless (#1653).
        _invoke(listener, "UPDATE users SET balance = (SELECT 0 FROM t WHERE t.id = 1)")


def test_a_real_where_still_passes_even_next_to_brackets_and_quotes() -> None:
    """The other half of the trade: a false positive raises in prod and
    takes a legitimate write down, so the shapes most likely to confuse
    a depth counter get their own pins.
    """
    listener = install(AppEnv.PROD)

    _invoke(listener, "UPDATE users SET a=1 WHERE id=2")
    # An unbalanced bracket inside a literal must not swallow the WHERE.
    _invoke(listener, "UPDATE t SET note = 'smile :)' WHERE id = 1")
    # A subquery on the bounded side is ordinary and very common.
    _invoke(listener, "DELETE FROM users WHERE id IN (SELECT id FROM stale)")
    _invoke(listener, "UPDATE users SET balance = (SELECT 1) WHERE id = 2")
    # Malformed SQL never reaches SQLite intact, but the guard reads the
    # text first: an unbalanced bracket must degrade to the old
    # behaviour, not turn a syntax error into a data-safety raise.
    _invoke(listener, "UPDATE t SET a = 1) WHERE id = 1")


def test_a_leading_comment_is_not_a_way_past_the_guard() -> None:
    """A BOM or a leading comment changes nothing about what SQLite
    does, which is exactly why it must not change what the guard does.
    The head is only the head after normalisation.
    """
    listener = install(AppEnv.PROD)

    for sql in (
        "-- cleanup\nDELETE FROM users",
        "/* nightly */ DELETE FROM users",
        "\ufeffDELETE FROM users",
        "/* a */ -- b\n\t UPDATE users SET balance = 0",
    ):
        with pytest.raises(UnboundedWriteError):
            _invoke(listener, sql)


def test_a_destructive_head_that_is_not_delete_or_update_is_only_logged() -> None:
    """#1494's second channel. These heads are destructive by shape, but
    the set is not sharp enough to raise on — an ordinary
    ``WITH ... SELECT`` report has the same head as ``WITH ... DELETE``
    — so prod gets a journal line and the statement goes through.
    """
    listener = install(AppEnv.PROD)
    records: list[str] = []
    handler_id = logger.add(records.append, level="WARNING", format="{message}")
    try:
        for sql in (
            "WITH doomed AS (SELECT id FROM users) DELETE FROM users",
            "REPLACE INTO users (id, balance) VALUES (1, 0)",
            "INSERT OR REPLACE INTO users (id) VALUES (1)",
            "DROP TABLE users",
            "TRUNCATE users",
        ):
            _invoke(listener, sql)
    finally:
        logger.remove(handler_id)

    assert len(records) == 5
    assert all("suspicious statement shape" in line for line in records)


def test_an_ordinary_statement_says_nothing() -> None:
    """The advisory line has to stay rare enough to be worth reading."""
    listener = install(AppEnv.PROD)
    records: list[str] = []
    handler_id = logger.add(records.append, level="WARNING", format="{message}")
    try:
        _invoke(listener, "SELECT * FROM users")
        _invoke(listener, "INSERT INTO users (id) VALUES (1)")
        _invoke(listener, "UPDATE users SET a=1 WHERE id=2")
    finally:
        logger.remove(handler_id)

    assert records == []


# #1979 — the same principle as the leading-comment test above, applied
# to the rest of the statement. #1494 wrote the rule down ("a comment is
# not a defence and must not be able to read like one") and enforced it
# only at position zero.


def test_a_comment_in_the_body_is_not_a_way_past_the_guard() -> None:
    """A comment anywhere hides the head or fakes a bound.

    The first shape is the one that matters: commenting a ``WHERE`` out
    is how an unbounded write gets written in the first place, and the
    guard exists for exactly that statement. The rest are the same
    cause seen from other angles — a note that merely contains the
    word, and a comment splitting the head.
    """
    listener = install(AppEnv.PROD)

    for sql in (
        "DELETE FROM users -- WHERE id = 1",
        "DELETE FROM message_counts\n-- where omitted on purpose: full wipe",
        "UPDATE users SET balance = 0 /* where id = 1 */",
        "DELETE /* oops */ FROM users",
        "UPDATE\n-- note\nusers SET balance = 0",
    ):
        with pytest.raises(UnboundedWriteError):
            _invoke(listener, sql)


def test_an_apostrophe_in_a_comment_does_not_take_a_bounded_write_down() -> None:
    """The other half of #1979, and the expensive half.

    ``it's`` opens a literal that only closes at the next quote in the
    SQL — several tokens later, on the far side of the real ``WHERE``.
    Blanking that span removed the clause, and a bounded DELETE became
    an :class:`UnboundedWriteError` in prod: a comment took a correct
    write down.
    """
    listener = install(AppEnv.PROD)

    _invoke(listener, "DELETE FROM sessions -- it's fine\nWHERE user_id = '5'")
    _invoke(listener, "UPDATE users SET nick = 'x' /* don't ask */ WHERE user_id = 5")


def test_a_comment_marker_inside_a_literal_is_still_data() -> None:
    """Symmetry check, and the reason this is a scanner and not two
    substitutions: ``--`` inside a string starts no comment, so the
    ``WHERE`` after it is real and the write is bounded. The second
    statement has no clause at all and must still be refused — the
    literal must not become a bound either.
    """
    listener = install(AppEnv.PROD)

    _invoke(listener, "UPDATE users SET note = '-- where' WHERE user_id = 1")
    with pytest.raises(UnboundedWriteError):
        _invoke(listener, "UPDATE users SET note = '/* where */'")


def test_a_comment_does_not_quiet_the_advisory_channel() -> None:
    """The same blind spot in #1494's other half.

    These heads never raise — the set is not sharp enough — so the
    journal line is the only trace they leave. A comment splitting the
    head took even that away: ``DROP /* x */ TABLE users`` matched
    nothing and was reported ordinary, which is the one failure mode a
    log-only channel cannot afford.
    """
    listener = install(AppEnv.PROD)
    records: list[str] = []
    handler_id = logger.add(records.append, level="WARNING", format="{message}")
    try:
        for sql in (
            "DROP /* just tidying up */ TABLE users",
            "REPLACE\n-- see ticket\nINTO users (id) VALUES (1)",
        ):
            _invoke(listener, sql)
    finally:
        logger.remove(handler_id)

    assert len(records) == 2
    assert all("suspicious statement shape" in line for line in records)
