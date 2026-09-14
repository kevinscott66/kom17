"""economy: withdrawal_requests.alerted_at — durable stale-payout alert ledger (#1518)

Revision ID: 0016_withdrawal_alerted_at
Revises: 0015_webhook_fiat_amount

#169 tells the owner when the manual payout queue stops moving, and
#208 taught it to walk past a stuck head instead of falling silent on
it. Both halves depended on an in-process ``set[int]`` of "ids already
reported", which the constructor defended as the cheap side of an
asymmetric trade: a repeat DM after a restart is noise, a missed DM is
a user waiting on a payout nobody sees.

That trade was priced against an HOURLY alert. #281 moved the scan onto
the money tick, so it now runs every 60 seconds — and the unit restarts
on every deploy, with ``Restart=always`` on top. The ledger is therefore
emptied far more often than the queue drains, and each restart re-sends
the full backlog. "Noise" was an honest word for one repeat an hour; it
is not an honest word for a repeat on every deploy, and the alarm the
owner mutes is worse than no alarm at all — which is the failure mode
#169 exists to prevent.

``alerted_at`` stores the naive-UTC timestamp of the DM that named this
request, in the same frame and format ``created_at`` is written in
(``withdraw_service._now_iso``). NULL — the default for every existing
row — means "never reported", so the first pass after this migration
reports the current backlog exactly once and then goes quiet.

Cleared by ``WithdrawalsRepo.release_processing``: a provider refusal
puts the row back into ``pending``, i.e. genuinely back into the queue,
and a request the owner must look at again has to be able to earn a
fresh alert. Terminal rows keep their stamp — they have left the
``pending`` slice the alert reads, so it can never see them again.

Plain nullable ADD COLUMN — instant on SQLite, no table rebuild, and
invisible to the legacy writer, which never touches this column. The
``batch_alter_table`` the webhook revisions use is only needed for an
ADD COLUMN carrying a DEFAULT. Inspect-first for the same reason every
revision here does it: re-running an upgrade that already did its work
must be a no-op, not an abort.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016_withdrawal_alerted_at"
down_revision: str | None = "0015_webhook_fiat_amount"
branch_labels = None
depends_on = None

_TABLE = "withdrawal_requests"
_COLUMN = "alerted_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN in {col["name"] for col in inspector.get_columns(_TABLE)}:
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ledger column again, if this revision is the one that added it.

    A no-op when the table is gone or the column was never created —
    downgrading past a revision that already found its work done must
    not fail the chain. Dropping it loses only the "already reported"
    memory: the next pass re-reports the standing backlog once, which
    is precisely the pre-#1518 behaviour.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COLUMN not in {col["name"] for col in inspector.get_columns(_TABLE)}:
        return
    op.drop_column(_TABLE, _COLUMN)
