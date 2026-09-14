"""``/give`` argument parser — pure-logic unit tests (L-24).

The handler's wallet/credit path is exercised by the economy_service +
economy_repo integration tests; here we pin the three target-form parser
that decides whether the gift goes to a @username, a numeric id, or a
reply target, plus its rejection cases.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.admin.give import _parse_target_and_amount


@pytest.mark.parametrize(
    ("raw", "has_reply", "expected"),
    [
        # @username form.
        ("@alice 100", False, ("alice", None, 100)),
        # numeric id form.
        ("12345 50", False, (None, 12345, 50)),
        # reply form (bare amount, reply present).
        ("100", True, (None, None, 100)),
        # explicit target wins even when a reply is present.
        ("777 25", True, (None, 777, 25)),
        ("@bob 25", True, ("bob", None, 25)),
        # negative amount parses here (range check is the handler's job).
        ("12345 -5", False, (None, 12345, -5)),
        # #1595 narrowed the grammar; an explicit sign was accepted by
        # the bare int() before and still is, so nothing regressed.
        ("12345 +100", False, (None, 12345, 100)),
    ],
)
def test_parse_ok(raw: str, has_reply: bool, expected: tuple[str | None, int | None, int]) -> None:
    assert _parse_target_and_amount(raw, has_reply=has_reply) == expected


@pytest.mark.parametrize(
    ("raw", "has_reply"),
    [
        ("", False),  # no args
        ("   ", False),  # whitespace only
        ("100", False),  # bare amount, NO reply → ambiguous, rejected
        ("@alice abc", False),  # non-int amount
        ("12345 abc", False),  # non-int amount
        ("@ 100", False),  # bare @ token
        ("notanid 100", False),  # non-numeric, non-@ target
        # #1595: exotic integer spellings that bare int() accepts.
        # This command MINTS coins, so the audit-log string and the
        # credited number have to be the same number.
        ("12345 １００", False),  # fullwidth amount
        ("12345 ١٠٠", False),  # arabic-indic amount
        ("12345 1_00", False),  # underscore separator in amount
        ("１２３４５ 100", False),  # fullwidth numeric target
        ("12_345 100", False),  # underscore separator in target
        ("１００", True),  # fullwidth amount, reply form
        ("1_00", True),  # underscore separator, reply form
    ],
)
def test_parse_rejects(raw: str, has_reply: bool) -> None:
    assert _parse_target_and_amount(raw, has_reply=has_reply) is None


def test_bare_int_would_have_accepted_the_exotic_spellings() -> None:
    """The reason #1595 is a fix and not a style change.

    Every token below is a plain ``int()`` success, so before the
    change ``/give 12345 １００`` credited one hundred coins from a
    string no operator reading the ledger would recognise as 100.
    Pinned here so the rejection list above cannot be dismissed as
    testing tokens nothing ever produced."""
    assert int("１００") == 100
    assert int("١٠٠") == 100
    assert int("1_00") == 100
    assert int("１２３４５") == 12345
