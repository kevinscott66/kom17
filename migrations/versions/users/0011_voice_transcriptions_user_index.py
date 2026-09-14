"""idx_voice_transcriptions_user — the per-speaker STT budget's index (#1938)

Revision ID: 0011_voice_transcriptions_user_index
Revises: 0010_rp_vip_outside
Create Date: 2026-09-09

``OPENAI_STT_USER_DAILY_SECONDS`` bounds one speaker's billed audio
across every group, so its query filters on ``user_id`` alone — the
whole point is to see chats the current one knows nothing about. The
table has only ``idx_voice_transcriptions_group``, which that predicate
cannot use, so the gate would full-scan ``voice_transcriptions`` on
every group voice note.

Unlike the three revisions #1937 corrected, this index is OURS: nothing
created it before us, so ``downgrade`` dropping it is a faithful
inverse and not the destruction of adopted state.

Idempotent in both directions. Test databases come from ``create_all``,
which builds the index off the model, and prod's table predates the new
pipeline entirely — so both ends check before acting rather than
assuming which of the two they are running against.

Heads chained: ``0010_rp_vip_outside`` -> ``0011_voice_transcriptions_user_index``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_voice_transcriptions_user_index"
down_revision: str | None = "0010_rp_vip_outside"
branch_labels = None
depends_on = None

_TABLE = "voice_transcriptions"
_INDEX = "idx_voice_transcriptions_user"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _INDEX in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        return
    op.create_index(_INDEX, _TABLE, ["user_id"])


def downgrade() -> None:
    """Drops the index this revision created — see the module docstring."""
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _INDEX in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
