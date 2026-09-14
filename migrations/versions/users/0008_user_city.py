"""users: user_settings.city — saved home city (RR-6 #74)

Revision ID: 0008_user_city
Revises: 0007_user_group_joins
Create Date: 2026-08-08

``/city`` is a setter again, and ``/weather`` / ``/forecast`` fall back
to the saved value when called bare. (``/time`` does not — it answers
from ``user_settings.timezone``, a separate preference; ``/city`` only
*offers* the zone its geocoder found.)

**Why a column and not legacy's file.** Legacy did NOT keep the city in
any database: ``get_user_city`` / ``set_user_city`` (bot.py:4428) read a
module-global dict persisted to ``DATABASE_DIR/user_cities.json``
(bot.py:757) — a whole-file rewrite per set, with no locking. The new
pipeline stores the preference next to the other per-user preferences it
already owns (``user_settings.language`` since Stage 26,
``user_settings.timezone`` since Stage 27), which buys transactional
writes and one read path instead of two.

The JSON file is *not* synced back. That would have been a live
correctness problem while the telebot still served commands, but
``telegram-bot.service`` is disabled and stopped on prod — the new bot
is the only writer. Prod's copy of the file holds exactly one entry, so
it gets a one-off ``UPDATE`` at deploy time rather than an importer: a
migration that reaches outside the database for a file whose path it can
only guess would be more machinery than the data justifies, and it would
run on every environment that has no such file.

Idempotent, like ``economy/0013_display_currency``: test databases come
from ``create_all`` (which already has the column off the model), so a
blind ``add_column`` would abort there with "duplicate column name".

No ``server_default``: NULL and ``''`` both mean "no city", exactly as
:meth:`UserSettingsRepo.get_city` reads them. Choosing a literal default
here is what made ``display_currency``'s "chose RUB" vs "never chose"
ambiguity unresolvable — not a mistake worth repeating.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_user_city"
down_revision: str | None = "0007_user_group_joins"
branch_labels = None
depends_on = None

_TABLE = "user_settings"
_COLUMN = "city"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN in existing:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing:
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
