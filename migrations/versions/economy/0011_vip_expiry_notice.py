"""economy: users.vip_notified_till — once-per-grant VIP expiry notice (L-95)

Revision ID: 0011_vip_expiry_notice
Revises: 0010_p2p
Create Date: 2026-06-13

Legacy reminded a user whose VIP expires "soon" via
``maybe_notify_vip_expiring`` (bot.py:6957) — but only when a profile
view happened to run, and the "already notified" memory was a 24h
in-process cache (bot.py:6970), lost on every restart. The new pipeline
moves the notice into the hourly ``EconomyCleanupSweeper`` pass, so it
needs a durable "already notified for THIS grant" marker.

``vip_notified_till`` stores the ``vip_till`` value the user was last
notified about. The sweep notifies when the grant is inside the notice
window AND ``vip_notified_till != vip_till`` — so each distinct grant
deadline produces at most one DM, and extending VIP (which changes
``vip_till``) automatically re-arms the notice. NULL (the default for
every existing row) means "never notified".

Plain nullable ADD COLUMN — instant on SQLite, no table rebuild, and
backwards-compatible with the legacy writer which never touches it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011_vip_expiry_notice"
down_revision: str | None = "0010_p2p"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("vip_notified_till", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "vip_notified_till")
