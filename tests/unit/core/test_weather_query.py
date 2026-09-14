"""Pure parsers behind the weather / time surfaces (RR-6 #68, #69).

These live in :mod:`telegram_invite_bot.core.weather_query` precisely so
they can be tested without a service, a client, or a dispatcher. The
cases below are the phrasings legacy answered plus the ones that broke
in production (oblique-case city names silently failing to geocode).
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.weather_query import (
    detect_period,
    extract_city_from_time_query,
    geocode_variants,
    is_weather_query,
    nominalize_ru_city,
    normalize_weather_location,
    strip_period_tokens,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("погода", True),
        ("какая погода в Казани", True),
        ("что по погоде", True),
        ("скажи погоду в Магадане", True),
        ("weather in Berlin", True),
        # Near-misses that must NOT trigger a weather card.
        ("погоди немного", False),
        ("привет всем", False),
        ("", False),
        (None, False),
    ],
)
def test_is_weather_query(text: str | None, expected: bool) -> None:
    assert is_weather_query(text) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Москва", "Москва"),
        ("погода Москва", "Москва"),
        ("какая погода в Казани", "Казани"),
        ("погода завтра в Сочи", "Сочи"),
        ("weather in Berlin", "Berlin"),
        ("погода на 3 дня в Уфе", "Уфе"),
        # Placeholders mean "my own city" — empty signals the caller to
        # fall back to the stored /city rather than to give up.
        ("погода в моём городе", ""),
        ("погода у меня", ""),
        ("погода", ""),
    ],
)
def test_normalize_weather_location(raw: str, expected: str) -> None:
    assert normalize_weather_location(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Москва", ("today", 1)),
        ("погода завтра", ("tomorrow", 2)),
        ("weather tomorrow", ("tomorrow", 2)),
        ("погода на неделю", ("range", 7)),
        ("weather for a week", ("range", 7)),
        ("погода на 3 дня", ("range", 3)),
        ("forecast for 5 days", ("range", 5)),
        # A one-day "range" is just today with extra words.
        ("погода на 1 день", ("today", 1)),
        # Clamped to Open-Meteo's free horizon.
        ("погода на 30 дней", ("range", 10)),
        (None, ("today", 1)),
    ],
)
def test_detect_period(raw: str | None, expected: tuple[str, int]) -> None:
    assert detect_period(raw) == expected


def test_strip_period_tokens_leaves_the_city() -> None:
    assert strip_period_tokens("завтра в Сочи") == "в Сочи"
    assert strip_period_tokens("на 3 дня Уфа") == "Уфа"
    assert strip_period_tokens("Москва") == "Москва"


@pytest.mark.parametrize(
    ("oblique", "nominative"),
    [
        ("Москве", "Москва"),
        ("Казани", "Казань"),
        ("Твери", "Тверь"),
        ("Калуге", "Калуга"),
        ("Петербурге", "Петербург"),
        # Already nominative / non-Cyrillic → untouched.
        ("Москва", "Москва"),
        ("Berlin", "Berlin"),
        ("", ""),
    ],
)
def test_nominalize_ru_city(oblique: str, nominative: str) -> None:
    assert nominalize_ru_city(oblique) == nominative


def test_geocode_variants_tries_the_raw_spelling_first() -> None:
    """The heuristic must never get a chance to mangle a name that
    would have resolved as typed — that ordering is the whole safety
    argument for having a heuristic at all."""
    variants = geocode_variants("в Москве")
    assert variants[0] == "в Москве"
    assert "Москве" in variants
    assert "Москва" in variants


def test_geocode_variants_dedupes_and_handles_empty() -> None:
    assert geocode_variants("Berlin") == ["Berlin"]
    assert geocode_variants("   ") == []


@pytest.mark.parametrize(
    ("oblique", "nominative"),
    [
        ("в Новосибирске", "Новосибирск"),
        ("во Владивостоке", "Владивосток"),
        ("в Воронеже", "Воронеж"),
        ("в Омске", "Омск"),
        # Soft-sign masculine: the stem the geocoder resolves is «Ярославл»,
        # which Open-Meteo answers with «Ярославль». A suffix table cannot
        # tell this apart from «Туле» → «Тула», so both spellings are
        # emitted and the geocoder's fuzzy match settles it.
        ("в Ярославле", "Ярославл"),
    ],
)
def test_geocode_variants_offers_the_masculine_nominative(oblique: str, nominative: str) -> None:
    """#1072: ``nominalize_ru_city`` alone only ever yields the feminine.

    Its generic ``е`` → ``а`` rule turns «Новосибирске» into the GENITIVE
    «Новосибирска». Measured against the live Open-Meteo endpoint, that
    spelling and the oblique one both return zero results while
    «Новосибирск» resolves — so the whole family of masculine city names
    answered "city not found". The legacy rule this module's docstring
    dismissed (bot.py:37441-37442) is the one that covers them.
    """
    assert nominative in geocode_variants(oblique)


def test_geocode_variants_still_offers_the_feminine_nominative() -> None:
    """The #1072 fix ADDS a candidate; it must not displace the old one."""
    assert "Москва" in geocode_variants("в Москве")
    assert "Тула" in geocode_variants("в Туле")


def test_geocode_variants_leaves_short_and_latin_names_alone() -> None:
    """The length floor is legacy's, and it is what keeps «Уфе» intact."""
    assert "Уф" not in geocode_variants("в Уфе")
    assert geocode_variants("Berlin") == ["Berlin"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("сколько сейчас времени в Лондоне", "лондоне"),
        ("который час в Токио", "токио"),
        ("время в Париже", "париже"),
        ("время в моём", None),
        ("просто текст", None),
        (None, None),
    ],
)
def test_extract_city_from_time_query(text: str | None, expected: str | None) -> None:
    assert extract_city_from_time_query(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Legacy's noun was ``дн(?:я|ей)?`` (bot.py:37486), so the bare
        # stump «дн» that people type when abbreviating was answered.
        # The port's alternation dropped it and silently fell back to
        # the default horizon (#505).
        ("погода Москва на 5 дн", ("range", 5)),
        ("погода Москва на 3 дня", ("range", 3)),
        ("погода Москва на 4 дней", ("range", 4)),
        # Port-only spellings, kept: legacy had neither.
        ("погода Москва на 2 день", ("range", 2)),
        ("weather Moscow for 6 days", ("range", 6)),
        # A one-day range is still today, stump or not.
        ("погода Москва на 1 дн", ("today", 1)),
    ],
)
def test_detect_period_accepts_the_legacy_day_stump(text: str, expected: tuple[str, int]) -> None:
    assert detect_period(text) == expected


def test_day_stump_is_stripped_from_the_place() -> None:
    """Whatever ``detect_period`` matches, ``strip_period_tokens`` has to
    remove — otherwise «дн» reaches the geocoder as part of the city."""
    assert strip_period_tokens("Москва на 5 дн") == "Москва"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("погода в Москве в выходные", "Москве"),
        ("погода в Санкт-Петербурге в среду", "Санкт-Петербурге"),
        ("погода на выходных в Казани", "Казани"),
        ("погода в Уфе в понедельник", "Уфе"),
        # The split these guard is still what rescues the phrasing it
        # was written for: «сейчас» is not a period word, so only the
        # split hands back «Москве».
        ("ком какая погода сейчас в Москве", "Москве"),
        ("погода завтра в Сочи", "Сочи"),
    ],
)
def test_a_trailing_period_word_does_not_eat_the_city(text: str, expected: str) -> None:
    """#949: the second « в » split handed the geocoder «выходные» and
    the user got "city not found" for a message that named the city
    unambiguously — plus one wasted upstream request per message."""
    assert normalize_weather_location(text) == expected


def test_time_and_weather_agree_on_own_city_placeholders() -> None:
    """#955: «в моем городе» short-circuited to the stored ``/city`` on
    ``/weather`` and went to the geocoder verbatim on ``/time``."""
    assert normalize_weather_location("погода в моем городе") == ""
    assert extract_city_from_time_query("время в моем городе") is None
    assert extract_city_from_time_query("время в Москве") == "москве"


@pytest.mark.parametrize(
    "text",
    [
        "время в твоем городе",
        "время в твоём городе",
        "сколько времени в твой город",
    ],
)
def test_second_person_own_city_placeholders_are_recognised(text: str) -> None:
    """#1075: #955 fixed the first person and left the second behind.

    ``_TIME_PLACEHOLDERS`` carried only the BARE «твоём»/«твоем» while the
    first-person set it derives from carries bare and full alike, so
    «время в твоем городе» stripped to «твоем городе» and travelled on as
    if it named a place.
    """
    assert extract_city_from_time_query(text) is None
