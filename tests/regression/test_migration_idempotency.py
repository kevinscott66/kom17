"""Regression guard: a money-converting migration must survive a re-run.

``economy/0003_withdrawal_amount_int`` rewrites every stored payout from
major units (a REAL number of roubles) to minor units (an INTEGER number
of kopecks) with a bare ``UPDATE ... SET amount_fiat = amount_fiat *
100``. The statement carries no marker of having run: replay it and the
same rows are multiplied by a hundred a second time. A 1 234.56 ₽
withdrawal becomes 123 456 ₽.

That is not a hypothetical. Alembic aborts a revision on the first error
and leaves ``alembic_version`` at the PREVIOUS revision, so a partial
failure anywhere later in the batch invites exactly one re-run of the
whole thing. A disaster-recovery restore that stamps a stale version, or
an operator who downgrades and upgrades to test a rollback, does the
same. Every sibling economy revision (``0004``, ``0009``, ``0010``,
``0012``…``0016``) already inspects before it acts; this one did not.

The guard runs the real revision functions against a real SQLite file
rather than reading the source, because the property under test is what
the SQL *does* to the rows, not how it is spelled.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

if TYPE_CHECKING:
    from collections.abc import Iterator

_REVISION = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "economy"
    / "0003_withdrawal_amount_int.py"
)

# One legacy row, in the shape legacy actually wrote: a 2dp REAL.
_LEGACY_AMOUNT = 1234.56
_MINOR_UNITS = 123456


def _load_revision() -> Any:
    """Import the revision by path — its filename starts with a digit."""
    spec = importlib.util.spec_from_file_location("_rev_0003", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """A SQLite file carrying the legacy REAL column and one real row."""
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'economy.db'}")
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE withdrawal_requests ("
                "id INTEGER PRIMARY KEY, user_id INTEGER, amount_fiat REAL)"
            )
        )
        conn.execute(
            sa.text("INSERT INTO withdrawal_requests (id, user_id, amount_fiat) VALUES (1, 7, :a)"),
            {"a": _LEGACY_AMOUNT},
        )
    yield eng
    eng.dispose()


def _run(engine: sa.Engine, name: str) -> None:
    """Execute ``upgrade``/``downgrade`` with the ``op`` proxy bound."""
    module = _load_revision()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            getattr(module, name)()


def _amount(engine: sa.Engine) -> float:
    with engine.connect() as conn:
        value = conn.execute(sa.text("SELECT amount_fiat FROM withdrawal_requests")).scalar_one()
    assert isinstance(value, (int, float))
    return value


def _column_type(engine: sa.Engine) -> sa.types.TypeEngine[Any]:
    (column,) = [
        col
        for col in sa.inspect(engine).get_columns("withdrawal_requests")
        if col["name"] == "amount_fiat"
    ]
    return column["type"]


def test_upgrade_converts_the_legacy_float_once(engine: sa.Engine) -> None:
    """The happy path stays exactly as it was — 1 234.56 ₽ -> 123 456 kopecks."""
    _run(engine, "upgrade")

    assert _amount(engine) == _MINOR_UNITS
    assert isinstance(_column_type(engine), sa.Integer)


def test_a_second_upgrade_does_not_multiply_the_money_again(engine: sa.Engine) -> None:
    """The replay. Without a guard the row becomes 12 345 600 kopecks.

    Asserted on the row rather than on the column type: the type change
    is idempotent on its own (``alter_column`` to the type it already
    has is a harmless rebuild), and it is the unguarded data ``UPDATE``
    in front of it that destroys the money.
    """
    _run(engine, "upgrade")
    _run(engine, "upgrade")

    assert _amount(engine) == _MINOR_UNITS


def test_a_second_downgrade_does_not_divide_the_money_again(engine: sa.Engine) -> None:
    """The mirror hole: replaying the inverse turns 1 234.56 into 12.3456."""
    _run(engine, "upgrade")
    _run(engine, "downgrade")
    assert _amount(engine) == pytest.approx(_LEGACY_AMOUNT)

    _run(engine, "downgrade")

    assert _amount(engine) == pytest.approx(_LEGACY_AMOUNT)


def test_upgrade_is_a_no_op_when_the_table_is_absent(tmp_path: Path) -> None:
    """A fresh disaster-recovery file has no legacy table to convert.

    The sibling revisions all return early in that case rather than
    dying on ``no such table``, which would strand ``alembic_version``
    at the previous revision and silently skip everything after it.
    """
    eng = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / 'empty.db'}")
    try:
        _run(eng, "upgrade")
    finally:
        eng.dispose()
