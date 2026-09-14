"""Voice-transcription settings inline keyboards — CallbackData + markup (L-71/L-72).

Ports the legacy per-group transcription settings menu
(``bot.py:_render_voice_settings_menu`` 27955-27984) and its callbacks
(``bot.py`` 27987-28228) to the aiogram pipeline. The menu lets a group
admin toggle transcription on/off, pick a delivery target, pick a
recognition language, and open a read-only stats card (L-72, legacy
``bot.py:28187``).

Strangler invariant (same reasoning as ``keyboards/builders/groupadmin.py``):
the legacy callback literals (``voice_toggle_<id>`` / ``voice_target_<id>``
/ ``voice_language_<id>`` / ``voice_stats_<id>`` / ``voice_settings_<id>``)
no longer have a process behind them (T-011), but they are still on
keyboards legacy sent and a tap on one still arrives. The new buttons
keep a distinct ``vset`` prefix so such a tap resolves to nothing rather
than into a handler that never authored the payload.

No ``group_id`` field rides in the payload: every handler re-derives the
group from ``callback.message.chat.id`` (Telegram-set, not user-controlled)
and re-gates the tapping user, so a forged payload cannot cross groups or
impersonate an admin (mirrors ``GroupAdminRefresh``).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.i18n import t

# Published delivery targets (legacy ``target_map`` keys, bot.py:27959).
# ``log_chat`` is shown but only meaningful once a log chat is configured;
# the menu still lets an admin select it (legacy did too).
TARGET_CHAT = "chat"
TARGET_PRIVATE = "private"
TARGET_ADMINS = "admins"
TARGET_LOG_CHAT = "log_chat"

_TARGETS: tuple[str, ...] = (
    TARGET_CHAT,
    TARGET_PRIVATE,
    TARGET_ADMINS,
    TARGET_LOG_CHAT,
)

# Recognition languages. Legacy exposed a wider list driven by the Whisper
# model; ru/en is the honest minimum the new pipeline guarantees — a
# deliberate narrowing. Button labels live under ``h_vset_lang_<code>``.
LANG_RU = "ru"
LANG_EN = "en"

_LANGUAGES: tuple[str, ...] = (LANG_RU, LANG_EN)


def is_known_target(token: str) -> bool:
    """True iff ``token`` is one of the published ``TARGET_*`` values."""
    return token in _TARGETS


def is_known_language(token: str) -> bool:
    """True iff ``token`` is one of the published language codes."""
    return token in _LANGUAGES


class VoiceSettingsToggle(CallbackData, prefix="vset_tgl"):
    """Flip the master on/off flag, then re-render the menu."""


class VoiceSettingsOpenTarget(CallbackData, prefix="vset_tgt_open"):
    """Open the delivery-target submenu."""


class VoiceSettingsPickTarget(CallbackData, prefix="vset_tgt"):
    """Set the delivery target to ``target`` (validated in the handler)."""

    target: str


class VoiceSettingsOpenLanguage(CallbackData, prefix="vset_lng_open"):
    """Open the recognition-language submenu."""


class VoiceSettingsPickLanguage(CallbackData, prefix="vset_lng"):
    """Set the recognition language to ``language`` (validated in the handler)."""

    language: str


class VoiceSettingsToggleAutoDelete(CallbackData, prefix="vset_autodel"):
    """Flip "delete the voice note after transcribing" (RR-6 #73)."""


class VoiceSettingsToggleOnlyAdmins(CallbackData, prefix="vset_adminonly"):
    """Flip "transcribe admins' voice notes only" (RR-6 #73)."""


class VoiceSettingsStats(CallbackData, prefix="vset_stats"):
    """Open the read-only transcription stats card (L-72)."""


class VoiceSettingsBack(CallbackData, prefix="vset_back"):
    """Return to the main settings menu from a submenu / stats card."""


def _switch_label(key: str, on: bool, lang: str) -> str:
    """``✅ Label`` / ``❌ Label`` for an on-off switch button."""
    return f"{'✅' if on else '❌'} {t(key, lang)}"


def build_menu_markup(
    *,
    enabled: bool,
    auto_delete: bool,
    only_admins: bool,
    lang: str,
) -> InlineKeyboardMarkup:
    """Main settings menu keyboard (legacy bot.py:27974-27982).

    The master toggle's label reflects the NEXT action: "Enable" when
    currently off, "Disable" when on — exactly like legacy.

    The two RR-6 #73 switches read differently, on purpose: legacy's
    labels were bare nouns ("🗑️ Автоудаление голосового"), so the only
    way to learn a switch's state was to read it off the card above and
    map it back to the button by name. Here each carries its own ✅/❌,
    which is the same convention the target and language submenus
    already use for the active choice.

    Whisper *model* and *device* buttons are deliberately absent. They
    configured the monolith's local ``faster-whisper`` install; this
    pipeline calls the OpenAI ``whisper-1`` endpoint
    (``services/whisper_stt_service.py``), where neither knob means
    anything. Rendering them would be a menu of levers connected to
    nothing.
    """
    builder = InlineKeyboardBuilder()
    toggle_label = t("h_vset_btn_disable" if enabled else "h_vset_btn_enable", lang)
    builder.button(text=toggle_label, callback_data=VoiceSettingsToggle())
    builder.button(text=t("h_vset_btn_target", lang), callback_data=VoiceSettingsOpenTarget())
    builder.button(text=t("h_vset_btn_language", lang), callback_data=VoiceSettingsOpenLanguage())
    builder.button(
        text=_switch_label("h_vset_btn_autodelete", auto_delete, lang),
        callback_data=VoiceSettingsToggleAutoDelete(),
    )
    builder.button(
        text=_switch_label("h_vset_btn_onlyadmins", only_admins, lang),
        callback_data=VoiceSettingsToggleOnlyAdmins(),
    )
    builder.button(text=t("h_vset_btn_stats", lang), callback_data=VoiceSettingsStats())
    # One button per row — matches legacy ``markup.add`` (single-column).
    builder.adjust(1)
    return builder.as_markup()


def build_target_markup(*, current: str, lang: str) -> InlineKeyboardMarkup:
    """Delivery-target submenu; the active target is prefixed with a check."""
    builder = InlineKeyboardBuilder()
    for token in _TARGETS:
        label = t(f"h_vset_target_{token}", lang)
        if token == current:
            label = f"✅ {label}"
        builder.button(text=label, callback_data=VoiceSettingsPickTarget(target=token))
    builder.button(text=t("h_vset_btn_back", lang), callback_data=VoiceSettingsBack())
    builder.adjust(1)
    return builder.as_markup()


def build_language_markup(*, current: str, lang: str) -> InlineKeyboardMarkup:
    """Recognition-language submenu; the active language is prefixed with a check."""
    builder = InlineKeyboardBuilder()
    for code in _LANGUAGES:
        label = t(f"h_vset_lang_{code}", lang)
        if code == current:
            label = f"✅ {label}"
        builder.button(text=label, callback_data=VoiceSettingsPickLanguage(language=code))
    builder.button(text=t("h_vset_btn_back", lang), callback_data=VoiceSettingsBack())
    builder.adjust(1)
    return builder.as_markup()


def build_stats_markup(lang: str) -> InlineKeyboardMarkup:
    """Stats card keyboard — a single Back button (legacy bot.py:28224)."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("h_vset_btn_back", lang), callback_data=VoiceSettingsBack())
    builder.adjust(1)
    return builder.as_markup()
