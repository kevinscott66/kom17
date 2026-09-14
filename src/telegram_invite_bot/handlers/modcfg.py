"""/modcfg — per-group moderation configuration (L-43).

Ports the *config* half of legacy moderation (``bot.py`` settings:
``auto_moderate`` / ``profanity_enabled`` / ``max_warnings`` /
``mute_duration`` / ``auto_ban_on_max_warnings``) into a per-group store
so each group can override the otherwise-global moderation defaults.

Command surface (group-only, gated on LIVE Telegram admin status):

  /modcfg                 (alias /модконфиг) — show current config
  /modcfg <key> <value>   — set one field

Recognised keys (also accepts their RU aliases, normalised in
:func:`_normalise_key`):

  automod    bool  — auto-moderation on/off            (legacy auto_moderate)
  profanity  bool  — profanity filter on/off           (legacy profanity_enabled)
  warns      int   — max warnings before auto-ban       (legacy max_warnings)
  mute       int   — default mute duration, minutes     (legacy mute_duration)
  autoban    bool  — auto-ban at max warnings on/off     (legacy auto_ban_on_max_warnings)

Boolean values accept ``on/off``, ``1/0``, ``true/false``, ``yes/no``
and the RU ``вкл/выкл``, ``да/нет``.

Admin gate (CRITICAL) — a DELIBERATE widening over legacy, and why.
Authorisation reuses :func:`handlers.moderation._require_admin` verbatim
— live ``get_chat_member`` moderation authority, with the same
anonymous-admin + dev-bypass + fail-closed handling as every other
moderation command. We import that helper rather than reimplementing it.

Legacy ``cmd_modcfg`` was bot-owner-only (``is_owner``, bot.py:42175).
An earlier revision of this docstring justified the widening with "there
is NO rank/role model in the new pipeline" — that is false (#340): a
rank model exists in :mod:`services.rank_service`, and legacy's gate
never consulted one anyway. The real justification is the *scope* change
this port made: legacy ``/modcfg`` mutated process-global variables
(``global AUTO_MODERATE, PROFANITY_ENABLED, …``, bot.py:42173) that
applied to every chat the bot served, so owner-only was the only safe
gate. Here every write lands on one ``moderation.group_mod_config`` row
keyed by ``chat_id`` (:class:`repositories.group_mod_config_repo
.GroupModConfigRepo`), read back per-group by moderation, antiflood,
wordfilter and the captcha path. A group admin can therefore change
nothing outside their own group, which is exactly the authority
``/groupadmin`` grants over the same row (``handlers.groupadmin``
writes ``set_field``).

#673: that last clause used to read "under the same gate", and it was
not true when written — ``groupadmin._actor_allowed`` carried a
``get_rank >= ADMIN`` branch this gate has never had, so ``/groupadmin``
was the WIDER of the two and the sentence justified this widening by
pointing at a bigger one. #670 removed that branch, and the two gates
now really are the same decision: developer → live TG-admin, with
anonymous actors going through ``_require_admin``'s R-FIX-011 policy in
both files. Kept as a statement of the invariant, not as a claim about
the past: if the two ever diverge again, this paragraph is wrong and
one of them is a hole.

Storage: the simple ``/modcfg key value`` form is chosen over an inline
keyboard — it is stateless (no FSM, no callback router), trivially
scriptable, and the cleaner fit for a five-field toggle store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.moderation import _require_admin, _resolve_lang
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.repositories.group_mod_config_repo import (
    FIELD_NAMES,
    GroupModConfigRepo,
)
from telegram_invite_bot.utils.aiogram import command_body
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.render import on_off_text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigView
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo

log = logger.bind(component="handlers.modcfg")


# Validation bounds for the integer fields. Both are generous; the point
# is to reject pathological input (0 warns = instant ban on first warn,
# a year-long default mute) rather than to encode policy.
_MAX_WARNS_MIN = 1
_MAX_WARNS_MAX = 20
_MUTE_MINUTES_MIN = 1
_MUTE_MINUTES_MAX = 366 * 24 * 60  # Telegram's 366-day restrict ceiling, in minutes

# Public key aliases → canonical repo field name. RU aliases included so
# a Russian-speaking admin can type /модконфиг мут 30.
_KEY_ALIASES: dict[str, str] = {
    "automod": "automod_enabled",
    "автомод": "automod_enabled",
    "profanity": "profanity_enabled",
    "мат": "profanity_enabled",
    "warns": "max_warns",
    "варны": "max_warns",
    "mute": "mute_minutes",
    "мут": "mute_minutes",
    "autoban": "autoban_enabled",
    "автобан": "autoban_enabled",
    # L-56 antiflood (per-group message-burst limiter).
    "antiflood": "antiflood_enabled",
    "антифлуд": "antiflood_enabled",
    "floodmax": "flood_max_msgs",
    "флудмакс": "flood_max_msgs",
    "floodwin": "flood_window_sec",
    "флудокно": "flood_window_sec",
    "floodmute": "flood_mute_minutes",
    "флудмут": "flood_mute_minutes",
    # L-55 join captcha (restrict-until-button verification on join).
    "captcha": "captcha_enabled",
    "капча": "captcha_enabled",
    "captchatime": "captcha_timeout_sec",
    "капчавремя": "captcha_timeout_sec",
    # L-54 per-group economy earn toggle (passive per-message coins).
    "coins": "coins_enabled",
    "монеты": "coins_enabled",
}

# Canonical field → public key shown in the rendered config (the short
# form the admin types). The first alias pointing at each field, by
# insertion order, is the canonical display key.
_DISPLAY_KEY: dict[str, str] = {
    "automod_enabled": "automod",
    "profanity_enabled": "profanity",
    "max_warns": "warns",
    "mute_minutes": "mute",
    "autoban_enabled": "autoban",
    "antiflood_enabled": "antiflood",
    "flood_max_msgs": "floodmax",
    "flood_window_sec": "floodwin",
    "flood_mute_minutes": "floodmute",
    "captcha_enabled": "captcha",
    "captcha_timeout_sec": "captchatime",
    "coins_enabled": "coins",
}

_TRUE_TOKENS = frozenset({"on", "1", "true", "yes", "y", "вкл", "да"})
_FALSE_TOKENS = frozenset({"off", "0", "false", "no", "n", "выкл", "нет"})

_BOOL_FIELDS = frozenset(
    {
        "automod_enabled",
        "profanity_enabled",
        "autoban_enabled",
        "antiflood_enabled",
        "captcha_enabled",
        "coins_enabled",
    }
)

# Validation bounds for the L-56 antiflood integer fields. Generous —
# they reject pathological input (a 1-message "burst", a sub-second
# window, a year-long flood mute), not encode policy. Keyed by canonical
# field name; rendered through the generic ``h_af_cfg_range`` copy.
_FLOOD_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "flood_max_msgs": (2, 100),
    "flood_window_sec": (3, 600),
    "flood_mute_minutes": (1, 366 * 24 * 60),
    # L-55 join captcha: a sub-10s window is unanswerable on mobile; an
    # hour-plus pending restriction is pathological.
    "captcha_timeout_sec": (10, 3600),
}


def _normalise_key(raw: str) -> str | None:
    """Map a user-typed key (any case, RU or EN alias) to a repo field name."""
    return _KEY_ALIASES.get(raw.strip().lower())


def _parse_bool(raw: str) -> bool | None:
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def _render_config(cfg: GroupModConfigView, lang: str) -> str:
    """Build the multi-line config display."""
    lines = [t("h_modcfg_header", lang)]
    lines.append(
        t(
            "h_modcfg_row_automod",
            lang,
            key=_DISPLAY_KEY["automod_enabled"],
            value=on_off_text(cfg.automod_enabled, lang),
        )
    )
    lines.append(
        t(
            "h_modcfg_row_profanity",
            lang,
            key=_DISPLAY_KEY["profanity_enabled"],
            value=on_off_text(cfg.profanity_enabled, lang),
        )
    )
    lines.append(t("h_modcfg_row_warns", lang, key=_DISPLAY_KEY["max_warns"], value=cfg.max_warns))
    lines.append(
        t(
            "h_modcfg_row_mute",
            lang,
            key=_DISPLAY_KEY["mute_minutes"],
            value=cfg.mute_minutes,
            # RR-4 #42: also show the human-friendly hours figure legacy did.
            hours=round(cfg.mute_minutes / 60, 1),
        )
    )
    lines.append(
        t(
            "h_modcfg_row_autoban",
            lang,
            key=_DISPLAY_KEY["autoban_enabled"],
            value=on_off_text(cfg.autoban_enabled, lang),
        )
    )
    lines.append(
        t(
            "h_af_row_antiflood",
            lang,
            key=_DISPLAY_KEY["antiflood_enabled"],
            value=on_off_text(cfg.antiflood_enabled, lang),
        )
    )
    lines.append(
        t(
            "h_af_row_floodmax",
            lang,
            key=_DISPLAY_KEY["flood_max_msgs"],
            value=cfg.flood_max_msgs,
        )
    )
    lines.append(
        t(
            "h_af_row_floodwin",
            lang,
            key=_DISPLAY_KEY["flood_window_sec"],
            value=cfg.flood_window_sec,
        )
    )
    lines.append(
        t(
            "h_af_row_floodmute",
            lang,
            key=_DISPLAY_KEY["flood_mute_minutes"],
            value=cfg.flood_mute_minutes,
        )
    )
    lines.append(
        t(
            "h_cap_row_captcha",
            lang,
            key=_DISPLAY_KEY["captcha_enabled"],
            value=on_off_text(cfg.captcha_enabled, lang),
        )
    )
    lines.append(
        t(
            "h_cap_row_captchatime",
            lang,
            key=_DISPLAY_KEY["captcha_timeout_sec"],
            value=cfg.captcha_timeout_sec,
        )
    )
    lines.append(
        t(
            "h_eco_row_coins",
            lang,
            key=_DISPLAY_KEY["coins_enabled"],
            value=on_off_text(cfg.coins_enabled, lang),
        )
    )
    lines.append(t("h_modcfg_hint", lang))
    return "\n".join(lines)


async def handle_modcfg(
    message: Message,
    bot: Bot,
    group_mod_config_repo: GroupModConfigRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Show config (no args) or set one field (``/modcfg key value``)."""
    lang = await _resolve_lang(message, user_settings_repo)
    group_id = message.chat.id

    if not await _require_admin(message, bot, settings, lang):
        return

    parts = command_body(message).split()
    # parts[0] is the command itself.
    if len(parts) == 1:
        cfg = await group_mod_config_repo.get_or_default(group_id)
        await message.reply(_render_config(cfg, lang))
        return

    if len(parts) < 3:
        await message.reply(t("h_modcfg_usage", lang))
        return

    field = _normalise_key(parts[1])
    if field is None or field not in FIELD_NAMES:
        await message.reply(t("h_modcfg_unknown_key", lang, keys=", ".join(_DISPLAY_KEY.values())))
        return

    raw_value = parts[2]
    value: bool | int
    if field in _BOOL_FIELDS:
        parsed = _parse_bool(raw_value)
        if parsed is None:
            await message.reply(t("h_modcfg_bad_bool", lang))
            return
        value = parsed
    elif field == "max_warns":
        if not is_int_token(raw_value):
            await message.reply(t("h_modcfg_bad_int", lang))
            return
        n = int(raw_value)
        if n < _MAX_WARNS_MIN or n > _MAX_WARNS_MAX:
            await message.reply(
                t("h_modcfg_warns_range", lang, min=_MAX_WARNS_MIN, max=_MAX_WARNS_MAX)
            )
            return
        value = n
    elif field == "mute_minutes":
        if not is_int_token(raw_value):
            await message.reply(t("h_modcfg_bad_int", lang))
            return
        n = int(raw_value)
        if n < _MUTE_MINUTES_MIN or n > _MUTE_MINUTES_MAX:
            await message.reply(
                t(
                    "h_modcfg_mute_range",
                    lang,
                    min=_MUTE_MINUTES_MIN,
                    max=_MUTE_MINUTES_MAX,
                )
            )
            return
        value = n
    else:  # bounded integer fields (L-56 flood_* / L-55 captcha_timeout_sec)
        if not is_int_token(raw_value):
            await message.reply(t("h_modcfg_bad_int", lang))
            return
        n = int(raw_value)
        lo, hi = _FLOOD_INT_BOUNDS[field]
        if n < lo or n > hi:
            await message.reply(t("h_af_cfg_range", lang, key=_DISPLAY_KEY[field], min=lo, max=hi))
            return
        value = n

    try:
        updated = await group_mod_config_repo.set_field(group_id=group_id, field=field, value=value)
    except Exception as exc:  # noqa: BLE001 — surface a clean error, log the cause
        log.warning(
            "set_field failed (group={g}, field={f}): {exc!r}",
            g=group_id,
            f=field,
            exc=exc,
        )
        await message.reply(t("h_modcfg_save_fail", lang))
        return

    # #1876: the row is written and ``moderation.db`` is locked
    # (``set_field`` is an UPSERT, so ``BEGIN IMMEDIATE`` is held). Two
    # Telegram round-trips follow — the confirmation and the full config
    # echo — and the session middleware would otherwise keep the writer
    # slot for both. Worse than the lock: a FloodWait on the FIRST of
    # them rolls the setting back, so an admin who was told nothing has
    # in fact toggled nothing, but an admin whose confirmation arrived
    # and whose echo did not would have kept the change either way. The
    # setting is what matters; the echo is decoration.
    if checkpoint is not None:
        await checkpoint()

    display_value = on_off_text(bool(value), lang) if field in _BOOL_FIELDS else str(value)
    await message.reply(t("h_modcfg_set_ok", lang, key=_DISPLAY_KEY[field], value=display_value))
    # Echo the full config after a change so the admin sees the result.
    await message.answer(_render_config(updated, lang))
    log.bind(group=group_id, field=field, value=value).info("/modcfg set")


# ---------------------------------------------------------------------------
# Session middleware (moderation.db) — injects GroupModConfigRepo
# ---------------------------------------------------------------------------


class _GroupModConfigMiddleware(BaseSessionMiddleware):
    """Open one ``moderation`` session per update; expose the config repo.

    Mirrors :class:`middlewares.moderation.ModerationMiddleware` (same DB,
    same lifecycle) but lives next to its only consumer instead of in the
    shared ``middlewares/`` package. The two
    middlewares both target ``moderation.db`` and never run on the same
    router, so there is no double-session concern.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.MODERATION)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["group_mod_config_repo"] = GroupModConfigRepo(session)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the /modcfg router (group-only).

    Middlewares:
    * :class:`_GroupModConfigMiddleware` — injects ``group_mod_config_repo``
      (moderation.db).
    * :class:`SessionMiddleware` — injects ``user_settings_repo`` for the
      caller-language resolution shared with ``handlers.moderation``.

    The handler is a closure capturing ``settings`` (the same pattern as
    ``handlers.moderation`` and every other handler module).
    """
    from telegram_invite_bot.middlewares.session import SessionMiddleware

    router = Router(name="modcfg")
    router.message.middleware(_GroupModConfigMiddleware(registry))
    router.message.middleware(SessionMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _modcfg(
        message: Message,
        bot: Bot,
        group_mod_config_repo: GroupModConfigRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_modcfg(
            message, bot, group_mod_config_repo, user_settings_repo, settings, checkpoint
        )

    router.message.register(
        _modcfg,
        Command("modcfg", "модконфиг", ignore_case=True),
        F.from_user,
        group_filter,
    )

    return with_chat_type_refusal(router, scope="group")
