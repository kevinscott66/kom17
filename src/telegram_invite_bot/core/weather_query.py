"""Pure parsers for free-form weather / time phrases (RR-6 #68, #69).

These started life inside :mod:`telegram_invite_bot.services.ai_context`,
where they existed to decide what facts to inject into Kom's system
prompt. RR-6 #68 gives them a second caller — the plain-text weather
surface (`«какая погода в Казани»` with no slash) — and #69 a third
(`/time Лондон`). Two callers on opposite sides of the codebase importing
"AI context" for a regex is the kind of dependency that reads like an
accident, so the parsing moved down here: no I/O, no service objects,
nothing to mock. ``ai_context`` re-exports the names it always had.

Everything here is a legacy port with the line reference on it. Legacy
did this parsing twice — once for its plain-text handler and once for the
AI prompt builder — and the two copies had already drifted; one home
means one behaviour.
"""

from __future__ import annotations

import re

# Phrases that mean "the user's own city" — treated as "no explicit city"
# so the caller falls back to the stored ``/city``. Port of legacy
# ``WEATHER_LOCATION_PLACEHOLDERS`` (bot.py:37397).
LOCATION_PLACEHOLDERS = frozenset(
    {
        "моём городе",
        "моем городе",
        "у меня",
        "здесь",
        "тут",
        "мой город",
        "моего города",
        "в моём городе",
        "в моем городе",
        "моём",
        "моем",
        "у нас",
        "здесь у нас",
    }
)

# ``/time`` accepts the same "my own city" phrasings plus two
# second-person forms that never name a place either. Derived from
# :data:`LOCATION_PLACEHOLDERS` so the two surfaces cannot drift again
# (#955): «время в моем городе» used to reach the geocoder verbatim
# while «погода в моем городе» short-circuited to the stored ``/city``.
_TIME_PLACEHOLDERS = LOCATION_PLACEHOLDERS | frozenset(
    {
        # #1075: the first-person set above carries the bare word AND
        # the full phrase; carrying only the bare second-person forms
        # was the same drift #955 set out to end, one person over.
        # «время в твоем городе» stripped to «твоем городе» and reached
        # the geocoder verbatim, while «время в моем городе» did not.
        "твоём",
        "твоем",
        "твой город",
        "твоём городе",
        "твоем городе",
        "в твоём городе",
        "в твоем городе",
    }
)

# Forecast horizon Open-Meteo serves for free. Legacy clamped to the same
# window (bot.py:37492).
MAX_FORECAST_DAYS = 10

# ``завтра`` / ``tomorrow`` needs two days of data, not one: day[0] is
# today and day[1] is the answer.
_TOMORROW_DAYS = 2

_TOMORROW_RE = re.compile(r"\b(завтра|tomorrow)\b", re.IGNORECASE)
# Legacy spelled the noun ``дн(?:я|ей)?`` (bot.py:37486), which also
# matches the bare stump «дн» people type when they abbreviate — «на
# 5 дн». That stump is legacy behaviour and has to stay. «день» and
# the «for » preposition are additions: legacy had neither, and both
# are what users actually type. Keep them ordered longest-first so
# the intent is readable, though the trailing ``\b`` already makes
# the shorter alternatives unable to steal a longer word.
_DAYS_RE = re.compile(
    r"\b(?:на\s+|for\s+)?(\d{1,2})\s*(?:дней|дня|день|дн|days|day)\b",
    re.IGNORECASE,
)
# «на неделю» / «for a week» is not a legacy phrasing, but it is what
# people type when they mean /forecast, and answering it costs one more
# alternation rather than a "не понял".
_WEEK_RE = re.compile(r"\b(?:на\s+)?недел[юяи]\b|\bfor\s+(?:a\s+|the\s+)?week\b", re.IGNORECASE)
# Weekdays and «выходные» are period words too. :func:`detect_period`
# deliberately does not read them (a weekday is not a forecast horizon
# we can serve), but they MUST be stripped before a location survives:
# «погода в Москве в выходные» otherwise handed the geocoder the string
# «выходные» and answered "city not found" for a message that named the
# city unambiguously (#949). The leading preposition is part of the
# match so the phrase leaves no dangling «в» behind for the split in
# :func:`normalize_weather_location`. A settlement actually named
# «Пятница» or «Среда» loses to this in its nominative form; the
# oblique form users type after «в» («в Пятнице») is not matched and
# still resolves.
_WEEKDAY_RE = re.compile(
    r"\b(?:в|во|на)?\s*(?:"
    r"понедельник|вторник|сред[ауы]|четверг|пятниц[ауы]|"
    r"суббот[ауы]|воскресень[еяю]|выходны[ехм]+"
    r")\b",
    re.IGNORECASE,
)
_PERIOD_STRIP_RES: tuple[re.Pattern[str], ...] = (
    _WEEK_RE,
    _DAYS_RE,
    _WEEKDAY_RE,
    re.compile(r"\b(сегодня|завтра|today|tomorrow)\b", re.IGNORECASE),
)

_WEATHER_WORD_RE = re.compile(r"\b(погод[ауыев]?|weather)\b", re.IGNORECASE)

# Prepositions that introduce the place. Stripped twice — once up front
# and once after the period words are gone — because «погода завтра в
# Сочи» only exposes its «в» after «завтра» has been removed, and a
# leading «в» is exactly what makes the geocoder miss.
_LEADING_PREPOSITION_RE = re.compile(r"^\s*(?:в|во|in|at)\s+", re.IGNORECASE)


def is_weather_query(text: str | None) -> bool:
    """Whether the text asks about weather. Port of ``bot.py:37652``."""
    if not text:
        return False
    low = text.strip().lower()
    return (
        low.startswith(("погода", "weather"))
        or bool(re.search(r"\bпогод[ауыев]?\b", low))
        or bool(re.search(r"\bweather\b", low))
        or bool(
            re.search(
                r"какая\s+погода|погоду\s+(?:в|на)|что\s+по\s+погоде|"
                r"расскажи\s+про\s+погод",
                low,
            )
        )
    )


def normalize_weather_location(raw: str) -> str:
    """Pull a city out of a free-form weather phrase. Port of ``bot.py:37404``.

    Returns ``""`` when the phrase names no city — including when it names
    the user's *own* city obliquely ("в моём городе"), which is the signal
    to fall back to the stored ``/city`` rather than to give up.
    """
    q = (raw or "").strip()
    q = re.sub(r"^\s*(ком|ии|ai)\s+", "", q, flags=re.IGNORECASE)
    q = re.sub(r"^\s*(погода|weather)\s*[:,-]?\s*", "", q, flags=re.IGNORECASE)
    m = _WEATHER_WORD_RE.search(q)
    if m:
        q = q[m.end() :].strip()
    q = _LEADING_PREPOSITION_RE.sub("", q)
    q = strip_period_tokens(q)
    q = _LEADING_PREPOSITION_RE.sub("", q)
    if " в " in q:
        # The split is what rescues «ком какая погода сейчас в Москве»:
        # «сейчас» is not a period word, the leading preposition has
        # already been consumed, and only this hands back «Москве».
        # The period check is the defensive half of #949 — a period
        # phrasing the strip above does not model must never be taken
        # for a city name.
        parts = q.split(" в ", 1)
        suffix = (parts[-1] or "").strip().strip("?!.,")
        if (
            len(suffix) > 1
            and suffix.lower() not in LOCATION_PLACEHOLDERS
            and not any(pattern.search(suffix) for pattern in _PERIOD_STRIP_RES)
        ):
            q = suffix
    q = q.strip(" \t\r\n.,!?")
    if q.lower() in LOCATION_PLACEHOLDERS or not q:
        return ""
    if re.match(r"^и\s+(время|дата|погод)", q, re.IGNORECASE) or re.match(
        r"^(время|дата)\s+и\s+", q, re.IGNORECASE
    ):
        return ""
    return q


def detect_period(raw: str | None) -> tuple[str, int]:
    """``(mode, days)`` for a phrase. Port of ``_detect_weather_period``.

    ``mode`` is ``today`` / ``tomorrow`` / ``range``; ``days`` is how many
    days of forecast the mode needs. ``"погода на 1 день"`` collapses to
    ``today`` because a one-day "range" is just today with extra words.
    """
    low = (raw or "").lower()
    if _TOMORROW_RE.search(low):
        return "tomorrow", _TOMORROW_DAYS
    if _WEEK_RE.search(low):
        return "range", 7
    m = _DAYS_RE.search(low)
    if m:
        days = max(1, min(MAX_FORECAST_DAYS, int(m.group(1))))
        return ("today", 1) if days == 1 else ("range", days)
    return "today", 1


def strip_period_tokens(raw: str) -> str:
    """Remove the period words :func:`detect_period` reads, leave the rest.

    Lets a caller parse ``«погода завтра в Казани»`` once — period from
    :func:`detect_period`, city from what survives here — instead of
    threading a "which words did I already consume" flag around.
    """
    out = raw or ""
    for pattern in _PERIOD_STRIP_RES:
        out = pattern.sub(" ", out)
    return re.sub(r"\s+", " ", out).strip()


# Russian city names extracted from a free-form phrase arrive in an
# oblique case ("погода в Москве" → "Москве", "в Казани" → "Казани"),
# but the Open-Meteo geocoder only resolves the nominative ("Москва",
# "Казань") — a prefix mismatch that silently fails the lookup, so the
# model is told the weather is unavailable even for a well-known city
# (the live "погода в Москве" bug). We can't decline arbitrary Russian
# nouns without a morphology library, but a small suffix-rewrite covers
# the overwhelmingly common locative/prepositional endings city names
# take after «в»/«во». Each rule maps an oblique suffix to its
# nominative form; the first matching rule wins. Conservative on
# purpose: when nothing matches we return the input unchanged so a city
# that was already nominative (or that we don't model) is passed through
# untouched.
_RU_CITY_NOMINATIVE_RULES: tuple[tuple[str, str], ...] = (
    # -ге/-ке/-хе → -га/-ка/-ха (Калуге→Калуга, Уфе keep below) — vowel
    # palatal-stem datives/prepositionals.
    ("ге", "га"),
    ("ке", "ка"),
    ("хе", "ха"),
    # Soft-sign feminine -и → -ь (Казани→Казань, Твери→Тверь, Перми→Пермь).
    ("ани", "ань"),
    ("ери", "ерь"),
    ("ыни", "ынь"),
    ("ерми", "ермь"),
    # Generic -е → -а (Москве→Москва, Уфе→Уфа, Самаре→Самара).
    ("е", "а"),
)

# Longer, more specific suffixes that must be tried BEFORE the generic
# table (e.g. "-бурге" must beat "-ге"→"-га"). Kept separate so order is
# explicit rather than relying on the tuple position.
_RU_CITY_NOMINATIVE_SPECIAL: tuple[tuple[str, str], ...] = (
    ("бурге", "бург"),  # Петербурге → Петербург
)


def nominalize_ru_city(city: str) -> str:
    """Best-effort oblique-case → nominative Russian city name.

    Returns the city in (an approximation of) the nominative so the
    Open-Meteo geocoder can resolve it. Multi-word names are normalised
    on the LAST token only ("Нижнем Новгороде" → "Нижнем Новгород" is
    not attempted; we conservatively rewrite the final word). Non-Cyrillic
    or already-nominative inputs pass through unchanged. Never raises.
    """
    city = (city or "").strip()
    if not city:
        return city
    # Only attempt this on Cyrillic input — Latin city names ("Berlin")
    # are already nominative for the geocoder.
    if not re.search(r"[а-яё]", city.lower()):
        return city
    head, sep, last = city.rpartition(" ")
    word = last or city
    low = word.lower()
    for suffix, repl in _RU_CITY_NOMINATIVE_SPECIAL:
        if low.endswith(suffix) and len(low) > len(suffix):
            word = word[: -len(suffix)] + repl
            return f"{head}{sep}{word}" if sep else word
    for suffix, repl in _RU_CITY_NOMINATIVE_RULES:
        if low.endswith(suffix) and len(low) > len(suffix):
            word = word[: -len(suffix)] + repl
            return f"{head}{sep}{word}" if sep else word
    return city


def _strip_oblique_e(city: str) -> str:
    """Last word minus a final ``е`` — the masculine nominative (#1072).

    The mirror image of :func:`nominalize_ru_city`'s generic ``е`` →
    ``а`` rule, which only ever yields the feminine form. Port of the
    legacy rule at bot.py:37441-37442, widened from "single word" to
    "last word" so it lines up with :func:`nominalize_ru_city`. The
    length floor is legacy's: below it the stem carries no information
    («Уфе» → «Уф» is noise, and «Уфа» is feminine anyway).

    Returns the input unchanged when the rule does not apply, so the
    caller can append the result unconditionally and let the dedupe in
    :func:`geocode_variants` drop it.
    """
    city = (city or "").strip()
    if not city:
        return city
    head, sep, last = city.rpartition(" ")
    word = last or city
    low = word.lower()
    if len(low) <= 4 or not low.endswith("е") or not re.search(r"[а-яё]", low):
        return city
    word = word[:-1]
    return f"{head}{sep}{word}" if sep else word


def geocode_variants(query: str) -> list[str]:
    """Spellings to try against the geocoder, best guess first.

    The raw query always goes first, so a name that resolves as typed is
    never rewritten — :func:`nominalize_ru_city` is a heuristic and this
    ordering is what keeps it from mangling a city it doesn't model. The
    later variants only cost an HTTP call on a miss, which is exactly the
    case where the user was about to be told "no such city".

    Legacy also lopped the final ``е`` off any single word longer than
    four characters (bot.py:37441-37442). That was dismissed here as
    producing «Москв» — true for a FEMININE name, and false for every
    masculine one, which forms its prepositional as stem + ``е``. For
    those the legacy rule is exactly right and
    :func:`nominalize_ru_city`'s generic ``е`` → ``а`` is exactly
    wrong: it hands the geocoder a genitive. «Новосибирске» became
    «Новосибирска», and Open-Meteo resolves neither — measured against
    the live endpoint, both spellings return zero results while
    «Новосибирск» resolves (#1072).

    So both are emitted rather than one being guessed at: nothing short
    of a morphology table distinguishes «Туле» (Тула) from «Ярославле»
    (Ярославль). The geocoder's own fuzzy matching absorbs the near
    miss either way — it answers «Ярославль» for both «Ярославл» and
    «Ярославль», and «Тула» for both «Тул» and «Тула».

    Legacy's companion ``у`` rule (bot.py:37443-37444) is deliberately
    NOT restored: «в»/«во» governs the prepositional, not the dative,
    so it never fires on the phrasing this function exists to serve,
    and every extra candidate is one more HTTP request on the miss
    path — the path that is already the slow one.

    Multi-word names are still rewritten on their last token only, so
    «Нижнем Новгороде» stays unresolved: «Нижний Новгород» needs
    adjective declension, which is not something a suffix table can do.
    """
    raw = (query or "").strip()
    if not raw:
        return []
    candidates = [raw]
    stripped = _LEADING_PREPOSITION_RE.sub("", raw).strip()
    candidates.append(stripped)
    candidates.append(nominalize_ru_city(stripped))
    candidates.append(_strip_oblique_e(stripped))
    out: list[str] = []
    seen: set[str] = set()
    for name in candidates:
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def extract_city_from_time_query(text: str | None) -> str | None:
    """City out of "время в X" phrases. Port of ``bot.py:37736``."""
    if not text or not text.strip():
        return None
    low = text.strip().lower()
    for pattern in (
        r"(?:сколько\s+)?(?:сейчас\s+)?времени?\s+в\s+([^?!.,]+?)\s*[?!.]*$",
        r"который\s+час\s+в\s+([^?!.,]+?)\s*[?!.]*$",
        r"время\s+в\s+([^?!.,]+?)\s*[?!.]*$",
        r"в\s+([^?!.,]+?)\s+сейчас\s+время",
    ):
        m = re.search(pattern, low, re.IGNORECASE)
        if m:
            city = m.group(1).strip().strip("?!.,")
            if len(city) > 1 and city not in _TIME_PLACEHOLDERS:
                return city
    return None
