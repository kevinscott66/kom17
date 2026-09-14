"""Domain entity for a bot user.

A thin DTO crossing the repository → service → handler boundary. We
deliberately don't expose the SQLAlchemy ``User`` row directly so
handlers can't lazy-load from a closed session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    language_code: str | None
    is_premium: bool
    joined_date: datetime | None
    last_seen: datetime | None
    last_active: datetime | None
    is_new: bool
    """``True`` when this ``touch`` call inserted the row (vs updating)."""

    language_override: str | None = None
    """User's explicit choice from ``/lang`` (``user_settings.language``).

    ``None`` means they never picked a language and renderers should
    fall back to the Telegram ``language_code``. Populated by
    :class:`UserService.touch` via a join on ``user_settings`` (Stage 26).
    """

    timezone: str | None = None
    """User's stored IANA timezone from ``/timezone`` (Stage 27).

    ``None`` if unset / cleared. Populated by :class:`UserService.touch`
    alongside ``language_override`` so any renderer that already calls
    ``touch`` (most notably ``/profile``) can surface the tz for free
    without re-querying ``user_settings``. Kept distinct from the
    Telegram-side ``language_code`` because there is no Telegram
    equivalent — tz is purely a bot preference.
    """

    @property
    def language(self) -> str:
        """Coarse bot language: ``ru`` or ``en``.

        Resolution order matches legacy ``get_user_language``
        (bot.py:41778): explicit override first (``user_settings.language``),
        Telegram client locale second, RU as the default fallback. The
        ``en`` branch fires only on Telegram locales that actually start
        with ``en`` — anything else (``ru``, ``uk``, ``de``, ``None``) →
        Russian, matching the audience. Read the legacy CODE, not its
        docstring: ``bot.py:41781`` claims the default is ``en`` while
        ``bot.py:41812`` returns ``ru``.
        """
        if self.language_override in ("ru", "en"):
            return self.language_override
        code = (self.language_code or "").strip().lower()
        return "en" if code.startswith("en") else "ru"
