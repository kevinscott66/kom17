"""Pure formatters for marriage / relationship leaderboards (Stage 19).

Mirrors a handful of legacy helpers:

* ``marriage_xp_to_level``         — bot.py:21636
* ``MARRIAGE_LEVEL_NAMES`` table  — bot.py:21619
* ``relationship_xp_to_level``     — bot.py:22197 (table lookup)
* ``_format_db_timestamp_as_date`` — bot.py:21676
* ``_format_marriage_duration``    — bot.py:21686
* ``_marriage_category``           — bot.py:21724

All functions are pure (no DB, no Telegram). The handler composes them
to build the leaderboard line — keeps the handler render-only and lets
us unit-test the arithmetic in isolation.

i18n note: legacy looks up RU/EN names from ``translations.py``. These
formatters inline both the Russian copy (byte-identical to legacy) and
an English mirror, switching on the ``lang`` parameter (defaulting to
``"ru"``). EN names are Cyrillic-free per the ru/en convergence rule.
"""

from __future__ import annotations

from datetime import datetime

# bot.py:21618 — 100 XP per marriage level, levels cap at 5.
_MARRIAGE_XP_PER_LEVEL = 100
_MARRIAGE_LEVEL_NAMES_RU: dict[int, str] = {
    1: "Новобрачные",
    2: "Супруги",
    3: "Семья",
    4: "Ветераны брака",
    5: "Неразлучны",
}
_MARRIAGE_LEVEL_FALLBACK = "Супруги"

# EN mirror of the RU level table. Kept inline (not in i18n YAML) so the
# pure formatters stay DB/Telegram-free and import-light; the handler
# passes ``lang`` through. Cyrillic-free per the ru/en convergence rule.
_MARRIAGE_LEVEL_NAMES_EN: dict[int, str] = {
    1: "Newlyweds",
    2: "Spouses",
    3: "Family",
    4: "Marriage Veterans",
    5: "Inseparable",
}
_MARRIAGE_LEVEL_FALLBACK_EN = "Spouses"

# bot.py:22097 — non-linear thresholds so the title genuinely tracks
# longevity (1 → 11 covers 150 XP → 10M XP).
_RELATIONSHIP_LEVEL_XP: tuple[int, ...] = (
    0,
    150,
    1500,
    5000,
    10000,
    30000,
    60000,
    150000,
    300000,
    1_000_000,
    3_000_000,
    10_000_000,
)


def marriage_xp_to_level(experience: int) -> int:
    """Mirror bot.py:21636 exactly — ``max(1, 1 + xp // 100)``.

    No upper clamp: legacy lets the integer grow past the name table
    and lets :func:`marriage_level_name` fall back to "Супруги".
    Clamping here would silently disagree with what legacy returns.
    """
    return max(1, 1 + (max(0, experience) // _MARRIAGE_XP_PER_LEVEL))


def marriage_level_name(level: int, lang: str = "ru") -> str:
    """Localized name for a marriage level.

    ``lang`` defaults to ``"ru"`` so callers that forget to thread a
    language still render the exact legacy Russian copy (byte-identical
    to bot.py:21619). ``lang="en"`` returns the English mirror; any
    other value falls back to RU.
    """
    if lang == "en":
        return _MARRIAGE_LEVEL_NAMES_EN.get(level, _MARRIAGE_LEVEL_FALLBACK_EN)
    return _MARRIAGE_LEVEL_NAMES_RU.get(level, _MARRIAGE_LEVEL_FALLBACK)


def relationship_xp_to_level(experience: int) -> int:
    """0..11; same threshold table as bot.py:22197.

    Legacy starts at level 0 for "no XP yet". Each subsequent threshold
    bumps the level by one — we mirror that exactly so the leaderboard
    number matches what users see in legacy ``/relationship``.
    """
    level = 0
    for idx in range(1, len(_RELATIONSHIP_LEVEL_XP)):
        if experience >= _RELATIONSHIP_LEVEL_XP[idx]:
            level = idx
    return level


def format_db_date(value: datetime | str | None) -> str:
    """``YYYY-MM-DD`` for UI, or ``'—'`` if missing.

    SQLite returns either ``datetime`` (when PARSE_DECLTYPES is on, as
    in our SQLAlchemy setup) or ``str`` (legacy sync path). We accept
    both so this helper doubles as a parity oracle in tests.
    """
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    return s[:10] if len(s) >= 10 else (s or "—")


def _coerce_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def format_duration(
    created_at: datetime | str | None,
    *,
    now: datetime | None = None,
    lang: str = "ru",
) -> str:
    """Coarse "since X" string: ``N дн.`` / ``N мес.`` / ``N лет``.

    Buckets match bot.py:21686 exactly (1mo = 31d, 1yr = 365d). The
    ``now`` parameter is the seam for deterministic tests; in
    production we use local-time ``datetime.now()`` — matching legacy,
    which also uses naive local time for these labels.
    """
    dt = _coerce_dt(created_at)
    if dt is None:
        return "—"
    now = now if now is not None else datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
    days = (now - dt).days
    if lang == "en":
        if days < 1:
            return "0 d"
        if days < 31:
            return f"{days} d"
        if days < 365:
            return f"{days // 30} mo"
        return f"{days // 365} y"
    if days < 1:
        return "0 дн."
    if days < 31:
        return f"{days} дн."
    if days < 365:
        return f"{days // 30} мес."
    return f"{days // 365} лет"


def marriage_category(
    created_at: datetime | str | None,
    extra_days: int = 0,
    *,
    now: datetime | None = None,
    lang: str = "ru",
) -> str:
    """Pick one of four longevity tiers — same buckets as bot.py:21724.

    ``lang`` defaults to ``"ru"`` (byte-identical legacy copy);
    ``lang="en"`` returns the English tier names.
    """
    dt = _coerce_dt(created_at)
    if dt is None:
        days = max(0, extra_days)
    else:
        now = now if now is not None else datetime.now()  # noqa: DTZ005  (mirrors legacy naive-local)
        days = max(0, (now - dt).days + max(0, extra_days))
    if lang == "en":
        if days < 31:
            return "Newlyweds"
        if days < 31 * 6:
            return "Experienced"
        if days < 365:
            return "Settled"
        return "Veterans"
    if days < 31:
        return "Молодожёны"
    if days < 31 * 6:
        return "Опытные"
    if days < 365:
        return "Семейные"
    return "Ветераны"
