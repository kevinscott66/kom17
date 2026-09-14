"""Unit tests for the word-filter automod middleware + arg parser (L-52).

Covered:

* ``_arg`` extracts the command argument (and only the argument).
* ``WordFilterAutomodMiddleware`` deletes a message containing a banned
  word, leaves clean messages alone, always calls the downstream
  handler (never consumes), skips non-group chats, and swallows a
  delete failure without raising.
* #265: the legacy escalation that follows a deletion — the chat
  warning, the ``automod_enabled``-gated warn, the pre-increment
  boundary, the ``elif`` re-ban, and the ``autoban_enabled`` gate.
* #1848/#1781: the staff exemption that sits between the deletion and
  the sanction chain — group admins and the bot owner get their message
  deleted like everyone else, but are never warned and never auto-banned.
* #1857/#1858: the two remaining sharp edges in the same escalation —
  the unlocked read that let a concurrent pair ban twice, and the audit
  write whose failure used to swallow the ban announcement.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import Message

from telegram_invite_bot.handlers import wordfilter as wf_mod
from telegram_invite_bot.handlers.wordfilter import (
    WordFilterAutomodMiddleware,
    _arg,
)
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigView

_CHAT = -100
_USER = 4242


# ---------------------------------------------------------------------------
# _arg
# ---------------------------------------------------------------------------


def _msg(text: str) -> Message:
    return cast("Message", SimpleNamespace(text=text, caption=None))


def test_arg_extracts_single_token() -> None:
    assert _arg(_msg("/filter_add badword")) == "badword"


def test_arg_keeps_multiword_argument() -> None:
    assert _arg(_msg("/filter_add bad phrase here")) == "bad phrase here"


def test_arg_empty_when_no_argument() -> None:
    assert _arg(_msg("/filter_list")) == ""


# ---------------------------------------------------------------------------
# WordFilterAutomodMiddleware
# ---------------------------------------------------------------------------


class _RecordingMessage(Message):
    """Real aiogram ``Message`` (so the middleware's ``isinstance`` gate
    passes) with a recording ``delete`` override."""

    # Pydantic models forbid arbitrary attributes; declare the test
    # bookkeeping fields explicitly.
    deleted: bool = False
    raise_on_delete: bool = False
    sent: list[str] = []

    async def delete(self, **kwargs: Any) -> bool:  # type: ignore[override]
        if self.raise_on_delete:
            raise RuntimeError("no rights")
        object.__setattr__(self, "deleted", True)
        return True

    async def answer(self, text: str, **kwargs: Any) -> Any:  # type: ignore[override]
        self.sent.append(text)
        return None


#: Distinguishes "the test does not care who sent it" from the explicit
#: ``from_user=None`` that a channel post has. Since #253 the difference
#: decides whether the message is examined at all, so the two cannot
#: share a default.
_UNSET: Any = object()


def _FakeMessage(
    text: str | None,
    chat_type: ChatType = ChatType.SUPERGROUP,
    *,
    from_user: Any = _UNSET,
    sender_chat: Any = None,
) -> _RecordingMessage:
    author: Any = _member() if from_user is _UNSET else from_user
    return _RecordingMessage.model_construct(
        message_id=555,
        date=None,
        chat=SimpleNamespace(id=_CHAT, type=chat_type),
        text=text,
        caption=None,
        from_user=author,
        sender_chat=sender_chat,
        sent=[],
    )


def _cfg(**over: Any) -> GroupModConfigView:
    base: dict[str, Any] = {
        "group_id": _CHAT,
        "automod_enabled": True,
        "profanity_enabled": True,
        "max_warns": 3,
        "mute_minutes": 1440,
        "autoban_enabled": True,
        "antiflood_enabled": False,
        "flood_max_msgs": 5,
        "flood_window_sec": 10,
        "flood_mute_minutes": 10,
    }
    base.update(over)
    return GroupModConfigView(**base)


def _fake_settings(*developers: int) -> Any:
    """Minimal ``Settings`` stand-in: the middleware reads one predicate."""
    return SimpleNamespace(bot=SimpleNamespace(is_developer=lambda user_id: user_id in developers))


def _middleware_with_words(
    words: list[str],
    cfg: GroupModConfigView | None = None,
    *,
    settings: Any = None,
) -> WordFilterAutomodMiddleware:
    mw = WordFilterAutomodMiddleware(
        registry=cast("Any", _FakeRegistry()),
        settings=cast("Any", _fake_settings() if settings is None else settings),
    )
    compiled = WordFilterAutomodMiddleware._compile(words)
    policy = (compiled, cfg if cfg is not None else _cfg())

    async def _fake_policy_for(group_id: int) -> Any:
        return policy

    # Bypass the DB read (config toggle + word list) entirely, but exercise
    # the REAL boundary-anchored matcher via ``_compile``.
    mw._policy_for = _fake_policy_for  # type: ignore[method-assign]
    return mw


async def _run(
    mw: WordFilterAutomodMiddleware, event: Any, data: dict[str, Any] | None = None
) -> bool:
    called = {"v": False}

    async def _handler(ev: Any, data: dict[str, Any]) -> str:
        called["v"] = True
        return "ok"

    result = await mw(_handler, cast("Message", event), data if data is not None else {})
    assert result == "ok"  # never consumes
    return called["v"]


# --- #265 escalation doubles ------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _FakeRegistry:
    def session(self, name: Any) -> Any:
        return _FakeSession


class _FakeRepo:
    """Stands in for ``ModerationRepo``; shares state across instances."""

    state: dict[str, Any] = {}

    def __init__(self, session: Any) -> None:
        self._session = session

    async def get_warning_count(self, *, user_id: int, chat_id: int) -> int:
        return cast("int", _FakeRepo.state["count"])

    async def add_warning(self, **kw: Any) -> tuple[int, int]:
        _FakeRepo.state["warns"].append(kw)
        _FakeRepo.state["count"] += 1
        return 1, cast("int", _FakeRepo.state["count"])

    async def record_action(self, **kw: Any) -> None:
        _FakeRepo.state["actions"].append(kw)


class _FakeBot:
    id = 777

    def __init__(
        self,
        *,
        raise_on_ban: bool = False,
        member_status: str = ChatMemberStatus.MEMBER,
        raise_on_member: bool = False,
    ) -> None:
        self.banned: list[tuple[int, int]] = []
        self._raise = raise_on_ban
        self._member_status = member_status
        self._raise_on_member = raise_on_member
        #: Every (chat, user) the escalation asked Telegram about. The
        #: count is the assertion for #1848's TTL cache: a second hit
        #: from the same author must not cost a second round-trip.
        self.member_probes: list[tuple[int, int]] = []

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        """#1848: the staff probe ``is_chat_admin_any`` runs.

        Only ``status`` is read, but ``user`` is carried too so the
        double stays interchangeable with the real ``ChatMember``.
        """
        self.member_probes.append((chat_id, user_id))
        if self._raise_on_member:
            raise RuntimeError("Telegram is unreachable")
        return SimpleNamespace(
            status=self._member_status,
            user=SimpleNamespace(id=user_id, is_bot=False),
        )

    async def ban_chat_member(self, chat_id: int, user_id: int) -> bool:
        if self._raise:
            raise RuntimeError("not enough rights")
        self.banned.append((chat_id, user_id))
        return True


@pytest.fixture
def escalation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _FakeRepo.state = {"count": 0, "warns": [], "actions": []}
    monkeypatch.setattr(wf_mod, "ModerationRepo", _FakeRepo)
    return _FakeRepo.state


def _member() -> SimpleNamespace:
    return SimpleNamespace(id=_USER, is_bot=False, full_name="Вася")


async def test_deletes_message_with_banned_word() -> None:
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("you are a BadWord")
    assert await _run(mw, msg) is True
    assert msg.deleted is True


async def test_leaves_clean_message() -> None:
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("perfectly fine")
    assert await _run(mw, msg) is True
    assert msg.deleted is False


async def test_skips_private_chat() -> None:
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", chat_type=ChatType.PRIVATE)
    assert await _run(mw, msg) is True
    assert msg.deleted is False


async def test_swallows_delete_failure() -> None:
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword")
    object.__setattr__(msg, "raise_on_delete", True)
    # Must not raise; downstream handler still runs.
    assert await _run(mw, msg) is True
    assert msg.deleted is False


async def test_empty_word_list_is_noop() -> None:
    mw = _middleware_with_words([])
    msg = _FakeMessage("anything goes")
    assert await _run(mw, msg) is True
    assert msg.deleted is False


async def test_word_boundary_does_not_overdelete_substring() -> None:
    # The legacy substring bug: banning "ass" nuked "class"/"pass".
    mw = _middleware_with_words(["ass"])
    clean = _FakeMessage("what a great class we had, pass me the salt")
    assert await _run(mw, clean) is True
    assert clean.deleted is False
    # The standalone word is still deleted (case-insensitive, punctuation-adjacent).
    for sample in ("you ASS", "ass!", "(ass)"):
        msg = _FakeMessage(sample)
        assert await _run(mw, msg) is True
        assert msg.deleted is True, sample


async def test_boundary_works_for_cyrillic() -> None:
    mw = _middleware_with_words(["кот"])
    # "котлета" must NOT trigger on the substring "кот".
    clean = _FakeMessage("вкусная котлета на ужин")
    assert await _run(mw, clean) is True
    assert clean.deleted is False
    hit = _FakeMessage("смотри какой кот")
    assert await _run(mw, hit) is True
    assert hit.deleted is True


async def test_multiword_phrase_matches_as_phrase() -> None:
    mw = _middleware_with_words(["bad phrase"])
    hit = _FakeMessage("that is a bad phrase, stop")
    assert await _run(mw, hit) is True
    assert hit.deleted is True
    # The individual words alone do NOT trigger the phrase entry.
    clean = _FakeMessage("not bad, nice phrase")
    assert await _run(mw, clean) is True
    assert clean.deleted is False


def test_compile_empty_returns_none() -> None:
    assert WordFilterAutomodMiddleware._compile([]) is None
    assert WordFilterAutomodMiddleware._compile(["", "  "]) is None


# ---------------------------------------------------------------------------
# #265 — the escalation legacy ran after every deletion (bot.py:43852-43908)
# ---------------------------------------------------------------------------


async def test_deletion_sends_the_legacy_chat_warning(escalation: dict[str, Any]) -> None:
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert msg.deleted is True
    # bot.py:43864-43865 — the author is named in the warning.
    assert len(msg.sent) == 1
    assert "Вася" in msg.sent[0]
    assert len(escalation["warns"]) == 1
    assert escalation["warns"][0]["reason"] == "Мат: badword"
    assert escalation["warns"][0]["admin_id"] == bot.id
    assert bot.banned == []


async def test_automod_off_still_deletes_and_warns_in_chat(
    escalation: dict[str, Any],
) -> None:
    """bot.py:43853 gates the delete+notice, :43868 gates the escalation."""
    mw = _middleware_with_words(["badword"], _cfg(automod_enabled=False))
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert msg.deleted is True
    assert len(msg.sent) == 1
    assert escalation["warns"] == []
    assert bot.banned == []


async def test_last_warning_triggers_the_auto_ban(escalation: dict[str, Any]) -> None:
    escalation["count"] = 2  # one short of max_warns=3
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert len(escalation["warns"]) == 1
    assert bot.banned == [(_CHAT, _USER)]
    assert escalation["actions"][0]["action"] == "ban"
    assert escalation["actions"][0]["details"] == "automod_profanity_warns=3"
    # Warning notice + ban notice.
    assert len(msg.sent) == 2


async def test_at_the_limit_rebans_without_a_new_warning(
    escalation: dict[str, Any],
) -> None:
    """bot.py:43894 — the ``elif`` twin of the pre-increment check."""
    escalation["count"] = 3  # already at max_warns
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert escalation["warns"] == []  # no warning stacked on top
    assert bot.banned == [(_CHAT, _USER)]


async def test_autoban_off_warns_but_never_bans(escalation: dict[str, Any]) -> None:
    escalation["count"] = 2
    mw = _middleware_with_words(["badword"], _cfg(autoban_enabled=False))
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert len(escalation["warns"]) == 1
    assert bot.banned == []
    assert escalation["actions"] == []
    assert len(msg.sent) == 1


async def test_failed_ban_is_swallowed_and_not_recorded(
    escalation: dict[str, Any],
) -> None:
    escalation["count"] = 2
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot(raise_on_ban=True)
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert len(escalation["warns"]) == 1  # the warning is kept
    assert escalation["actions"] == []  # ...but no ban is claimed
    assert len(msg.sent) == 1  # no "забанен" notice


async def test_bot_and_anonymous_senders_are_left_alone_entirely(
    escalation: dict[str, Any],
) -> None:
    """#253: authorless posts are skipped, not deleted-then-not-warned.

    Until #253 the deletion ran for everyone and only the escalation
    checked the author, so an anonymous admin had their own message
    removed by their own bot with no warning, no ban and nothing to
    appeal — and the linked channel's automatic forward (which arrives
    with ``sender_chat`` set) took the discussion thread with it.
    Legacy returned on ``is_bot`` before ever reading the text
    (bot.py:43796).
    """
    senders: list[tuple[str, Any, Any]] = [
        ("bot", SimpleNamespace(id=9, is_bot=True, full_name="Bot"), None),
        ("anonymous admin", _member(), SimpleNamespace(id=_CHAT)),
        ("channel auto-forward", None, SimpleNamespace(id=-1001)),
        ("no author at all", None, None),
    ]
    for label, from_user, sender_chat in senders:
        mw = _middleware_with_words(["badword"])
        msg = _FakeMessage("badword", from_user=from_user, sender_chat=sender_chat)
        bot = _FakeBot()
        assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
        assert msg.deleted is False, label
        assert msg.sent == [], label
        assert escalation["warns"] == [], label
        assert bot.banned == [], label


async def test_missing_bot_in_data_does_not_raise(escalation: dict[str, Any]) -> None:
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    assert await _run(mw, msg, {"lang": "ru"}) is True
    assert msg.deleted is True
    assert len(msg.sent) == 1  # the chat warning is not bot-dependent
    assert escalation["warns"] == []


async def test_escalation_failure_never_breaks_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The class docstring promises automod never raises into dispatch."""
    mw = _middleware_with_words(["badword"])

    async def _boom(group_id: int) -> Any:
        raise RuntimeError("registry exploded")

    mw._policy_for = _boom  # type: ignore[method-assign]
    msg = _FakeMessage("badword", from_user=_member())
    assert await _run(mw, msg, {"bot": _FakeBot(), "lang": "ru"}) is True
    assert msg.deleted is False


# ---------------------------------------------------------------------------
# #1848 / #1781 — staff are filtered, but never sanctioned
# ---------------------------------------------------------------------------


async def test_group_admin_is_deleted_but_never_warned(escalation: dict[str, Any]) -> None:
    """#1781: an admin's warning row could never be lifted again.

    ``_check_target_ok`` refuses /unwarn against any admin status, so a
    row the automod wrote against an admin outlived every operator
    surface — it just sat there for its full 30 days. The deletion and
    the public notice stay: the word list is the group's own policy and
    an admin who wants the word back can /filter_remove it.
    """
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot(member_status=ChatMemberStatus.ADMINISTRATOR)
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert msg.deleted is True
    assert len(msg.sent) == 1  # the chat notice belongs to the deletion
    assert escalation["warns"] == []
    assert bot.banned == []


async def test_group_admin_at_the_limit_is_not_rebanned(escalation: dict[str, Any]) -> None:
    """The ``else`` branch bans without adding a warning — also skipped.

    An admin who accumulated warnings before being promoted would
    otherwise be banned by their own bot on their very next hit, with no
    new row to explain it (#1780's chain, one rung up).
    """
    escalation["count"] = 3  # already at max_warns
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot(member_status=ChatMemberStatus.ADMINISTRATOR)
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert escalation["warns"] == []
    assert escalation["actions"] == []
    assert bot.banned == []


async def test_probe_failure_skips_the_sanction(escalation: dict[str, Any]) -> None:
    """Fail-SAFE, the same direction ``antiflood._is_exempt_admin`` takes.

    ``is_chat_admin_any`` returns ``None`` on any API error, and this is
    an automatic, permanent sanction: skipping one deserved warning
    costs a repost, while banning one admin on a 429 costs the group its
    moderator. Note the delete already happened — uncertainty about WHO
    sent it does not make the word allowed.
    """
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot(raise_on_member=True)
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert msg.deleted is True
    assert escalation["warns"] == []
    assert bot.banned == []


async def test_bot_owner_is_exempt_without_asking_telegram(
    escalation: dict[str, Any],
) -> None:
    """The owner check is local, so it also costs no round-trip.

    #1848: every other moderation gate lets the owner through
    (``moderation._require_admin``), and the owner is not necessarily an
    admin of a group their bot serves. Deliberately WIDER than
    ``_check_target_ok``, which protects admins only — the asymmetry is
    the point: a human moderator refusing to act is recoverable, a bot
    permanently banning its own owner is not.
    """
    mw = _middleware_with_words(["badword"], settings=_fake_settings(_USER))
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert msg.deleted is True
    assert escalation["warns"] == []
    assert bot.banned == []
    assert bot.member_probes == []


async def test_plain_member_is_still_warned(escalation: dict[str, Any]) -> None:
    """The control: the exemption must not swallow ordinary authors."""
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert len(escalation["warns"]) == 1
    assert bot.member_probes == [(_CHAT, _USER)]


async def test_staff_verdict_is_cached_across_messages(escalation: dict[str, Any]) -> None:
    """One probe per author per TTL, not one per banned word."""
    mw = _middleware_with_words(["badword"])
    bot = _FakeBot(member_status=ChatMemberStatus.ADMINISTRATOR)
    for _ in range(3):
        msg = _FakeMessage("badword", from_user=_member())
        assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert bot.member_probes == [(_CHAT, _USER)]
    assert escalation["warns"] == []


# ---------------------------------------------------------------------------
# #1857 / #1858 — the escalation's remaining sharp edges
# ---------------------------------------------------------------------------


class _RacingRepo(_FakeRepo):
    """Reproduces the unlocked read at the heart of #1857.

    ``get_warning_count`` is a bare SELECT, and the ``before_cursor_execute``
    hook in ``db/engines.py`` opens ``BEGIN IMMEDIATE`` only on a
    write-headed statement — so the read runs in autocommit and holds
    nothing. The snapshot-then-``sleep(0)`` is that missing lock: the
    value is fixed at read time and the sibling's write lands after,
    which is exactly what two messages arriving together do in prod.
    """

    async def get_warning_count(self, *, user_id: int, chat_id: int) -> int:
        snapshot = cast("int", _FakeRepo.state["count"])
        await asyncio.sleep(0)
        return snapshot


class _AuditFailingRepo(_FakeRepo):
    """The ban lands in Telegram; the audit row cannot be written."""

    async def record_action(self, **kw: Any) -> None:
        raise RuntimeError("moderation.db is locked")


async def test_a_concurrent_pair_bans_once_not_twice(
    escalation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1857: both callers cross the limit, but only one is AT it.

    Two hits read ``count == 2`` (one short of ``max_warns=3``), then
    write; ``add_warning`` returns 3 to one and 4 to the other. Under
    ``>=`` both were at/over the limit, so the target was banned twice,
    announced twice and logged twice. ``==`` gives the ban to the caller
    that actually reached the threshold — the same reason /warn's M-M-1
    branch compares with ``==`` (``moderation.py`` ``handle_warn``).

    Both warnings still land. That over-warn is the unlocked read's
    other half and is not what this ticket fixes; the ban is.
    """
    monkeypatch.setattr(wf_mod, "ModerationRepo", _RacingRepo)
    escalation["count"] = 2
    mw = _middleware_with_words(["badword"])
    bot = _FakeBot()
    first = _FakeMessage("badword", from_user=_member())
    second = _FakeMessage("badword", from_user=_member())
    await asyncio.gather(
        _run(mw, first, {"bot": bot, "lang": "ru"}),
        _run(mw, second, {"bot": bot, "lang": "ru"}),
    )
    assert len(escalation["warns"]) == 2
    assert bot.banned == [(_CHAT, _USER)]
    assert [a["action"] for a in escalation["actions"]] == ["ban"]
    assert escalation["actions"][0]["details"] == "automod_profanity_warns=3"


async def test_a_failing_audit_write_still_announces_the_ban(
    escalation: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1858: the ban is already irreversible by the time the row fails.

    ``ban_chat_member`` has returned, and there is no ``bans`` table in
    this pipeline (``db/models/moderation.py``) — the ban lives only
    inside Telegram. Letting the audit failure escape meant the chat
    never learned why the message author vanished, which is the one
    surface that could still have explained it.
    """
    monkeypatch.setattr(wf_mod, "ModerationRepo", _AuditFailingRepo)
    escalation["count"] = 2
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert bot.banned == [(_CHAT, _USER)]
    assert escalation["actions"] == []  # the row is genuinely lost
    assert len(msg.sent) == 2  # ...but the ban notice still goes out


async def test_a_target_above_the_limit_is_still_rebanned(escalation: dict[str, Any]) -> None:
    """#1857's other half: ``==`` belongs to the write branch only.

    Someone whose count already sits ABOVE ``max_warns`` — the limit was
    lowered under them, or an older row survived — writes nothing, so
    there is no second writer to collide with and nothing to narrow.
    Tightening this branch to ``==`` too would quietly stop re-banning
    them, which is why the two branches do not share a comparison.
    """
    escalation["count"] = 5  # max_warns is 3
    mw = _middleware_with_words(["badword"])
    msg = _FakeMessage("badword", from_user=_member())
    bot = _FakeBot()
    assert await _run(mw, msg, {"bot": bot, "lang": "ru"}) is True
    assert escalation["warns"] == []
    assert bot.banned == [(_CHAT, _USER)]
    assert escalation["actions"][0]["details"] == "automod_profanity_warns=5"
