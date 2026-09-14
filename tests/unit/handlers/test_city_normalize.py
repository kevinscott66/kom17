"""``normalize_city`` — the gate between a chat message and a DB column
(RR-6 #74).

Every value this function returns is later rendered into an HTML message
and stored for the lifetime of the account, so it is worth its own pure
test file. Legacy did ``city.strip()`` and nothing else (bot.py:4433),
which is how a profile card could end up carrying a paragraph of markup.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.city import MAX_CITY_LENGTH, normalize_city


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Краснодар", "Краснодар"),
        ("  Москва  ", "Москва"),
        ("Санкт-Петербург", "Санкт-Петербург"),
        ("Ростов-на-Дону", "Ростов-на-Дону"),
        ("New York", "New York"),
        ("Frankfurt am Main", "Frankfurt am Main"),
        ("St. Petersburg", "St. Petersburg"),
        ("Sant'Angelo", "Sant'Angelo"),
        ("Nur-Sultan (Astana)", "Nur-Sultan (Astana)"),
        ("Комсомольск-на-Амуре, Хабаровский край", "Комсомольск-на-Амуре, Хабаровский край"),
    ],
)
def test_real_place_names_survive(raw: str, expected: str) -> None:
    assert normalize_city(raw) == expected


def test_internal_whitespace_collapses() -> None:
    """A pasted name brings its newlines and double spaces along; the
    stored value must be one line, because it lands mid-sentence in the
    confirmation card."""
    assert normalize_city("Нижний\n\tНовгород") == "Нижний Новгород"
    assert normalize_city("New    York") == "New York"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "123",  # no letters — not a city
        "---",
        "42 42",
        "<b>Москва</b>",  # markup
        "Москва<br>",
        "Москва & Питер",
        "https://example.com",  # the thing people actually paste
        "Москва 🌍",  # emoji
        "город_родной",  # underscore is not place-name punctuation
        "Москва; DROP TABLE user_settings",
    ],
)
def test_junk_is_refused(raw: str) -> None:
    assert normalize_city(raw) is None


def test_length_is_capped_after_collapsing() -> None:
    """The limit applies to what would be *stored*, so whitespace a user
    padded with doesn't count against them."""
    assert normalize_city("а" * MAX_CITY_LENGTH) == "а" * MAX_CITY_LENGTH
    assert normalize_city("а" * (MAX_CITY_LENGTH + 1)) is None
    padded = "  " + "а" * MAX_CITY_LENGTH + "  "
    assert normalize_city(padded) == "а" * MAX_CITY_LENGTH


def test_no_accepted_value_can_carry_html() -> None:
    """Belt to the ``html.escape`` suspenders: nothing that survives may
    contain a character with meaning under ``parse_mode=HTML``."""
    candidates = [
        "<script>alert(1)</script>",
        "Москва<b>",
        "A & B",
        'Say "hi"',
        "Москва'>",
    ]
    for raw in candidates:
        accepted = normalize_city(raw)
        if accepted is not None:  # pragma: no cover - defensive
            assert not set(accepted) & set('<>&"')
