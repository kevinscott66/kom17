"""ORM mapping for ``moderation.group_mod_config`` — per-group moderation config (L-43).

One row per group holds that group's moderation toggles/thresholds. The
columns mirror the hardcoded defaults the new moderation pipeline (and
legacy) currently apply globally:

* ``automod_enabled``    — legacy ``auto_moderate``       (default True)
* ``profanity_enabled``  — legacy ``profanity_enabled``   (default True)
* ``max_warns``          — legacy ``max_warnings`` /
                           :data:`handlers.moderation.WARNING_THRESHOLD` (default 3)
* ``mute_minutes``       — legacy ``mute_duration`` (24h → 1440 min) (default 1440)
* ``autoban_enabled``    — legacy ``auto_ban_on_max_warnings`` (default True)

``group_id`` is the PRIMARY KEY — a group has at most one config row.
A group with no row uses the dataclass defaults (see
:class:`repositories.group_mod_config_repo.GroupModConfigRepo`), so the
table is purely an override store: writing a row is opt-in, and the
moderation handlers stay on their hardcoded defaults until this config
is wired in (a documented follow-up, not an oversight).

The table is net-new (absent from the prod dump), so the Alembic
migration is a plain CREATE TABLE chained to the moderation head.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import ModerationBase


class GroupModConfig(ModerationBase):
    """Per-group moderation configuration (one row per group)."""

    __tablename__ = "group_mod_config"

    # Telegram group ids are large negative numbers (supergroups use the
    # ``-100…`` form), so BigInteger rather than Integer.
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    automod_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    profanity_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_warns: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    mute_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=1440)
    autoban_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # L-56 antiflood: per-group message-burst limiter. OFF by default —
    # legacy had no antiflood, so an unconfigured group must behave
    # exactly as before (migration 0006_antiflood_config).
    antiflood_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flood_max_msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    flood_window_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    flood_mute_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # L-55 join captcha: restrict-until-button verification for new
    # members. OFF by default — an unconfigured group must behave exactly
    # as before (migration 0007_captcha_config).
    captcha_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    captcha_timeout_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    # L-54 per-group economy earn toggle: gates the passive per-message
    # coin reward (middlewares/message_activity) for this group. ON by
    # default — legacy gated earning only by the GLOBAL ``coins_enabled``
    # setting (bot.py:43834), so an unconfigured group must keep earning
    # exactly as before (migration 0010_group_coins_toggle).
    coins_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
