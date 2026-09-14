"""Per-DB Alembic wrapper that sets ``version_locations`` before any
``ScriptDirectory`` is constructed.

Why this exists
---------------
Our migration layout puts each SQLite database's revisions in its own
sub-tree under ``migrations/versions/<db>/``. Alembic's standard CLI
constructs ``ScriptDirectory.from_config(config)`` *before* invoking
``env.py``; once built, ``ScriptDirectory`` has already snapshotted
``version_locations``. Setting ``version_locations`` inside ``env.py``
(as the legacy ``migrations/env.py`` does) therefore has no effect on
metadata commands like ``heads`` / ``current`` / ``history`` (which
never run ``env.py``), and arrives too late even for ``upgrade`` /
``stamp`` (where ``ScriptDirectory`` was already built around the
default ``script_location/versions/`` path, which contains only the
per-DB sub-directories — no ``.py`` files — so the script tree is
empty and the command silently no-ops).

This wrapper:

1. Parses ``-x db=<name>`` from the command line.
2. Builds an :class:`alembic.config.Config` against ``alembic.ini``.
3. Calls ``config.set_main_option("version_locations", ...)`` pointing
   at ``migrations/versions/<db>/`` **before** dispatching to the
   alembic command, so the freshly-built ``ScriptDirectory`` sees the
   correct directory.
4. Forwards every other arg to :mod:`alembic.config.CommandLine` so the
   full CLI surface (``upgrade``, ``downgrade``, ``revision``,
   ``stamp``, ``heads``, ``current``, ``history``, …) works unchanged.

Usage::

    python -m scripts.alembic_run -x db=users heads
    python -m scripts.alembic_run -x db=economy upgrade head
    python -m scripts.alembic_run -x db=users stamp head

Plain ``alembic`` invocations still work for migrations triggered by
``env.py`` after ``ScriptDirectory`` is loaded, but the ``heads`` /
``current`` / ``history`` commands will report empty for the per-DB
layout. Prefer this wrapper everywhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from alembic.config import Config

if TYPE_CHECKING:
    from collections.abc import Sequence


# Mirror :class:`telegram_invite_bot.db.names.DBName` without importing
# it — we want the wrapper to remain runnable even when the package
# isn't importable (e.g. fresh checkout, before ``pip install -e .``).
_KNOWN_DBS: frozenset[str] = frozenset(
    {"users", "economy", "activity", "moderation", "message_stats"}
)

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_ALEMBIC_INI: Path = _REPO_ROOT / "alembic.ini"
_VERSIONS_ROOT: Path = _REPO_ROOT / "migrations" / "versions"


def _extract_db_from_x_args(argv: Sequence[str]) -> str:
    """Return the value of ``-x db=<name>`` (or raise ``SystemExit``).

    We don't consume the argument — :class:`alembic.config.CommandLine`
    still needs to see it so ``env.py``'s ``context.get_x_argument``
    keeps working (used by tests that want to assert the chosen DB).
    """
    db: str | None = None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-x", action="append", default=[])
    known, _rest = parser.parse_known_args(list(argv))
    for kv in known.x:
        if not isinstance(kv, str) or "=" not in kv:
            continue
        key, _, value = kv.partition("=")
        if key.strip() == "db":
            db = value.strip()
            break
    if db is None:
        sys.stderr.write(
            "alembic_run: missing required '-x db=<name>' argument; "
            f"known DBs: {sorted(_KNOWN_DBS)}\n"
        )
        raise SystemExit(2)
    if db not in _KNOWN_DBS:
        sys.stderr.write(
            f"alembic_run: unknown db '{db}'; known: {sorted(_KNOWN_DBS)}\n"
        )
        raise SystemExit(2)
    return db


def _build_config(db: str) -> Config:
    """Return a Config with ``version_locations`` already pointed at the
    per-DB directory. Mirrors ``alembic`` CLI startup but injects the
    override *before* any ``ScriptDirectory`` is built.
    """
    versions_dir = _VERSIONS_ROOT / db
    versions_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(str(_ALEMBIC_INI))
    # Critical: set BEFORE any command runs. ``ScriptDirectory.from_config``
    # snapshots this value at construction; setting it later (e.g. inside
    # ``env.py``) is a no-op for ``heads`` / ``current`` and arrives too
    # late for ``upgrade`` / ``stamp``.
    cfg.set_main_option("version_locations", str(versions_dir))
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    db = _extract_db_from_x_args(args)
    cfg = _build_config(db)

    # Re-use alembic's own CommandLine to keep parity with ``alembic``
    # CLI: all sub-commands, --autogenerate, etc.
    from alembic.config import CommandLine

    cl = CommandLine(prog="alembic_run")
    options = cl.parser.parse_args(args)
    if not hasattr(options, "cmd"):
        cl.parser.error("too few arguments")  # mimics alembic's own check

    # Propagate -x args onto the Config we built (CommandLine usually
    # does this when it constructs its own Config; we built ours
    # manually to pre-set version_locations, so we wire -x ourselves).
    cfg.cmd_opts = options
    cl.run_cmd(cfg, options)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
