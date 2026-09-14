"""Regression guard: adopted legacy tables are never dropped on downgrade.

Most revisions create the table they later drop, so their ``downgrade``
is a faithful inverse and losing the rows is what a downgrade *means*.
A handful of revisions are different: the table already existed on prod,
created years earlier by the legacy telebot and filled with real rows,
and the revision only *adopts* it — guaranteeing a shape, creating what
is absent. For those, ``upgrade`` never established the table, so a
``downgrade`` that drops it destroys history no ``upgrade`` can rebuild.
Backing a bad deploy out with ``alembic downgrade -1`` would be
unrecoverable.

The rule for an adopting revision is therefore: ``downgrade`` is a no-op
carrying a docstring that says why. On a fresh dev database that leaves
an orphan table behind, which is the cheaper mistake — it costs a stale
table, not the data.

This guard is deliberately an explicit list rather than a blanket ban on
``op.drop_table`` in a ``downgrade``: ``0010_p2p`` legitimately drops
``p2p_trades`` and ``p2p_sell_orders`` as the middle step of a
copy-drop-rename rebuild that preserves every row, and a blanket rule
would have to special-case it anyway.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"

# revision path -> why the tables it touches cannot be dropped.
ADOPTING_REVISIONS: dict[str, str] = {
    "economy/0012_pvp_stake_games.py": (
        "pvp_offers/pvp_escrow hold 80 + 93 legacy rows from three players"
    ),
    "users/0007_user_group_joins.py": (
        "user_group_joins has been collecting memberships since March 2026"
    ),
    # #1937: three revisions were violating the doctrine this file
    # states, and were simply not on the list. Each one's own module
    # docstring already said the objects predate it.
    "users/0004_bond_activity_log.py": (
        "marriage_activity_log / relationship_activity_log are legacy tables "
        "(bot.py:5568/:5583, docs/prod_schemas.sql:107/:119)"
    ),
    "users/0005_voice_transcription.py": (
        "voice_transcriptions holds the STT history since bot.py:5899, and the six "
        "group_settings columns are read and written by the LIVE legacy process"
    ),
    # #1974: three more with the same shape, found by re-reading every
    # chain end to end rather than only the revisions #1937 named. Two of
    # them sit on either side of the pair #1937 fixed, so a downgrade of
    # the users tree from head runs them anyway.
    "users/0006_rp_18_gate.py": (
        "group_settings.rp_18_enabled / rp_18_prompt_sent are legacy columns "
        "(bot.py:5854-5861, docs/prod_schemas.sql:236) the LIVE process reads "
        "on every RP action"
    ),
    "users/0010_rp_vip_outside.py": (
        "group_settings.rp_vip_outside_enabled is a legacy column carrying each "
        "group's choice about a PAID perk (bot.py:5858-5859, "
        "docs/prod_schemas.sql:236)"
    ),
    "economy/0013_display_currency.py": (
        "economy.users.display_currency is created by legacy at startup "
        "(bot.py:5306, docs/prod_schemas.sql:320) and holds every user's chosen "
        "display currency"
    ),
}

# Revisions that adopt one object and genuinely own another, so their
# ``downgrade`` cannot be a no-op — it must simply never destroy the
# adopted half. Value is the set of object names it may not touch.
PARTIALLY_ADOPTING_REVISIONS: dict[str, tuple[str, ...]] = {
    "economy/0009_donations_rating_writeside.py": (
        "rating_history",
        "idx_rating_history_date",
    ),
    # #1974: same shape, milder cost — the revision owns the UNIQUE
    # double-claim guard and adopts legacy's lookup index. The AST check
    # below cannot actually see this one: it drops through a loop
    # variable, not a literal, so the assertion holds vacuously and only
    # catches a future literal. What really pins it is the functional
    # test at the end of this file, which runs the downgrade.
    "economy/0004_check_claims_unique.py": ("idx_check_claims_check",),
}


def _downgrade(path: Path) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            return node
    pytest.fail(f"{path}: no downgrade() found")


@pytest.mark.parametrize(("relative", "reason"), sorted(ADOPTING_REVISIONS.items()))
def test_adopting_revision_downgrade_is_a_documented_no_op(relative: str, reason: str) -> None:
    path = _MIGRATIONS / relative
    assert path.is_file(), f"{relative} moved or was renamed — update this guard"

    func = _downgrade(path)
    body = func.body

    assert ast.get_docstring(func), (
        f"{relative}: downgrade() must carry a docstring explaining the no-op ({reason})"
    )
    assert len(body) == 1, (
        f"{relative}: downgrade() must be a no-op — it currently runs "
        f"{len(body) - 1} statement(s) after its docstring, and {reason}"
    )


@pytest.mark.parametrize(("relative", "protected"), sorted(PARTIALLY_ADOPTING_REVISIONS.items()))
def test_partially_adopting_revision_never_drops_the_adopted_half(
    relative: str, protected: tuple[str, ...]
) -> None:
    """#1937: a revision that owns one object and adopts another.

    ``economy/0009`` adds ``groups_donations.in_rating`` (ours, dropping
    it back is a faithful inverse) and adopts ``rating_history``, which
    legacy created and prod carries filled. A no-op ``downgrade`` would
    be wrong here — it would strand our own column — so the rule is
    narrower: the adopted names must not appear in any ``drop_*`` call.

    The original code was worse than an unguarded drop: it inspected the
    table and dropped it *because* it existed, so the guard guaranteed
    the destruction rather than preventing it. An AST check on the
    ``drop_*`` arguments catches exactly that shape, whatever the
    surrounding ``if`` claims to do.
    """
    path = _MIGRATIONS / relative
    assert path.is_file(), f"{relative} moved or was renamed — update this guard"

    dropped: list[str] = []
    for node in ast.walk(_downgrade(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if not name.startswith("drop_"):
            continue
        dropped += [
            arg.value
            for arg in [*node.args, *(kw.value for kw in node.keywords)]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]

    offending = sorted(set(dropped) & set(protected))
    assert not offending, (
        f"{relative}: downgrade() drops {offending}, which this revision only "
        f"adopted — the rows predate it and no upgrade can rebuild them"
    )


def _load_revision(relative: str) -> ModuleType:
    """Import a revision module by path, without going through Alembic."""
    path = _MIGRATIONS / relative
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    assert spec and spec.loader, f"{relative}: cannot be imported"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_claims_downgrade_keeps_the_legacy_lookup_index() -> None:
    """#1974: ``economy/0004`` drops its own index and adopts legacy's.

    The AST guard above is blind here — the revision iterates a tuple
    and drops through a variable — so this runs the real ``downgrade``
    against a SQLite database built in legacy's shape: the table plus
    ``idx_check_claims_check``, which ``docs/prod_schemas.sql:502`` shows
    on prod and which ``upgrade`` skips for that reason.

    The old body dropped whatever it found and so took the legacy index
    with it. An index carries no rows, so the cost is bounded — but
    until the next ``upgrade`` the LIVE telebot scans ``check_claims``
    unindexed on every claim.
    """
    owned = "uq_check_claims_check_user"
    adopted = "idx_check_claims_check"

    # dispose() matters: the pooled sqlite connection is otherwise closed
    # by the garbage collector, and this suite turns ResourceWarning into
    # an error inside whichever unrelated test happens to be running.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE check_claims (id INTEGER PRIMARY KEY, check_id INT, user_id INT)"
            )
            conn.exec_driver_sql(f"CREATE INDEX {adopted} ON check_claims(check_id)")
            conn.exec_driver_sql(f"CREATE UNIQUE INDEX {owned} ON check_claims(check_id, user_id)")

            with Operations.context(MigrationContext.configure(conn)):
                _load_revision("economy/0004_check_claims_unique.py").downgrade()

            surviving = {ix["name"] for ix in sa.inspect(conn).get_indexes("check_claims")}
    finally:
        engine.dispose()

    assert adopted in surviving, (
        "downgrade() removed legacy's lookup index, which upgrade() never created"
    )
    assert owned not in surviving, (
        "downgrade() must still drop the UNIQUE guard this revision does own"
    )
