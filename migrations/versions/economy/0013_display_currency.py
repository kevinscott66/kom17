"""economy: users.display_currency — saved display currency (RR-6 #66/#67)

Revision ID: 0013_display_currency
Revises: 0012_pvp_stake_games
Create Date: 2026-08-08

``/currency`` is an interactive setter again, and ``/rate`` / ``/convert``
default to whatever the user picked. The preference lives in
``economy.users.display_currency`` — deliberately the SAME column the
live telebot process reads and writes, so the two bots can never quote a
user two different currencies during the strangler window.

**Idempotent on purpose.** Legacy already creates this column at startup
via a lazy ``ALTER TABLE`` (bot.py:5298 ``_ensure_display_currency_
columns``), so on prod the column is present before this revision ever
runs — a blind ``add_column`` would abort with "duplicate column name".
We inspect first and add only what is missing; on a fresh test DB
``create_all`` has already produced it too. Same pattern as
``users/0005_voice_transcription``.

The server default is ``'RUB'``, matching what legacy's ALTER wrote —
which is exactly why "the row says RUB" and "this user never chose"
are indistinguishable. ``effective_currency`` documents how that
ambiguity is resolved for English users.

Deploy note: on prod prefer verifying with
``sqlite3 /var/lib/telegram-bot/economy.db 'PRAGMA table_info(users);'``
and then ``alembic stamp 0013_display_currency`` over running the
upgrade — the column is already there and the stamp records that fact
without touching the table. SQLite needs ``batch_alter_table`` for an
``ADD COLUMN`` carrying a ``DEFAULT``, so the add path uses it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_display_currency"
down_revision: str | None = "0012_pvp_stake_games"
branch_labels = None
depends_on = None

_TABLE = "users"
_COLUMN = "display_currency"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in existing:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(_COLUMN, sa.String(), nullable=True, server_default=sa.text("'RUB'"))
        )


def downgrade() -> None:
    """Deliberately a no-op.

    #1974: the previous body carried the inverted guard #1937 named as
    the worst shape of all. Its comment read "a downgrade on prod is a
    no-op by design" and then the code inspected the table and dropped
    the column *because* it was present. On prod the column is always
    present — legacy creates it at startup
    (``bot.py:5298`` ``_ensure_display_currency_columns``, the ALTER at
    ``bot.py:5306``, and ``docs/prod_schemas.sql:320``) — so the guard
    did not prevent the destruction, it guaranteed it, and only on the
    one database where it mattered.

    What is destroyed is every user's chosen display currency, and the
    module docstring above explains why it cannot be reconstructed: the
    server default is ``'RUB'``, so after a re-run of ``upgrade`` a row
    that says RUB is indistinguishable from a user who never chose. The
    LIVE telebot reads and writes the same column, which is the whole
    point of adopting it rather than adding our own.

    On a fresh dev database this leaves one unused column behind, which
    is the cheaper mistake.
    """
