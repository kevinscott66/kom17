"""End-to-end ``/timezone`` (Stage 27).

What this proves:

* All four legacy aliases (``timezone``, ``часовой_пояс``, ``tz``,
  ``kom_timezone``) route in private chats.
* Bare ``/timezone`` for a user with no stored value shows the help
  blurb (and does NOT crash on ``None`` from the repo).
* A valid IANA name (``Europe/Moscow``) persists into
  ``user_settings.timezone`` AND the confirmation echoes it.
* A nonsense name (``Mars/Olympus``) is rejected without writing.
* ``/timezone reset`` (and its Russian / English synonyms) clears the
  stored value — verified by reading the repo back, not just the
  confirmation text.
* Bare ``/timezone`` for a user *with* a stored value formats the
  current local time (asserted on shape, not exact value — the test
  can run at any wall-clock moment).
* Group ``/timezone`` renders too (parity with legacy, which is not
  chat-type gated).

The test does NOT exercise the strangler-bridge — only the new
pipeline's behaviour in isolation, like the other ported handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, chat_type: str = "private", user_id: int = 5151) -> Update:
    """File-local defaults: user 5151 named ``Tz`` (ru). Delegates to the
    shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Tz",
        language_code="ru",
    )


@pytest.mark.parametrize("alias", ["/timezone", "/часовой_пояс", "/tz", "/kom_timezone"])
async def test_aliases_render_help_when_unset(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """Bare ``/timezone`` with no stored tz → help blurb. Every alias
    must reach this branch so an existing legacy user's muscle memory
    still works post-migration.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Часовой пояс" in body
    assert "/timezone" in body  # the help blurb includes an example


async def test_valid_tz_persists_and_confirms(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/timezone Europe/Moscow"))
    assert result is not UNHANDLED
    assert "Europe/Moscow" in sent[-1]["text"]
    assert sent[-1]["text"].startswith("✅")

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        assert await repo.get_timezone(5151) == "Europe/Moscow"


async def test_invalid_tz_rejected_without_write(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A name that ZoneInfo can't resolve must (a) get an error reply
    and (b) NOT write anything to ``user_settings`` — otherwise a typo
    would corrupt the stored value silently.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/timezone Mars/Olympus"))
    assert result is not UNHANDLED
    assert sent[-1]["text"].startswith("❌")
    # Echoes the bad input back for clarity.
    assert "Mars/Olympus" in sent[-1]["text"]

    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        repo = UserSettingsRepo(session)
        assert await repo.get_timezone(5151) is None


@pytest.mark.parametrize("reset_token", ["reset", "сброс", "clear", "удалить", "Reset", "СБРОС"])
async def test_reset_clears_stored_value(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    reset_token: str,
) -> None:
    """All four reset synonyms (case-insensitive) must clear the row.
    Legacy accepts the same set — parity matters for user habits.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    capture_outgoing(bot)

    # Pre-seed a tz so reset has something to clear.
    await dispatcher.feed_update(bot, _update("/timezone Europe/Moscow"))
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        assert await UserSettingsRepo(session).get_timezone(5151) == "Europe/Moscow"

    result = await dispatcher.feed_update(bot, _update(f"/timezone {reset_token}"))
    assert result is not UNHANDLED

    async with sessionmaker() as session:
        # Empty string is the wire format legacy uses for "cleared";
        # ``get_timezone`` normalises that to ``None`` for callers.
        assert await UserSettingsRepo(session).get_timezone(5151) is None


async def test_show_renders_local_time_when_set(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """After setting a tz, the bare-command branch should format the
    current local time. We assert on structural markers (HH:MM:SS shape
    + weekday word) rather than the actual time string, so the test
    isn't time-sensitive.
    """
    import re

    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/timezone Europe/Moscow"))
    sent.clear()
    result = await dispatcher.feed_update(bot, _update("/timezone"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Текущий часовой пояс" in body
    assert "Europe/Moscow" in body
    # HH:MM:SS shape.
    assert re.search(r"\d{2}:\d{2}:\d{2}", body)
    # One of the Russian weekday names (legacy always rendered RU here).
    assert any(
        wd in body
        for wd in (
            "понедельник",
            "вторник",
            "среда",
            "четверг",
            "пятница",
            "суббота",
            "воскресенье",
        )
    )


async def test_show_warns_when_stored_tz_no_longer_resolves(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user can have a previously-valid tz stored that the system's
    ``tzdata`` later dropped (Debian retired ``US/Pacific-New`` in 2022,
    similar churn happens every release). The handler MUST surface
    "больше не распознаётся" rather than silently rendering only the
    raw stored name — the user wouldn't know why the time line vanished.

    We patch :func:`format_local_time` to simulate the "tzdata can't
    resolve this any more" return shape (three empty strings — locked
    by ``utils/time.py``). Persisting an unparseable name through
    ``/timezone`` itself is impossible because the same validator
    rejects it on write.
    """
    from telegram_invite_bot.handlers import timezone as timezone_module

    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    # Seed a real, currently-valid tz so the unset-help branch doesn't fire.
    await dispatcher.feed_update(bot, _update("/timezone Europe/Moscow"))
    sent.clear()

    # Now poison the resolver: pretend Europe/Moscow vanished from tzdata.
    monkeypatch.setattr(timezone_module, "format_local_time", lambda _tz, _lang="ru": ("", "", ""))

    result = await dispatcher.feed_update(bot, _update("/timezone"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    # User sees both the stored name (so they know what to replace) and
    # the explicit "no longer recognised" copy.
    assert "Europe/Moscow" in body
    assert "не распознаётся" in body


async def test_group_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Group ``/timezone`` renders the help blurb — parity with legacy
    ``cmd_timezone`` (bot.py:16888), which has no ``require_group_feature``
    gate and answers in groups too.

    REGRESSION PIN: after the legacy telebot bridge was deleted, a
    router-level PRIVATE filter turned group ``/timezone`` into a silent
    dead-end. A timezone is a per-user setting, so a group invocation is
    meaningful — it just sets the author's own zone.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/timezone", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert "Часовой пояс" in sent[0]["text"]


# --- I18N-1: English-language coverage --------------------------------------


def _update_en(text: str, *, user_id: int = 7272) -> Update:
    """English-tagged /timezone update (user 7272)."""
    return make_message_update(
        text,
        chat_type="private",
        user_id=user_id,
        first_name="Tz",
        language_code="en",
    )


async def test_english_unset_help_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/timezone`` for an ``en`` user → English help, no Cyrillic."""
    import re

    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_en("/timezone"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert not re.search("[А-Яа-яЁё]", body), body
    assert "/timezone" in body


async def test_english_set_and_reset_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Set + reset confirmations for an ``en`` user are English-only."""
    import re

    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update_en("/timezone Europe/Moscow"))
    set_body = sent[-1]["text"]
    assert not re.search("[А-Яа-яЁё]", set_body), set_body
    assert "Europe/Moscow" in set_body

    await dispatcher.feed_update(bot, _update_en("/timezone reset"))
    reset_body = sent[-1]["text"]
    assert not re.search("[А-Яа-яЁё]", reset_body), reset_body


async def test_english_invalid_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An unrecognized tz for an ``en`` user → English error, no Cyrillic."""
    import re

    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_en("/timezone Mars/Olympus"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert not re.search("[А-Яа-яЁё]", body), body
    assert "Mars/Olympus" in body
