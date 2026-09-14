"""SQLAlchemy mapping for ``users.user_settings`` (Stage 26).

Legacy stores the user's *explicit* language choice (and other
preferences) in a separate table from ``users.users``: the latter
holds Telegram-reported metadata (``language_code`` reflects the
client locale and gets overwritten every ``update_user_info``), while
``user_settings.language`` is the override the user made via ``/lang``
and is what every renderer should read first.

Reverse-engineered from the prod schema (and verified against the
local ``database/users.db`` PRAGMA): see ``bot.py:5469`` for the CREATE
TABLE statement and ``bot.py:5943`` for the ALTER TABLE migrations
(``timezone``, ``current_group_id``, ``show_balance``).

Only ``language`` is read/written in Stage 26 — declaring every column
so test-time ``create_all`` produces a schema-compatible table the
legacy queries can also read. The alternative (minimal model) would
mean SQLAlchemy's CREATE TABLE drops the columns legacy uses for
``/settings``, ``/timezone``, etc., and writes from this side would
omit them on UPSERT.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import UsersBase


class UserSetting(UsersBase):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.user_id"), primary_key=True)
    language: Mapped[str | None] = mapped_column(String, default="ru", nullable=True)
    notifications: Mapped[bool | None] = mapped_column(Boolean, default=True, nullable=True)
    theme: Mapped[str | None] = mapped_column(String, default="dark", nullable=True)
    show_rank: Mapped[bool | None] = mapped_column(Boolean, default=True, nullable=True)
    show_balance: Mapped[bool | None] = mapped_column(Boolean, default=True, nullable=True)
    auto_delete: Mapped[bool | None] = mapped_column(Boolean, default=False, nullable=True)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    current_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # RR-6 #74. Unlike every column above, this one has no legacy
    # counterpart in the table: legacy kept the city in a JSON file
    # (``DATABASE_DIR/user_cities.json``, bot.py:757). Added here by
    # ``users/0008_user_city`` so the preference lives with the other
    # per-user preferences and gets transactional writes.
    city: Mapped[str | None] = mapped_column(String, nullable=True)
