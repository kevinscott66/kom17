"""Runtime guard against unbounded DELETE/UPDATE statements.

Stage 2 of the strangler migration: legacy ``bot.py`` contains exactly
one such statement (``DELETE FROM pending_notifications`` at startup —
intentional table-wipe of stale entries). New code is required to use
explicit ``WHERE`` clauses, so any unguarded statement coming through
the SQLAlchemy engine in production is almost certainly a bug.

What it checks is the SHAPE of the statement text, not its semantics.
It is a regex over the SQL string and it can only ever be that: the
listener sees the statement after parameters have been bound out to
``?``, with no schema and no plan. "Unbounded" here means "no top-level
``WHERE`` in the text", which is a good proxy and not a proof.

Behaviour:

* ``APP_ENV=prod`` → raise :class:`UnboundedWriteError`. Fail-loud beats
  silent data loss.
* ``APP_ENV in (dev, staging)`` → log a warning and let the statement
  through. Tests need to wipe tables.
* Statements explicitly allow-listed via :func:`allow_unbounded_writes`
  (context manager) bypass the guard regardless of environment. It has
  no production caller: the only one in the tree is
  ``tests/unit/db/test_safety.py``. The legacy ``pending_notifications``
  wipe was named here as its user and is not one — ``bot.py`` runs that
  DELETE through the raw ``sqlite3`` helper ``execute_query``, which
  never reaches this listener at all (#1452). That cuts both ways: the
  escape hatch is a test affordance, and the one legacy statement this
  module was written for sits outside its reach by construction. The
  name was written here as ``allow`` (#1045); no such function has ever
  existed.

#1494 added a second, quieter channel. A handful of statement heads are
destructive by shape without being ``DELETE``/``UPDATE`` at all —
``REPLACE INTO``, ``INSERT OR REPLACE``, ``DROP TABLE``, ``TRUNCATE``,
and ``WITH`` (which can carry a ``DELETE`` behind the CTE). Those are
logged and let through in EVERY environment, prod included, because the
set is not sharp enough to raise on: an ordinary ``WITH ... SELECT``
reporting query has the same head as a ``WITH ... DELETE``, and taking
prod down over the difference is the worse trade. The line exists so
that the day one of these appears, it appears in the journal rather
than only in the data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from loguru import logger

from telegram_invite_bot.config.settings import AppEnv

log = logger.bind(component="db.safety")

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.engine.interfaces import ExecutionContext

# `\s` to tolerate newlines/tabs; case-insensitive at the call site.
_DESTRUCTIVE_RE = re.compile(
    r"^\s*(?P<verb>DELETE\s+FROM|UPDATE)\s+(?P<table>[\"'`]?\w+[\"'`]?)\s*",
    re.IGNORECASE,
)
_HAS_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)

# #1494. Everything the guard reads is anchored at the head of the
# statement, so anything sitting in front of the head hides it: a BOM, a
# leading ``--`` line, a ``/* */`` block. None of those change what
# SQLite does with the statement, which is exactly what makes them worth
# stripping — a comment is not a defence and must not be able to read
# like one. Input normalisation, not a second dialect: leading position
# only, nothing about the body.
_LEADING_NOISE_RE = re.compile(r"^(?:\ufeff|\s|--[^\n]*(?:\n|$)|/\*.*?\*/)+", re.DOTALL)

# Heads that are destructive by shape without being DELETE/UPDATE. This
# set LOGS and never raises, in prod as everywhere else — see the module
# docstring for why ``WITH`` in particular cannot be a raising rule.
_SUSPICIOUS_RE = re.compile(
    r"^(?:REPLACE\s+INTO|INSERT\s+OR\s+REPLACE|DROP\s+TABLE|TRUNCATE|WITH)\b",
    re.IGNORECASE,
)


def _blank_noise(sql: str) -> str:
    """Blank every quoted literal and every comment, in place.

    A quoted span is data, not structure: its brackets must not move
    :func:`_top_level`'s depth counter and its words must not count as
    keywords. A comment is neither, and #1979 is what it cost to learn
    that twice. This used to be two independent regexes — one blanking
    literals across the whole statement, one deleting comments at
    position zero only — and each was blind to the other:

    * ``DELETE FROM users -- WHERE id = 1`` kept a ``WHERE`` the
      database does not see, so the guard read a bound that was not
      there and let an unbounded DELETE through. Commenting a clause
      out is precisely how such a statement comes to be written, so the
      guard was blindest at its own subject. #1494 wrote the rule down
      — "a comment is not a defence and must not be able to read like
      one" — and enforced it at position zero only.
    * ``DELETE FROM sessions -- it's fine`` followed by a real clause
      went the other way: the apostrophe opened a literal that ran to
      the next quote in the SQL, blanking the real ``WHERE`` with it,
      and prod raised :class:`UnboundedWriteError` on a correct write.

    Both need one left-to-right scan that knows which construct it is
    inside, so this is that scan rather than a third regex. Length is
    preserved (spans become spaces) so offsets still line up with the
    statement text the operator is shown. An unterminated literal or
    ``/*`` blanks to the end: such a statement is already invalid SQL,
    and blanking is the conservative half of being wrong about it — it
    can only make the guard louder, never quieter.

    Backticks are deliberately not handled. SQLite accepts them as
    identifiers, but a quoted identifier spelled ``WHERE`` is the
    hand-written case #1045 weighed and accepted.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        if char in "'\"":
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == char:
                    # SQLite spells an escaped quote by doubling it.
                    if i + 1 < n and sql[i + 1] == char:
                        out.append("  ")
                        i += 2
                        continue
                    out.append(" ")
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        if sql.startswith("--", i):
            # The newline itself is left alone: the listener reports the
            # first line of the statement and must keep its shape.
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if sql.startswith("/*", i):
            out.append("  ")
            i += 2
            while i < n:
                if sql.startswith("*/", i):
                    out.append("  ")
                    i += 2
                    break
                out.append(" ")
                i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


class UnboundedWriteError(RuntimeError):
    """Raised when DELETE/UPDATE without WHERE hits the engine in prod."""


_BYPASS: ContextVar[bool] = ContextVar("db_safety_bypass", default=False)


@contextmanager
def allow_unbounded_writes() -> Iterator[None]:
    """Temporarily disable the guard.

    Use sparingly. This was written for the legacy ``pending_notifications``
    cleanup and does not in fact wrap it (see the module docstring, #1452),
    so today the only caller is the guard's own test. New code should write
    an explicit ``WHERE 1=1`` (and document why) rather than reach for this.
    """
    token = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(token)


def _strip_leading_noise(sql: str) -> str:
    """Drop a BOM and any leading comments so the head is the head.

    #1494. One substitution, anchored, alternation repeated — that is
    enough to peel ``/* a */ -- b\n  DELETE FROM users`` down to the
    ``DELETE``.

    Nothing is done to the body, and #1979 is the correction to what
    that used to claim next: "a comment in the middle of a statement
    hides nothing this module looks at". It hid two things — the head,
    when a comment sat between the verb and its table, and the bound,
    when a commented-out ``WHERE`` read like a real one. The body is now
    handled where it belongs, by :func:`_blank_noise`, and this function
    is left doing only what its name says: moving the head to position
    zero so the anchored ``match`` calls downstream have something to
    anchor to.
    """
    return _LEADING_NOISE_RE.sub("", sql, count=1)


def _top_level(sql: str) -> str:
    """Blank out literals, comments and everything inside brackets.

    #1494/#1653. ``WHERE`` only bounds the statement it belongs to, and
    the plain search cannot tell whose it is:

        UPDATE users SET balance = (SELECT 0 FROM t WHERE t.id = 1)

    updates every row, and the ORM emits it unaided out of an ordinary
    correlated subquery in ``.values()`` — nothing at that call site
    looks dangerous. Same for a literal: ``SET note = 'where you are'``
    (#1045). Blanking both leaves only the structure the outer statement
    actually owns.

    A depth counter, not a parser. Literals and comments go first
    (:func:`_blank_noise`, #1979) so their brackets cannot move it, and
    the counter clamps at zero so an unbalanced ``)`` degrades to the
    old behaviour instead of hiding the rest of the statement.
    """
    depth = 0
    out: list[str] = []
    for char in _blank_noise(sql):
        if char == "(":
            depth += 1
            out.append(" ")
        elif char == ")":
            depth = max(depth - 1, 0)
            out.append(" ")
        else:
            out.append(" " if depth else char)
    return "".join(out)


def _is_suspicious(sql: str) -> bool:
    """``True`` for a head that is destructive by shape but not a
    DELETE/UPDATE — see :data:`_SUSPICIOUS_RE`. Advisory only: the
    caller logs and lets it through, prod included.

    #1979: blanked first, for the same reason :func:`_is_unbounded` is.
    ``DROP /* x */ TABLE users`` matched nothing and so was reported
    ordinary. This channel only writes to the journal, which is exactly
    why it must not be the quieter of the two: a shape that raises
    nowhere has the log as its only trace.
    """
    return bool(_SUSPICIOUS_RE.match(_blank_noise(sql)))


def _is_unbounded(sql: str) -> bool:
    # #1979: the head is read off the blanked text too. A comment
    # between the verb and its table — ``DELETE /* x */ FROM users`` —
    # split the head, ``_DESTRUCTIVE_RE`` did not match, and a statement
    # that matches nothing is reported bounded. That is the guard's
    # quietest failure: no warning, no raise, nothing in the journal.
    # ``_top_level`` blanks again below rather than take this text; each
    # function stays answerable for its own input, and the statements
    # reaching this listener are one line of SQL, not a corpus.
    match = _DESTRUCTIVE_RE.match(_blank_noise(sql))
    if not match:
        return False
    # #1045: this used to claim `WHERE` "must appear *after* the table
    # identifier". It is not checked, here or anywhere — the search runs
    # over the whole statement, so the word counts wherever it lands.
    #
    # That makes the guard fail-OPEN, not fail-closed, on a statement
    # whose text embeds the word: ``UPDATE users SET note = 'where you
    # are'`` is unbounded and passes. Accepted rather than tightened,
    # because SQLAlchemy binds parameters — a user-supplied string
    # reaches the driver as ``?`` and never appears in ``statement`` —
    # so reaching this hole takes hand-written ``text()`` SQL with an
    # inlined literal, or a column quoted ``"where"``. Both are things a
    # reviewer sees.
    #
    # #1653: a THIRD way is not. The ORM emits it unaided, out of an
    # ordinary correlated subquery in ``.values()``:
    #     UPDATE users SET balance = (SELECT 0 FROM t WHERE t.id = 1)
    # The outer UPDATE is unbounded, the ``WHERE`` belongs to the
    # subquery, and the search finds it all the same. Nothing about
    # that call site looks dangerous, so no reviewer connects it to
    # this guard. Restricting the search to top-level bracket depth
    # would close it — a depth counter, not a parser; see #1494.
    #
    # The opposite error would be worse: a false positive
    # raises :class:`UnboundedWriteError` in prod and takes the write
    # down, and no regex short of a parser tells a literal from a clause.
    #
    # #1494 closed both of those after all, without becoming a parser:
    # the search now runs over :func:`_top_level`, which blanks quoted
    # spans and bracketed spans first. A statement whose only ``WHERE``
    # lives in a subquery or inside a string is unbounded and now says
    # so. The false-positive risk that argument was about is a
    # miscounted bracket, and the two shapes that could produce one —
    # a bracket inside a literal, an unbalanced ``)`` — are handled
    # there explicitly.
    return not _HAS_WHERE_RE.search(_top_level(sql))


def install(app_env: AppEnv) -> Any:
    """Return a SQLAlchemy ``before_cursor_execute`` listener.

    Caller wires it onto each engine in :mod:`telegram_invite_bot.db.engines`.
    Returns the callable so tests can invoke it directly without spinning
    up a real engine.
    """

    def _before_cursor_execute(
        _conn: Connection,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: ExecutionContext | None,
        _executemany: bool,
    ) -> None:
        if _BYPASS.get():
            return
        # #1494: normalise once, then ask both questions of the same
        # text — a leading comment must not be able to answer either.
        normalised = _strip_leading_noise(statement)
        if _is_suspicious(normalised):
            # Advisory, never fatal: see the module docstring.
            log.warning(f"suspicious statement shape: {normalised.strip().splitlines()[0][:200]}")
        if not _is_unbounded(normalised):
            return
        msg = f"unbounded write blocked: {normalised.strip().splitlines()[0][:200]}"
        # ``==`` not ``is``: pydantic-settings re-instantiates StrEnum
        # values on load, so identity comparison fails across boundaries.
        if app_env == AppEnv.PROD:
            raise UnboundedWriteError(msg)
        log.warning(msg)

    return _before_cursor_execute
