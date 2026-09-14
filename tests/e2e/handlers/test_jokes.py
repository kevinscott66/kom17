"""End-to-end ``/joke18`` (Stage 23).

What's worth proving:

* Each of the four aliases routes in private chat and produces a
  non-empty reply with the 18+ header.
* RU users see the RU header, EN users see the EN header — the
  language switch is keyed off ``users.language``, which the
  ``SessionMiddleware`` populates via ``UserService.touch``.
* Group ``/joke18`` falls through (UNHANDLED) so legacy's
  ``require_group_feature("ai")`` gate still runs there.
* ``/joke18 anything`` still renders — legacy ignores trailing args,
  so the new handler tolerates (and ignores) them too rather than
  silently dropping argful invocations.
* Body always comes from the local pool — fixed random seed lets us
  pin the exact joke without flaking on weekly pool edits.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
in conftest.py at Stage 24 — see that module for the rationale.
"""

from __future__ import annotations

import html
import random
import re
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update
from sqlalchemy import update

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User as DBUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.jokes import (
    _ADULT_PICKER,
    _JOKES_EN_ADULT,
    _JOKES_EN_SFW,
    _JOKES_RU_ADULT,
    _JOKES_RU_SFW,
    _SFW_PICKER,
)
from telegram_invite_bot.services.joke_service import JokeService
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(
    text: str,
    *,
    chat_type: str = "private",
    user_id: int = 333,
    language_code: str | None = "ru",
) -> Update:
    """File-local defaults: user 333, language ``ru``. Delegates to the
    shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        language_code=language_code,
    )


@pytest.fixture(autouse=True)
def _reset_joke_pickers() -> None:
    """The anti-repeat pickers are module-level singletons, so one test's
    pick narrows the next test's candidate list and the fixed-RNG
    assertions below would start depending on test order.
    """
    _ADULT_PICKER.clear()
    _SFW_PICKER.clear()


@pytest.mark.parametrize("alias", ["/joke18", "/шутка18", "/анекдот18", "/kom_joke18"])
async def test_joke18_aliases_route_in_private(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    # Seed RNG so the assertion below is deterministic — without this
    # a future pool edit could pick a string that happens to contain
    # an unexpected "header-ish" prefix and confuse the substring check.
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert body.startswith("🔞 18+")
    # The chosen joke must come from the actual pool — guard against
    # accidental hard-coding of a placeholder. The handler applies
    # ``html.escape`` to the body (defensive against the bot-wide HTML
    # parse_mode), so compare against the escaped form.
    assert html.escape(_JOKES_RU_ADULT[0]) in body


async def test_joke18_uses_english_header_for_en_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/joke18", language_code="en"))
    body = sent[0]["text"]
    assert "adults only" in body
    assert html.escape(_JOKES_EN_ADULT[0]) in body


async def test_joke_does_not_hold_the_write_lock_across_the_fetch(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/joke`` waits on a third-party API; users.db must stay writable.

    ``user_service.touch`` opens the update's write transaction, and
    ``BEGIN IMMEDIATE`` means one writer per DB until the middleware
    commits — which, without a checkpoint, is after the joke API has
    answered. Everyone else's update would spend ``busy_timeout`` (5 s)
    waiting and then fail with ``database is locked``. The probe below
    runs *inside* the stubbed fetch, exactly where a real request would
    be in flight.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    other_updates_could_write: list[bool] = []

    async def fetch_while_probing(_self: JokeService, _lang: str) -> str:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(
                update(DBUser).where(DBUser.user_id == 333).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return "ха-ха"

    monkeypatch.setattr(JokeService, "fetch", fetch_while_probing)

    await dispatcher.feed_update(bot, _update("/joke"))

    assert other_updates_could_write == [True]
    assert "ха-ха" in sent[0]["text"]


async def test_joke18_escapes_html_special_chars_in_pool(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: bot.py runs HTML parse_mode globally (app.py). Today's
    pool contains no `<` / `>` / `&` but a future editor could drop one
    in and silently break the envelope. ``html.escape`` on the body
    closes that door.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    # Swap in a synthetic punchline with every special char Telegram cares
    # about. Patch the underlying tuple — random.choice will pick the
    # only element.
    monkeypatch.setattr(
        "telegram_invite_bot.handlers.jokes._JOKES_RU_ADULT",
        ("<script>alert(1)</script> & friends",),
    )
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/joke18"))
    body = sent[0]["text"]
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp; friends" in body


async def test_joke18_answers_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #72: group ``/joke18`` answers.

    The private-only gate cited legacy's ``require_group_feature(message,
    "ai", ...)``, which can never deny — ``is_feature_enabled_for_chat``
    returns ``True`` in ``full`` mode and ``feature == "ai"`` in
    ``restricted`` mode, and no third mode is ever written. ``/cmdcfg``
    (min-rank 6) is the real per-group off-switch, and an 18+ command is
    exactly the kind a group owner would reach for it.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    result = await dispatcher.feed_update(
        bot, _update("/joke18", chat_type="supergroup", user_id=777)
    )
    assert result is not UNHANDLED
    assert sent[0]["text"].startswith("🔞 18+")


async def test_joke18_with_args_still_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/joke18 give me one`` must render the same card as the bare
    form. Legacy ``cmd_joke18`` (bot.py:17296) matches
    ``commands=['joke18', ...]`` regardless of trailing args and ignores
    them. After the legacy bridge was deleted, a ``magic=F.args.is_(None)``
    filter here turned argful invocations into a silent dead-end — a user
    who typed ``/joke18 please`` got nothing. The filter is removed so
    args are tolerated (and ignored), matching legacy.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/joke18 give me one"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert sent[0]["text"]


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@pytest.mark.parametrize("alias", ["/joke", "/шутка", "/анекдот", "/kom_joke"])
async def test_joke_aliases_route_in_private(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    """SFW ``/joke`` and its aliases route in private chat and render a
    BARE pool-pick — no 18+ header (only ``/joke18`` has one).
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    # Bare text: no 18+ header prefix from the adult command.
    assert not body.startswith("🔞 18+")
    assert body == html.escape(_JOKES_RU_SFW[0])


async def test_joke_uses_english_pool_for_en_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``en`` user gets an English-pool entry that contains NO Cyrillic."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/joke", language_code="en"))
    body = sent[0]["text"]
    assert body == html.escape(_JOKES_EN_SFW[0])
    assert not _CYRILLIC.search(body)


def test_joke_en_pool_has_no_cyrillic() -> None:
    """Every English SFW joke must be free of Cyrillic — guards against a
    copy-paste-from-RU regression in the pool itself.
    """
    offenders = [j for j in _JOKES_EN_SFW if _CYRILLIC.search(j)]
    assert not offenders, f"EN SFW pool has Cyrillic: {offenders}"


async def test_joke_answers_in_group(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #72: group ``/joke`` answers — same phantom-gate finding as
    ``/joke18`` above.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    result = await dispatcher.feed_update(
        bot, _update("/joke", chat_type="supergroup", user_id=778)
    )
    assert result is not UNHANDLED
    assert sent[0]["text"] == html.escape(_JOKES_RU_SFW[0])


async def test_joke_prefers_online_source(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #72: when the network source answers, its joke wins over the
    finite local pool — and is escaped, because a third party we have no
    contract with must never be able to inject markup into a message the
    bot sends with HTML parse mode.

    ``JokeService.fetch`` is patched rather than the transport so this
    test also covers the case where the whole service is disabled by
    ``JOKE_OFFLINE_ONLY`` (which the e2e conftest sets by default to keep
    the suite off the network); the fetch chain itself is unit-tested.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    async def _fake(self: JokeService, lang: str) -> str:
        return "Свежая шутка <b>из сети</b>"

    monkeypatch.setattr(JokeService, "fetch", _fake)

    await dispatcher.feed_update(bot, _update("/joke"))
    body = sent[0]["text"]
    assert body == html.escape("Свежая шутка <b>из сети</b>")
    assert "<b>" not in body


async def test_joke_falls_back_to_pool_when_online_source_is_down(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage of a free third-party humour API can never take down the
    command.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    async def _fake(self: JokeService, lang: str) -> None:
        return None

    monkeypatch.setattr(JokeService, "fetch", _fake)
    monkeypatch.setattr(random, "choice", lambda seq: seq[0])

    await dispatcher.feed_update(bot, _update("/joke"))
    assert sent[0]["text"] == html.escape(_JOKES_RU_SFW[0])
