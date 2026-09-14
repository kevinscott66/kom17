"""Bot-language selection helper.

Handlers render localised text by picking between an RU and an EN
variant. Before this module each call site repeated the same
``X_EN if user.language == "en" else X_RU`` ternary inline (9 sites
across ``help``, ``jokes``, ``profile``, ``start``, ``language``).
Some call sites additionally re-normalised the language string with
``(lang or "").strip().lower()`` even though :pyattr:`User.language`
is already the canonical ``"ru"``/``"en"`` form — that defensive
strip-lower was a leftover from a pre-entity refactor and silently
diverged from the entity contract.

Centralising the choice in one named helper gives us:

* One place to flip a default if we ever add a third language —
  the ternary becomes a mapping lookup in exactly one file instead
  of grepping nine.
* A consistent definition of "English" — strict equality to ``"en"``,
  matching :pyattr:`User.language` semantics. The legacy
  strip-lower path is gone; callers must pass the canonical form
  (which every handler already has via the entity property).
* A type-checked signature so a caller can't accidentally swap the
  RU/EN keywords — they're keyword-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from aiogram.types import User as TelegramUser

T = TypeVar("T")


def lang_from_code(language_code: str | None) -> str:
    """Coarse bot language (``"ru"``/``"en"``) from a Telegram locale.

    Single definition of the "is this English?" rule used everywhere the
    only signal available is the Telegram client locale: the ``en`` branch
    fires only on codes that start with ``en`` (case-insensitive); every
    other value (``ru``, ``uk``, ``de``, ``""``, ``None``) → ``ru``,
    matching the default-to-Russian audience policy and
    :pyattr:`telegram_invite_bot.core.entities.user.User.language`.
    """
    return "en" if (language_code or "").strip().lower().startswith("en") else "ru"


def resolve_lang(data: dict[str, Any], fallback_user: TelegramUser | None = None) -> str:
    """Return the effective bot language for the current update.

    The canonical source is ``data["lang"]``, stamped by
    :class:`telegram_invite_bot.middlewares.language.LanguageMiddleware`
    (stored ``user.language`` preference > Telegram ``language_code`` >
    ``"ru"``). This helper exists for handlers that might run on a path
    where that middleware did not stamp ``data`` (e.g. a router mounted
    without the root outer-middleware, or a unit test): it returns
    ``data["lang"]`` when present and valid, else derives ``ru``/``en``
    from ``fallback_user.language_code``, else ``"ru"``.

    Handlers that always run under the root router should prefer injecting
    ``lang: str`` directly (aiogram passes ``data["lang"]`` by name); reach
    for this helper only when the middleware presence is not guaranteed.
    """
    lang = data.get("lang")
    if lang == "en":
        return "en"
    if lang == "ru":
        return "ru"
    code = fallback_user.language_code if fallback_user is not None else None
    return lang_from_code(code)


def pick_by_language(language: str, *, ru: T, en: T) -> T:
    """Return ``en`` when ``language == "en"``, else ``ru``.

    ``language`` is expected to be the canonical bot-language string
    produced by :pyattr:`telegram_invite_bot.core.entities.user.User.language`
    — i.e. exactly ``"ru"`` or ``"en"``. Any other value (including
    ``None``-cast-to-str by an upstream bug) falls through to ``ru``,
    matching the legacy default-to-Russian audience policy.

    Keyword-only ``ru`` / ``en`` so swapping the two at a call site
    requires explicitly swapping the labels — it can't be a
    positional-order typo.
    """
    return en if language == "en" else ru
