"""voice transcription: group_settings columns + voice_transcriptions table (L-70)

Revision ID: 0005_voice_transcription
Revises: 0004_bond_activity_log
Create Date: 2026-06-13

Registers the L-70 group voice-message Speech-To-Text storage in the new
pipeline's Alembic chain:

* six ``group_settings`` columns (``voice_transcription``,
  ``transcription_target``, ``transcription_language``,
  ``transcription_log_chat_id``, ``auto_delete_voice``,
  ``transcription_only_for_admins``);
* the ``voice_transcriptions`` table + ``idx_voice_transcriptions_group``.

PROD ALREADY HAS ALL OF THIS. Legacy ``bot.py`` creates the columns via
``ALTER TABLE group_settings ADD COLUMN`` at startup (``bot.py:7771,
7800-7835``) and the table via ``CREATE TABLE IF NOT EXISTS
voice_transcriptions`` (``bot.py:5899-5917``). The new baseline
``0001_baseline_users`` never captured them. Because a real prod
``users.db`` already carries the columns/table, this migration is
**idempotent**: ``upgrade`` inspects the live schema and only adds what's
absent — on prod it is effectively a no-op, on a fresh test/dev DB it
builds the legacy layout.

Deploy note: on prod use the stamp-after-verify path
(confirm the columns/table exist, then ``alembic stamp 0005_voice_transcription``)
rather than running ``upgrade`` blindly — the idempotent guards make a
real run safe too, but the stamp keeps the revision pointer honest without
touching populated tables.

SQLite requires :func:`op.batch_alter_table` for ``ADD COLUMN`` with a
``DEFAULT`` clause (the batch helper rebuilds the table); the column
``server_default`` values mirror the legacy ALTER defaults so a row built
by either side reads identically.

Heads chained: ``0004_bond_activity_log`` -> ``0005_voice_transcription``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_voice_transcription"
down_revision: str | None = "0004_bond_activity_log"
branch_labels = None
depends_on = None


# (name, column-factory) — server_default values match the legacy
# ``ALTER TABLE group_settings ADD COLUMN ... DEFAULT ...`` statements.
def _columns() -> list[sa.Column]:
    return [
        sa.Column("voice_transcription", sa.Integer(), nullable=True, server_default="0"),
        sa.Column(
            "transcription_target", sa.Text(), nullable=True, server_default="chat"
        ),
        sa.Column(
            "transcription_language", sa.Text(), nullable=True, server_default="ru"
        ),
        sa.Column("transcription_log_chat_id", sa.Integer(), nullable=True),
        sa.Column("auto_delete_voice", sa.Integer(), nullable=True, server_default="0"),
        sa.Column(
            "transcription_only_for_admins",
            sa.Integer(),
            nullable=True,
            server_default="0",
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ---- group_settings columns (idempotent) --------------------------
    if "group_settings" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("group_settings")}
    else:
        # A fresh DB whose baseline already created a 2-column
        # group_settings; if it's truly absent we still add columns inside
        # the batch below against whatever the baseline produced.
        existing_cols = set()
    missing = [col for col in _columns() if col.name not in existing_cols]
    if missing and "group_settings" in existing_tables:
        with op.batch_alter_table("group_settings") as batch_op:
            for col in missing:
                batch_op.add_column(col)

    # ---- voice_transcriptions table (idempotent) ----------------------
    if "voice_transcriptions" not in existing_tables:
        op.create_table(
            "voice_transcriptions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("group_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=True),
            sa.Column("file_id", sa.Text(), nullable=True),
            sa.Column("file_unique_id", sa.Text(), nullable=True),
            sa.Column("duration", sa.Integer(), nullable=True),
            sa.Column("transcribed_text", sa.Text(), nullable=True),
            sa.Column("language", sa.Text(), nullable=True),
            sa.Column("model_used", sa.Text(), nullable=True),
            sa.Column("processing_time", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=True,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index(
            "idx_voice_transcriptions_group", "voice_transcriptions", ["group_id"]
        )


def downgrade() -> None:
    """Deliberately a no-op.

    #1937: every object this revision names predates it. The module
    docstring above says so in capitals — PROD ALREADY HAS ALL OF THIS
    — and ``upgrade`` is written as pure create-if-absent because of it.
    The old ``downgrade`` was the opposite: it dropped
    ``voice_transcriptions`` (``docs/prod_schemas.sql:282``, the whole
    STT history since ``bot.py:5899``) and six ``group_settings``
    columns (``docs/prod_schemas.sql:236``) that legacy adds with
    ``ALTER TABLE`` at startup (``bot.py:7771``, ``:7800-7835``).

    Those columns are the worst part: the legacy telebot process is
    still live on prod and still reads and writes them, so an
    ``alembic downgrade -1`` here does not merely lose the transcription
    history — it breaks a running process with ``no such column`` on
    every voice message. Re-running ``upgrade`` recreates empty
    structures; the rows only come back from a file-level backup.

    On a fresh dev database this leaves an orphan table and six unused
    columns, which is the cheaper mistake.
    """
