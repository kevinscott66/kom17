"""Regression: ``-x db=<name> heads`` must report a non-empty revision
tree for every database.

Before R-FIX-001 the project relied on ``migrations/env.py`` to set
``version_locations`` at the start of every alembic run. Alembic
constructs ``ScriptDirectory`` *before* ``env.py`` is executed, so the
per-DB override never took effect for ``heads`` / ``current`` /
``history`` (which don't run ``env.py`` at all), and arrived too late
for ``upgrade`` / ``stamp`` (which had already snapshotted the empty
parent ``migrations/versions/`` directory). Symptom: every ``heads``
call printed nothing, ``upgrade head`` was a silent no-op, and
``stamp head`` corrupted baselines — forcing the T-013 deploy to
hand-craft SQL ``INSERT INTO alembic_version`` statements.

The fix is ``scripts/alembic_run.py``: a small wrapper that builds the
:class:`alembic.config.Config` in Python and sets ``version_locations``
*before* the command runs. This test guards both halves of that
contract:

* The wrapper exits 0 for every known DB.
* Its stdout includes the expected per-DB head revision id.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# (db, substring expected in `heads` output). The substrings match the
# baseline revision filenames under ``migrations/versions/<db>/``;
# refreshing them when a new head lands is intentional — that's the
# moment we want this test to fail loudly.
_EXPECTED_HEADS: tuple[tuple[str, str], ...] = (
    ("users", "0012_marriages_in_top_backfill"),
    ("economy", "0017_shop_price_rebalance"),
    ("activity", "0001_baseline_activity"),
    ("moderation", "0011_chat_scoped_indexes"),
    ("message_stats", "0002_chat_scoped_index"),
)


@pytest.mark.parametrize(("db", "expected_substr"), _EXPECTED_HEADS)
def test_alembic_run_heads_non_empty(db: str, expected_substr: str) -> None:
    """``python -m scripts.alembic_run -x db=<db> heads`` prints the
    head revision id. A regression that re-introduces the pre-fix bug
    (version_locations set inside env.py) would yield empty stdout and
    fail the substring assertion."""
    result = subprocess.run(  # noqa: S603 — args fully controlled below
        [
            sys.executable,
            "-m",
            "scripts.alembic_run",
            "-x",
            f"db={db}",
            "heads",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic_run exited {result.returncode}: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.stdout.strip(), (
        f"expected non-empty heads for db={db}; stdout was empty (stderr={result.stderr!r})"
    )
    assert expected_substr in result.stdout, (
        f"expected '{expected_substr}' in heads output for db={db}; got: {result.stdout!r}"
    )


def test_alembic_run_rejects_missing_db_argument() -> None:
    """Without ``-x db=<name>`` the wrapper must exit non-zero with a
    helpful error — silent fallback to the empty parent ``versions/``
    tree is exactly the bug R-FIX-001 fixed.
    """
    result = subprocess.run(  # noqa: S603 — args fully controlled below
        [sys.executable, "-m", "scripts.alembic_run", "heads"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "db=" in result.stderr or "db=" in result.stdout


def test_alembic_run_rejects_unknown_db() -> None:
    """A typo'd DB name must fail fast, not fall through to a
    silently-empty migration run."""
    result = subprocess.run(  # noqa: S603 — args fully controlled below
        [
            sys.executable,
            "-m",
            "scripts.alembic_run",
            "-x",
            "db=does_not_exist",
            "heads",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
