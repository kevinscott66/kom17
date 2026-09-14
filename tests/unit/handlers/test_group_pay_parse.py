"""Pure-parsing pins for ``handlers/group_pay.py`` (L-28/L-41).

* :func:`parse_amount` is a verbatim port of the legacy first-digit-token
  scan (bot.py:25041-25046) — pin the token-shape decisions so a future
  "parse the SECOND arg" refactor can't silently change which number a
  ``/group_pay @user 200`` style message withdraws.
* :func:`_foreign_target_id` mirrors ``_resolve_target_user_from_message``
  (bot.py:22712-22719): reply target first, then ``text_mention``
  entities; plain ``@username`` mentions resolve to nobody.
"""

from __future__ import annotations

from types import SimpleNamespace

from telegram_invite_bot.handlers.group_pay import _foreign_target_id, parse_amount

_CALLER = 42


# ── parse_amount ────────────────────────────────────────────────────


def test_bare_command_has_no_amount() -> None:
    assert parse_amount("/group_pay") is None


def test_simple_amount() -> None:
    assert parse_amount("/group_pay 5000") == 5000


def test_first_digit_token_wins() -> None:
    # Legacy scans tokens in order — the 200 wins over the 999.
    assert parse_amount("/group_pay 200 999") == 200


def test_username_token_skipped() -> None:
    assert parse_amount("/group_pay @someone 1500") == 1500


def test_negative_and_decimal_are_not_digit_tokens() -> None:
    # ``str.isdigit`` rejects the sign / dot — same as legacy.
    assert parse_amount("/group_pay -500") is None
    assert parse_amount("/group_pay 5.5") is None


def test_empty_text() -> None:
    assert parse_amount("") is None


# ── _foreign_target_id ──────────────────────────────────────────────


def _msg(*, reply_uid: int | None = None, entities: list | None = None):  # noqa: ANN202
    reply = None
    if reply_uid is not None:
        reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_uid))
    return SimpleNamespace(reply_to_message=reply, entities=entities)


def test_no_target() -> None:
    assert _foreign_target_id(_msg(), _CALLER) is None


def test_reply_to_other_is_foreign() -> None:
    assert _foreign_target_id(_msg(reply_uid=99), _CALLER) == 99


def test_reply_to_self_is_not_foreign() -> None:
    assert _foreign_target_id(_msg(reply_uid=_CALLER), _CALLER) is None


def test_text_mention_of_other_is_foreign() -> None:
    ent = SimpleNamespace(type="text_mention", user=SimpleNamespace(id=7))
    assert _foreign_target_id(_msg(entities=[ent]), _CALLER) == 7


def test_text_mention_of_self_is_not_foreign() -> None:
    ent = SimpleNamespace(type="text_mention", user=SimpleNamespace(id=_CALLER))
    assert _foreign_target_id(_msg(entities=[ent]), _CALLER) is None


def test_plain_mention_entity_resolves_to_nobody() -> None:
    # ``@username`` entities carry no user object — legacy ignores them
    # (bot.py:22716-22718 checks text_mention only).
    ent = SimpleNamespace(type="mention", user=None)
    assert _foreign_target_id(_msg(entities=[ent]), _CALLER) is None
