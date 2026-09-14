"""Async repository for ``moderation.group_mod_config`` — per-group
moderation config (L-43).

Surface:

* :meth:`get_or_default` — return the group's config, synthesising a
  defaults row (NOT persisted) when none exists. Callers always get a
  fully-populated :class:`GroupModConfigView` so they never branch on
  ``None``.
* :meth:`set_field` — upsert a single named field on the group's row,
  preserving the other columns at their current (or default) values.

The defaults baked into :data:`_DEFAULTS` mirror the hardcoded values
the moderation pipeline currently applies globally (see
:mod:`db.models.group_mod_config` for the legacy-constant mapping), so a
group with no override row behaves exactly as it does today.

No inline SQL; all access goes through SQLAlchemy core / ORM. The repo
trusts its arguments — field-name and range validation lives in the
handler (:mod:`handlers.modcfg`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.group_mod_config import GroupModConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class GroupModConfigView:
    """Immutable view of one group's moderation config.

    ``group_id`` is carried so a defaults view (synthesised for a group
    with no persisted row) is still self-describing.
    """

    group_id: int
    automod_enabled: bool
    profanity_enabled: bool
    max_warns: int
    mute_minutes: int
    autoban_enabled: bool
    # L-56 antiflood (per-group message-burst limiter).
    antiflood_enabled: bool
    flood_max_msgs: int
    flood_window_sec: int
    flood_mute_minutes: int
    # L-55 join captcha (restrict-until-button verification). Defaulted
    # so a constructor that predates the feature keeps working without
    # naming the new fields.
    captcha_enabled: bool = False
    captcha_timeout_sec: int = 120
    # L-54 per-group economy earn toggle (passive per-message coin
    # reward). Defaulted ON — legacy had only the global gate, so a
    # group with no row keeps earning. Trailing default keeps existing
    # constructor call-sites working without naming the new field.
    coins_enabled: bool = True


# Defaults mirror the moderation pipeline's hardcoded values:
#   automod_enabled   ← legacy auto_moderate                 (True)
#   profanity_enabled ← legacy profanity_enabled             (True)
#   max_warns         ← handlers.moderation.WARNING_THRESHOLD (3)
#   mute_minutes      ← legacy mute_duration 24h              (1440)
#   autoban_enabled   ← legacy auto_ban_on_max_warnings       (True)
_DEFAULTS: Final[dict[str, object]] = {
    "automod_enabled": True,
    "profanity_enabled": True,
    "max_warns": 3,
    "mute_minutes": 1440,
    "autoban_enabled": True,
    # L-56 antiflood: OFF by default — legacy had no antiflood, so an
    # unconfigured group must behave exactly as before.
    "antiflood_enabled": False,
    "flood_max_msgs": 5,
    "flood_window_sec": 10,
    "flood_mute_minutes": 10,
    # L-55 join captcha: OFF by default — captcha has no legacy
    # counterpart at all (``bot.py`` never had one), so an unconfigured
    # group must behave exactly as before.
    "captcha_enabled": False,
    "captcha_timeout_sec": 120,
    # L-54 economy earn toggle: ON by default — legacy gated passive
    # earning only globally, so an unconfigured group must keep earning.
    "coins_enabled": True,
}

# The mutable config fields, in display order. Used by the handler to
# validate the field name and render the config; kept here so the repo
# and handler agree on one source of truth.
FIELD_NAMES: Final[tuple[str, ...]] = (
    "automod_enabled",
    "profanity_enabled",
    "max_warns",
    "mute_minutes",
    "autoban_enabled",
    "antiflood_enabled",
    "flood_max_msgs",
    "flood_window_sec",
    "flood_mute_minutes",
    "captcha_enabled",
    "captcha_timeout_sec",
    "coins_enabled",
)


class GroupModConfigRepo:
    """``moderation.group_mod_config`` access — get-or-default / set-one-field."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _defaults_view(self, group_id: int) -> GroupModConfigView:
        return GroupModConfigView(group_id=group_id, **_DEFAULTS)  # type: ignore[arg-type]

    async def get_or_default(self, group_id: int) -> GroupModConfigView:
        """Return the group's config, or a synthesised defaults view.

        The defaults view is NOT persisted — reads are side-effect-free.
        A row is only created on the first :meth:`set_field` call.
        """
        row = await self._session.get(GroupModConfig, group_id)
        if row is None:
            return self._defaults_view(group_id)
        return GroupModConfigView(
            group_id=row.group_id,
            automod_enabled=row.automod_enabled,
            profanity_enabled=row.profanity_enabled,
            max_warns=row.max_warns,
            mute_minutes=row.mute_minutes,
            autoban_enabled=row.autoban_enabled,
            antiflood_enabled=row.antiflood_enabled,
            flood_max_msgs=row.flood_max_msgs,
            flood_window_sec=row.flood_window_sec,
            flood_mute_minutes=row.flood_mute_minutes,
            captcha_enabled=row.captcha_enabled,
            captcha_timeout_sec=row.captcha_timeout_sec,
            coins_enabled=row.coins_enabled,
        )

    async def set_field(
        self, *, group_id: int, field: str, value: bool | int
    ) -> GroupModConfigView:
        """Set one field, creating the row from defaults if absent.

        Returns the updated config view. ``field`` MUST be a member of
        :data:`FIELD_NAMES` (the handler validates this before calling).

        Implemented as a read-merge-upsert against the ``group_id``
        primary key: the current values (or the defaults, for a missing
        row) are loaded, the one named field overridden, and the whole
        row written via ``INSERT ... ON CONFLICT DO UPDATE``. SQLite
        serialises writes per file, so the merge is race-free under the
        write lock.
        """
        if field not in FIELD_NAMES:
            msg = f"unknown group_mod_config field: {field!r}"
            raise ValueError(msg)

        current = await self.get_or_default(group_id)
        override: dict[str, Any] = {field: value}
        merged = replace(current, **override)

        stmt = sqlite_insert(GroupModConfig).values(
            group_id=group_id,
            automod_enabled=merged.automod_enabled,
            profanity_enabled=merged.profanity_enabled,
            max_warns=merged.max_warns,
            mute_minutes=merged.mute_minutes,
            autoban_enabled=merged.autoban_enabled,
            antiflood_enabled=merged.antiflood_enabled,
            flood_max_msgs=merged.flood_max_msgs,
            flood_window_sec=merged.flood_window_sec,
            flood_mute_minutes=merged.flood_mute_minutes,
            captcha_enabled=merged.captcha_enabled,
            captcha_timeout_sec=merged.captcha_timeout_sec,
            coins_enabled=merged.coins_enabled,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["group_id"],
            set_={field: value},
        )
        await self._session.execute(stmt)
        return merged
