"""End-to-end ``/nick``.

Pins:

* ``/nick Some Name`` in a group → row upserted, success reply.
* Bare ``/nick`` in a group → row deleted (if any), "cleared" reply.
* Bare ``/nick`` with no prior row → still "cleared" reply (DELETE
  is idempotent). Symmetric with legacy's ``set_user_group_nickname``
  reset path.
* 100-char clamp on the stored name (legacy slice at bot.py:24854).
* Aliases ``setnick`` / ``ник`` / ``никнейм`` all route to the same
  handler.
* Private DM → falls through to legacy (router-level group filter).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User, UserGroupNickname
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.nick import NICK_MAX_LEN
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import (
    _try_capture_send,
    assert_chat_scope_refusal,
    make_message_update,
)

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CHAT_ID = -100_777
_USER_ID = 5151


async def _seed_user(registry: EngineRegistry, lang: str = "ru") -> None:
    """Pre-create the user so ``UserService.touch`` reads our pinned
    language instead of upserting from the Telegram-supplied code."""
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=_USER_ID, first_name="N", language_code=lang))
        await session.commit()


async def _seed_existing_nick(registry: EngineRegistry, name: str) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(UserGroupNickname(user_id=_USER_ID, chat_id=_CHAT_ID, display_name=name))
        await session.commit()


async def _read_nick(registry: EngineRegistry) -> str | None:
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        row = await conn.execute(
            select(UserGroupNickname.display_name).where(
                UserGroupNickname.user_id == _USER_ID,
                UserGroupNickname.chat_id == _CHAT_ID,
            )
        )
        return row.scalar_one_or_none()


def _group_update(text: str, *, lang: str = "ru") -> Any:
    return make_message_update(
        text,
        user_id=_USER_ID,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        language_code=lang,
    )


@pytest.mark.asyncio
async def test_nick_sets_new(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _group_update("/nick КрутойНик"))

    assert result is not UNHANDLED
    assert await _read_nick(registry) == "КрутойНик"
    assert sent[-1]["text"] == t("nick_set", "ru", name="КрутойНик")


@pytest.mark.asyncio
async def test_nick_overwrites_existing(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Upsert path — the row existed, the new value must replace it.
    Pins the ``ON CONFLICT DO UPDATE`` branch independently from the
    INSERT path tested above."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    await _seed_existing_nick(registry, "OldName")
    _ = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _group_update("/nick NewName"))

    assert await _read_nick(registry) == "NewName"


@pytest.mark.asyncio
async def test_nick_bare_clears_existing(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    await _seed_existing_nick(registry, "ToBeCleared")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _group_update("/nick"))

    assert await _read_nick(registry) is None
    assert sent[-1]["text"] == t("nick_cleared", "ru")


@pytest.mark.asyncio
async def test_nick_bare_clears_when_no_row(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Idempotent reset — bare ``/nick`` when nothing's stored still
    replies success. Matches legacy's "DELETE missing row = ok" path."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _group_update("/nick"))

    assert await _read_nick(registry) is None
    assert sent[-1]["text"] == t("nick_cleared", "ru")


@pytest.mark.asyncio
async def test_nick_clamps_to_max_length(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Legacy clamps at 100 chars before INSERT. We mirror that — a
    150-char input lands as a 100-char row, not 150 (the column is TEXT
    so the DB wouldn't reject; the clamp is a UX policy, not a schema
    constraint)."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    _ = capture_outgoing(bot)

    overlong = "Z" * 150
    await dispatcher.feed_update(bot, _group_update(f"/nick {overlong}"))

    stored = await _read_nick(registry)
    assert stored is not None
    assert len(stored) == NICK_MAX_LEN
    assert stored == "Z" * NICK_MAX_LEN


@pytest.mark.asyncio
async def test_nick_confirmation_escapes_html(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The nick is raw user text and the bot sends parse_mode=HTML.

    Unescaped, ``/nick <a href="…">Поддержка</a>`` made the BOT post a
    live link into the group under its own name — a phishing seam — and
    a lone ``<`` broke entity parsing so Telegram rejected the reply.
    The STORED value keeps the raw characters (the read side escapes at
    render); only the outgoing confirmation is escaped.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    hostile = '<a href="https://evil.example">Поддержка</a>'
    await dispatcher.feed_update(bot, _group_update(f"/nick {hostile}"))

    assert await _read_nick(registry) == hostile
    body = sent[-1]["text"]
    assert "<a href=" not in body
    assert "&lt;a href=" in body


@pytest.mark.asyncio
async def test_nick_send_failure_is_not_reported_as_a_save_failure(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed *delivery* must not masquerade as a failed *save*.

    The ``try`` used to wrap the reply as well, so a Telegram hiccup on
    the confirmation ran the ``except`` branch and told the user
    "couldn't save" — while the write was about to commit. They'd retry
    a nick that was already stored. Now the reply lives outside the
    try: the send error propagates, the session middleware rolls the
    write back, the errors router owns the apology, and the user
    retries a genuinely unsaved nick.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)

    seen: list[str] = []

    async def failing_first_send(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        seen.append(getattr(method, "text", ""))
        if len(seen) == 1:
            raise RuntimeError("telegram is down")
        # Any follow-up send succeeds — so a second reply (the old
        # "couldn't save" one) would be visible in ``seen`` rather than
        # hidden behind the same failure.
        return _try_capture_send(method, [])

    monkeypatch.setattr(bot.session, "make_request", failing_first_send)

    with contextlib.suppress(RuntimeError):
        await dispatcher.feed_update(bot, _group_update("/nick Boom"))

    assert seen[0] == t("nick_set", "ru", name="Boom")
    # Whatever the errors router says afterwards, it must not be the
    # "couldn't save" line — that one is reserved for a real DB failure.
    assert t("nick_error", "ru") not in seen
    # And the write really is gone: the middleware rolled it back, so a
    # retry is honest work rather than a no-op on an already-saved row.
    assert await _read_nick(registry) is None


@pytest.mark.asyncio
async def test_nick_strips_bidi_and_zero_width(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Bidi-override + zero-width chars are visual-impersonation vectors.
    They must be stripped before storage so a stored nick can't render
    as someone else's name (or duplicate one) in /top and /profile."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    _ = capture_outgoing(bot)

    # U+202E (RLO) + U+200B (ZWSP) + U+2066 (LRI) wrapped around "admin".
    hostile = chr(0x202E) + "adm" + chr(0x200B) + "in" + chr(0x2066)
    await dispatcher.feed_update(bot, _group_update(f"/nick {hostile}"))

    assert await _read_nick(registry) == "admin"


@pytest.mark.asyncio
async def test_nick_all_control_chars_clears(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A nick made *only* of invisible control chars collapses to empty
    and is treated as a clear — never stored as an all-invisible row."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    await _seed_existing_nick(registry, "Visible")
    sent = capture_outgoing(bot)

    only_controls = chr(0x200B) + chr(0x202E) + chr(0x2066)
    await dispatcher.feed_update(bot, _group_update(f"/nick {only_controls}"))

    assert await _read_nick(registry) is None
    assert sent[-1]["text"] == t("nick_cleared", "ru")


@pytest.mark.asyncio
async def test_nick_aliases_route(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Each alias must reach the same handler. Pins the ``Command(...)``
    alias list against accidental trimming."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    for alias, expected in (
        ("/setnick A", "A"),
        ("/ник B", "B"),
        ("/никнейм C", "C"),
    ):
        await dispatcher.feed_update(bot, _group_update(alias))
        assert await _read_nick(registry) == expected
    # Each invocation produced one reply.
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_nick_private_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A DM ``/nick`` is answered, and still sets no nickname (#123).

    The router-level group filter keeps the worker from handling a DM;
    what changed is that the refusal twin now says "only in a group"
    instead of leaving the user with silence.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_message_update("/nick X", user_id=_USER_ID, chat_type="private"),
    )

    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="nick")
