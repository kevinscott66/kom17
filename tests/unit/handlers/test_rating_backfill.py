"""Pure-function guards for the RR-1 #9 invite-link backfill.

The e2e tests drive the backfill through a whole dispatcher and so cover
the happy path; the two things worth pinning in isolation are the href
allow-list (which decides what the leaderboard is willing to make
clickable) and the cooldown bookkeeping (which is the only thing standing
between a render path and a per-view pair of Telegram API calls for a
group that will never answer).
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.rating import (
    _BACKFILL_BLOCKED,
    _blocked_until,
    _invite_link,
    _name_display,
    _ranked,
    _RatingRow,
)


@pytest.fixture(autouse=True)
def _clear_cooldown() -> None:
    _BACKFILL_BLOCKED.clear()


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/+ExampleInviteHash2",
        "https://t.me/joinchat/abc",
        "https://t.me/some_public_group",
        "http://telegram.me/joinchat/abc",
        "https://telegram.dog/joinchat/abc",
        "https://T.ME/joinchat/abc",  # host comparison is case-insensitive
    ],
)
def test_telegram_invite_urls_are_accepted(raw: str) -> None:
    assert _invite_link(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "javascript:alert(1)",
        "https://t-me.example.com/joinchat/abc",  # look-alike host
        "https://evil.example/t.me/joinchat/abc",  # t.me only in the path
        "t.me/joinchat/abc",  # no scheme → urlsplit sees no host
        "https://t.me@evil.example/x",  # userinfo trick: real host is evil
        "https://t.me\\@evil.example/x",  # same, with a backslash
        # The inverse, and the nastier one: ``urlsplit`` reports the host
        # as ``t.me`` here, but a WHATWG parser (i.e. every browser and
        # Telegram client that would follow the anchor) treats ``\`` as
        # ``/`` and lands on ``evil.com``. Rejected because the userinfo
        # check fires first — ``parsed.username`` is ``evil.com\``.
        "https://evil.com\\@t.me/x",
        "https://user:pw@t.me/x",  # right host, but carries credentials
        "https://t.mе/joinchat/abc",  # Cyrillic е — IDN homograph
        "https://t.me./joinchat/abc",  # trailing dot ≠ the literal host
        "https://t.me%2eevil.example/x",  # percent-encoded dot
        "//t.me/joinchat/abc",  # scheme-relative
    ],
)
def test_anything_that_is_not_a_telegram_url_is_rejected(raw: str | None) -> None:
    """A rejected value is indistinguishable from a missing one.

    ``group_link`` is written by the still-live legacy monolith as well as
    by us, and every value in it becomes an ``href`` shown to whoever ran
    ``/rating``. Falling back to an unlinked name is the safe failure.
    """
    assert _invite_link(raw) is None


def test_a_rejected_link_renders_as_plain_text() -> None:
    row = _RatingRow(group_id=-100, group_name="Клуб", group_link="javascript:alert(1)", xp=5)
    assert _name_display(row, trunc=40) == "Клуб"


def test_a_group_without_a_name_falls_back_to_its_id() -> None:
    """Prod's top-ranked group has no stored name — the fallback is what
    the board actually shows until the backfill runs."""
    row = _RatingRow(group_id=-1002222222222, group_name=None, group_link=None, xp=645)
    assert _name_display(row, trunc=40) == "-1002222222222"


def test_a_name_is_escaped_before_it_reaches_the_anchor() -> None:
    row = _RatingRow(group_id=-100, group_name="<b>Клуб</b>", group_link="https://t.me/x", xp=5)
    rendered = _name_display(row, trunc=40)
    assert rendered == '<a href="https://t.me/x">&lt;b&gt;Клуб&lt;/b&gt;</a>'


def test_a_marked_group_is_blocked_until_its_deadline() -> None:
    _BACKFILL_BLOCKED[-100] = 500.0
    assert _blocked_until(-100, now=499.0) is True
    assert _blocked_until(-101, now=499.0) is False


def test_an_expired_mark_unblocks_and_is_pruned() -> None:
    """The cooldown map must not grow forever — it is keyed by group id
    and lives for the life of the process."""
    _BACKFILL_BLOCKED[-100] = 500.0
    assert _blocked_until(-100, now=500.0) is False
    assert _BACKFILL_BLOCKED == {}


def test_pruning_only_drops_the_entries_that_expired() -> None:
    _BACKFILL_BLOCKED.update({-100: 100.0, -101: 900.0})
    assert _blocked_until(-101, now=500.0) is True
    assert set(_BACKFILL_BLOCKED) == {-101}


def test_the_ranked_predicate_still_treats_a_null_flag_as_included() -> None:
    """``in_rating`` is ``NOT NULL DEFAULT 1``, so no schema this suite can
    build will hand the read side a NULL — which is exactly why the branch
    needs pinning here rather than through a fixture.

    It is not dead code. Prod's ``groups_donations`` predates the column,
    and SQLite's ``NULL != 0`` evaluates to NULL rather than true, so
    dropping the branch would silently delete every legacy row from the
    leaderboard instead of ranking it.
    """
    sql = str(_ranked().compile(compile_kwargs={"literal_binds": True}))
    assert "in_rating IS NULL" in sql
    assert "in_rating != 0" in sql
