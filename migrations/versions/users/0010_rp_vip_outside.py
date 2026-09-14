"""rp vip-outside toggle: group_settings.rp_vip_outside_enabled (#270)

Revision ID: 0010_rp_vip_outside
Revises: 0009_bot_groups_is_active
Create Date: 2026-08-27

Registers the per-group "VIP may use relationship-only RP actions
outside a relationship" toggle (#270) in the new pipeline's Alembic
chain:

* ``group_settings.rp_vip_outside_enabled`` — default **1 (ON)**.

The default is the whole point and is deliberately unlike its two
neighbours in ``0006_rp_18_gate``. The 18+ gate ships OFF because it
opens content a group has not asked for; this one ships ON because it
is a perk somebody paid for. Legacy sold it switched on
(``bot.py:5859`` ``... INTEGER DEFAULT 1``, echoed by the settings
loader defaults at ``bot.py:7798`` and ``bot.py:7827``) and let a group
admin turn it off. A ``server_default`` of ``"0"`` here would revoke a
paid feature for every group that has never touched the setting.

PROD ALREADY HAS THE COLUMN. Legacy creates it via ``ALTER TABLE
group_settings ADD COLUMN`` at startup (``bot.py:5858-5859``); a live
read-only ``PRAGMA table_info(group_settings)`` on prod's ``users.db``
shows it as column 16 with default 1. The new baseline
``0001_baseline_users`` never captured it. This migration is therefore
**idempotent**: ``upgrade`` inspects the live schema and only adds what
is absent — on prod it is a no-op, on a fresh test/dev DB it builds the
legacy layout.

Deploy note: on prod prefer the STAMP-AFTER-VERIFY
path (confirm the column with ``PRAGMA table_info(group_settings)``,
then ``alembic stamp 0010_rp_vip_outside``) over a blind ``upgrade``.
The idempotent guard makes a real run safe too, but the stamp keeps the
revision pointer honest without rebuilding a populated table.

SQLite requires :func:`op.batch_alter_table` for ``ADD COLUMN`` with a
``DEFAULT`` clause (the batch helper rebuilds the table); the column
``server_default`` mirrors the legacy ALTER default so a row built by
either side reads identically.

Heads chained: ``0009_bot_groups_is_active`` -> ``0010_rp_vip_outside``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_rp_vip_outside"
down_revision: str | None = "0009_bot_groups_is_active"
branch_labels = None
depends_on = None

_COLUMN_NAME = "rp_vip_outside_enabled"


def _column() -> sa.Column:
    # server_default matches the legacy ``ALTER TABLE group_settings ADD
    # COLUMN rp_vip_outside_enabled INTEGER DEFAULT 1`` statement.
    return sa.Column(_COLUMN_NAME, sa.Integer(), nullable=True, server_default="1")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "group_settings" not in set(inspector.get_table_names()):
        # Defensive: the baseline always creates group_settings; if it is
        # truly absent there is nothing to ALTER (a downstream baseline
        # bug would surface elsewhere first).
        return

    existing_cols = {c["name"] for c in inspector.get_columns("group_settings")}
    if _COLUMN_NAME in existing_cols:
        return

    with op.batch_alter_table("group_settings") as batch_op:
        batch_op.add_column(_column())


def downgrade() -> None:
    """Deliberately a no-op.

    #1974: the column predates this revision on prod — the module
    docstring above says so in capitals, and ``upgrade`` returns early
    when it is already there for exactly that reason. The old
    ``downgrade`` dropped it unconditionally, and what it dropped is a
    per-group decision about a **paid** perk.

    Losing it is not symmetric. The value legacy wrote is
    ``DEFAULT 1`` (``bot.py:5858-5859``), so re-running ``upgrade``
    after such a downgrade does not restore the setting — it silently
    switches the perk back ON for every group that had deliberately
    turned it off, and the group has no way to tell that its choice was
    discarded rather than changed. Until that re-run, the LIVE legacy
    process reads the column on every settings load
    (``bot.py:7798``, ``:7827``) and would fail with ``no such column``.

    On a fresh dev database this leaves one unused column behind, which
    is the cheaper mistake.
    """
