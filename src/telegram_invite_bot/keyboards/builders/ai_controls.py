"""Kom (AI) inline control keyboards — RR-6 #64/#65.

Legacy attached a control keyboard to every Kom reply in a private chat
(``create_ai_control_keyboard``, bot.py:38438) and a two-button rescue
keyboard to the daily-limit refusal (``create_limit_reached_keyboard``,
bot.py:38512). The monolith→split port dropped both: the new pipeline
answered with bare text, so switching persona, leaving the Kom session,
clearing the context or exporting the transcript all became commands the
user had to already know about — and the quota refusal was a dead end
with no way forward.

Two things this module owns:

* :func:`build_controls_markup` — the reply-card controls: seven persona
  buttons (VIP-gated, current one check-marked), the enter/leave toggle,
  clear-context and export.
* :func:`build_quota_limit_markup` — the daily-limit rescue: buy VIP and
  back to the menu.

Strangler invariant (same as ``keyboards/builders/voice_settings.py``):
the legacy callback literals ``ai_enter`` / ``ai_exit`` / ``ai_reset`` /
``ai_export`` / ``ai_save`` / ``ai_mode_<name>`` lost their process in
T-011 but not their keyboards, which are still in scrollback and still
tappable. Every prefix here is distinct (``aik_*``) so an old tap lands
nowhere instead of in a handler that never authored the payload.

No user id rides in any payload. Every action is keyed off
``callback.from_user.id`` — the persona, the session flag and the
transcript all belong to the *tapping* user, so a forged payload buys an
attacker nothing but a round trip through their own state. The keyboards
are attached in PRIVATE chats only (legacy did the same), which is also
what keeps :class:`~keyboards.builders.main_menu.MainMenu` buttons alive:
that router is private-filtered, so a menu button on a group card would
be dead on arrival.

Two deliberate divergences from legacy, both documented at the call site:

* **No "💾 Сохранить на сервере".** It wrote the transcript to a server
  file and echoed the absolute path back into the chat
  (bot.py:39238-39246) — an unbounded disk writer plus a filesystem-path
  disclosure, for content 📤 Export already hands the user directly.
* **The current persona is check-marked.** Legacy rendered seven
  identical buttons and told you the active one only in the message body,
  so a re-rendered card left you guessing.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.core.ai_modes import DEFAULT_MODE, SYSTEM_PROMPTS
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu

#: Persona buttons in the order legacy listed them (bot.py:38458-38464) —
#: the progression runs from the neutral default outward to the two
#: specialist voices, which is why it isn't alphabetised.
MODE_ORDER: tuple[str, ...] = (
    "default",
    "chat",
    "party",
    "help",
    "creative",
    "code",
    "expert",
)

# Persona buttons per row (legacy ``row_width=2``). The tail controls stay
# one-per-row: they are destructive-ish or produce a file, and a mis-tap
# next to a persona switch is the kind of thing users report as a bug.
_MODES_PER_ROW = 2


class AiKomEnter(CallbackData, prefix="aik_in"):
    """Start the «Войти в Ком» session (plain messages route to the model)."""


class AiKomExit(CallbackData, prefix="aik_out"):
    """Leave the Kom session; the conversation window is kept."""


class AiContextClear(CallbackData, prefix="aik_clr"):
    """Forget the rolling conversation window — the ``/reset`` half."""


class AiExport(CallbackData, prefix="aik_exp"):
    """Send the current window back as a ``.txt`` transcript."""


class AiModePick(CallbackData, prefix="aik_mode"):
    """Switch persona. ``mode`` is validated against :data:`MODE_ORDER`."""

    mode: str


def is_known_mode(token: str) -> bool:
    """True iff ``token`` is one of the seven personas.

    Checked against :data:`SYSTEM_PROMPTS` rather than :data:`MODE_ORDER`
    so a persona added to the value layer but not yet given a button is
    still accepted from a payload instead of being rejected as forged.
    """
    return token in SYSTEM_PROMPTS


def mode_title(mode: str, lang: str) -> str:
    """Localised persona label.

    ``core.ai_modes.MODE_TITLES`` is a byte-identical legacy port and so is
    Russian-only; rendering it to an English user leaked Cyrillic into an
    otherwise English card. The labels live in yaml under
    ``h_ai_mode_title_<mode>`` instead, with the canonical key itself as
    the last-resort fallback for a persona that has no label yet.
    """
    key = f"h_ai_mode_title_{mode}"
    label = t(key, lang)
    return mode if label == key else label


def build_controls_markup(
    *,
    lang: str,
    session_active: bool,
    can_change_mode: bool,
    current_mode: str = DEFAULT_MODE,
) -> InlineKeyboardMarkup:
    """Controls for a Kom reply card.

    ``can_change_mode`` is the VIP/developer gate (legacy
    ``can_change_ai_role``): a non-VIP user is pinned to the default
    persona, so showing them seven buttons that all answer "VIP only"
    would be a menu of refusals. They still get the toggle, clear and
    export controls, which are free for everyone.

    ``session_active`` flips the toggle label to the NEXT action — the
    same convention the voice-settings menu uses.
    """
    builder = InlineKeyboardBuilder()
    sizes: list[int] = []

    if can_change_mode:
        for mode in MODE_ORDER:
            label = mode_title(mode, lang)
            builder.button(
                text=f"✅ {label}" if mode == current_mode else label,
                callback_data=AiModePick(mode=mode),
            )
        full_rows, remainder = divmod(len(MODE_ORDER), _MODES_PER_ROW)
        sizes += [_MODES_PER_ROW] * full_rows
        if remainder:
            sizes.append(remainder)

    if session_active:
        builder.button(text=t("h_ai_btn_exit", lang), callback_data=AiKomExit())
    else:
        builder.button(text=t("h_ai_btn_enter", lang), callback_data=AiKomEnter())
    builder.button(text=t("h_ai_btn_clear", lang), callback_data=AiContextClear())
    builder.button(text=t("h_ai_btn_export", lang), callback_data=AiExport())
    sizes += [1, 1, 1]

    builder.adjust(*sizes)
    return builder.as_markup()


def build_quota_limit_markup(lang: str, *, is_vip: bool = False) -> InlineKeyboardMarkup:
    """Rescue keyboard for the daily-limit refusal (RR-6 #65).

    ``is_vip`` suppresses the upsell: a VIP who has burned through the VIP
    ceiling is told to buy the thing they already own otherwise. Legacy
    showed the button unconditionally because it only ever refused
    non-VIPs; the new quota service has a VIP ceiling too, so the copy has
    to keep up.
    """
    builder = InlineKeyboardBuilder()
    if not is_vip:
        builder.button(
            text=t("h_ai_btn_buy_vip", lang),
            callback_data=MainMenu(action="shop"),
        )
    builder.button(text=t("h_ai_btn_menu", lang), callback_data=MainMenu(action="home"))
    builder.adjust(1)
    return builder.as_markup()
