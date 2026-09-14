"""rp 18+ gate: group_settings.rp_18_enabled + rp_18_prompt_sent (AUD-4)

Revision ID: 0006_rp_18_gate
Revises: 0005_voice_transcription
Create Date: 2026-06-15

Registers the per-group 18+ RP gate (FEAT-RP / audit AUD-4) in the new
pipeline's Alembic chain:

* ``group_settings.rp_18_enabled`` — master per-group toggle (default 0 =
  OFF; 18+ RP actions are refused until a group admin enables it);
* ``group_settings.rp_18_prompt_sent`` — one-time "admin, enable 18+?"
  affordance flag (default 0) so the inline enable-prompt is shown at most
  once per group.

PROD ALREADY HAS BOTH COLUMNS. Legacy ``bot.py`` creates them via
``ALTER TABLE group_settings ADD COLUMN ... INTEGER DEFAULT 0`` at startup
(``bot.py:5854-5861``, alongside the unmodelled ``rp_18_confirmed_at``
TEXT). The new baseline ``0001_baseline_users`` never captured them.
Because a real prod ``users.db`` already carries these columns, this
migration is **idempotent**: ``upgrade`` inspects the live schema and only
adds what's absent — on prod it is effectively a no-op, on a fresh
test/dev DB it builds the legacy layout.

Deploy note: on prod use the STAMP-AFTER-VERIFY path
(confirm the columns exist with ``PRAGMA table_info(group_settings)``,
then ``alembic stamp 0006_rp_18_gate``) rather than running ``upgrade``
blindly — the idempotent guards make a real run safe too, but the stamp
keeps the revision pointer honest without touching the populated table.

SQLite requires :func:`op.batch_alter_table` for ``ADD COLUMN`` with a
``DEFAULT`` clause (the batch helper rebuilds the table); the column
``server_default`` values mirror the legacy ALTER defaults so a row built
by either side reads identically.

Heads chained: ``0005_voice_transcription`` -> ``0006_rp_18_gate``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_rp_18_gate"
down_revision: str | None = "0005_voice_transcription"
branch_labels = None
depends_on = None


# (name, column-factory) — server_default values match the legacy
# ``ALTER TABLE group_settings ADD COLUMN ... INTEGER DEFAULT 0`` statements.
def _columns() -> list[sa.Column]:
    return [
        sa.Column("rp_18_enabled", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("rp_18_prompt_sent", sa.Integer(), nullable=True, server_default="0"),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "group_settings" not in existing_tables:
        # Defensive: the baseline always creates group_settings; if it's
        # truly absent there is nothing to ALTER (a downstream baseline
        # bug would surface elsewhere first).
        return

    existing_cols = {c["name"] for c in inspector.get_columns("group_settings")}
    missing = [col for col in _columns() if col.name not in existing_cols]
    if missing:
        with op.batch_alter_table("group_settings") as batch_op:
            for col in missing:
                batch_op.add_column(col)


def downgrade() -> None:
    """Deliberately a no-op.

    #1974: both columns predate this revision on every database the
    legacy process has ever opened. The module docstring above says so
    in capitals — PROD ALREADY HAS BOTH COLUMNS — and ``upgrade`` is
    written as pure create-if-absent because of it. The old
    ``downgrade`` was the opposite: an unconditional two-column drop
    through ``batch_alter_table``, i.e. a full table rebuild that throws
    the values away.

    That is worse than losing our own data. The legacy telebot is still
    live on prod and reads ``rp_18_enabled`` on every RP action
    (``bot.py:21768``, ``:27413``) and writes it from the admin toggle
    (``bot.py:23572``, ``:27659``, ``:27679``); ``rp_18_prompt_sent`` is
    the one-shot flag that keeps the enable-prompt from repeating. An
    ``alembic downgrade -1`` here would reset every group's 18+ gate and
    then break the running process with ``no such column``.

    Note the position in the chain: ``0005`` below and ``0007`` above
    were both made no-ops by #1937, so a downgrade of the ``users`` tree
    from head passes straight through this revision — being surrounded
    by fixed neighbours protected nothing.

    On a fresh dev database this leaves two unused columns behind, which
    is the cheaper mistake.
    """
