"""Unit tests for the join captcha (L-55, cluster G2).

Covered:

* Config plumbing — the two new ``group_mod_config`` fields exist in the
  repo's defaults/FIELD_NAMES and the /modcfg key aliases resolve.
* Button-press path — the right user gets the restriction lifted, the
  timer cancelled and the notice edited; anyone else gets a
  callback-answer rejection and nothing changes. With no timer entry
  (a process restart) the lift still runs, but only after the live
  restriction is checked: a timed one belongs to /mute or antiflood and
  a stale button must not clear it (#342).
* Timeout helper — an unconfirmed pending entry is kicked (ban+unban)
  and its notice deleted; an already-confirmed (absent) entry is a
  no-op; Telegram failures are swallowed.
* Arming helper — restrict failure skips the captcha; notice-send
  failure rolls the restriction back.
* Join ordering (#245(e)) — the mute is the first thing the join path
  does; the bookkeeping write and the admin probe both happen after it.
* Invite-link joins (#245(d)) — a bare ``chat_member`` transition arms
  the captcha the same way the service message does, and the two
  updates about one arrival onboard it once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from telegram_invite_bot.handlers import group_events
from telegram_invite_bot.handlers.group_events import (
    _CAPTCHA_MUTE_UNTIL,
    _PENDING_CAPTCHA,
    _RECENT_ONBOARDS,
    _expire_captcha,
    _start_captcha,
    handle_captcha_confirm,
    handle_member_joined,
    handle_new_members,
)
from telegram_invite_bot.handlers.modcfg import _KEY_ALIASES, _normalise_key
from telegram_invite_bot.keyboards.builders.captcha import CaptchaConfirm
from telegram_invite_bot.repositories.group_mod_config_repo import (
    _DEFAULTS,
    FIELD_NAMES,
    GroupModConfigView,
)

_CHAT = -100555
_USER = 4242
_OTHER = 9999
_NOTICE_ID = 77

#: Placeholder registry. Nothing in these tests reaches a real database:
#: the join path's DB touches are monkeypatched out, and the captcha
#: kick's audit write is best-effort, so an unusable registry exercises
#: the swallow rather than breaking the assertion under test. The tests
#: that care about the audit row patch ``_record_captcha_kick`` instead.
_REGISTRY = cast("Any", object())


@pytest.fixture(autouse=True)
def _clean_pending() -> Any:
    _PENDING_CAPTCHA.clear()
    # #2027's recorded mute deadlines are module-level for the same
    # reason and have to go the same way.
    _CAPTCHA_MUTE_UNTIL.clear()
    # #245(d)'s join-dedup map is module-level too: leaving a claim
    # behind would make the next test's join a silent no-op.
    _RECENT_ONBOARDS.clear()
    yield
    for task in _PENDING_CAPTCHA.values():
        task.cancel()
    _PENDING_CAPTCHA.clear()
    _CAPTCHA_MUTE_UNTIL.clear()
    _RECENT_ONBOARDS.clear()


# ---------------------------------------------------------------------------
# Config fields
# ---------------------------------------------------------------------------


def test_captcha_fields_in_repo_surface() -> None:
    assert "captcha_enabled" in FIELD_NAMES
    assert "captcha_timeout_sec" in FIELD_NAMES
    assert _DEFAULTS["captcha_enabled"] is False
    assert _DEFAULTS["captcha_timeout_sec"] == 120


def test_captcha_view_defaults_off() -> None:
    # A constructor that predates the captcha fields keeps working and
    # yields the captcha-off defaults.
    view = GroupModConfigView(
        group_id=_CHAT,
        automod_enabled=True,
        profanity_enabled=True,
        max_warns=3,
        mute_minutes=1440,
        autoban_enabled=True,
        antiflood_enabled=False,
        flood_max_msgs=5,
        flood_window_sec=10,
        flood_mute_minutes=10,
    )
    assert view.captcha_enabled is False
    assert view.captcha_timeout_sec == 120


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("captcha", "captcha_enabled"),
        ("Captcha", "captcha_enabled"),
        ("капча", "captcha_enabled"),
        ("captchatime", "captcha_timeout_sec"),
        ("капчавремя", "captcha_timeout_sec"),
    ],
)
def test_modcfg_captcha_aliases(raw: str, expected: str) -> None:
    assert _normalise_key(raw) == expected


def test_modcfg_alias_targets_are_repo_fields() -> None:
    assert set(_KEY_ALIASES.values()) <= set(FIELD_NAMES)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeBot:
    """Duck-typed Bot capturing the captcha-relevant calls."""

    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    fail_restrict: bool = False
    fail_send: bool = False
    #: Users ``get_chat_member`` should answer as administrators.
    admin_ids: set[int] = field(default_factory=set)
    #: The bot's own account id — ``_record_captcha_kick`` stamps it as
    #: the acting admin on the audit row.
    id: int = 1000
    fail_ban: bool = False
    #: How many ``unban_chat_member`` calls should raise before one is
    #: allowed through. ``kick_member`` retries, so this is what tells a
    #: recovered kick apart from one that ran out of attempts.
    fail_unbans: int = 0
    #: When set, ``get_chat_member`` reports the user as ``restricted``
    #: with this expiry; ``None`` keeps the plain member/administrator
    #: answer. The Unix epoch is what Telegram sends for a restriction
    #: with no expiry at all — a moderator's "Forever", and since #2027
    #: no longer a shape the captcha itself ever produces.
    restricted_until: datetime | None = None

    async def restrict_chat_member(
        self, chat_id: int, user_id: int, *, permissions: Any, until_date: datetime | None = None
    ) -> None:
        self.calls.append(("restrict", (chat_id, user_id, permissions)))
        if self.fail_restrict:
            raise TelegramBadRequest(method=cast("Any", None), message="no rights")
        # One restriction row per member: a later restrict replaces the
        # earlier deadline rather than stacking on it. The probe's test
        # since #2027 is whether that deadline is still the one the
        # captcha set, so the fake has to carry it.
        self.restricted_until = until_date

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.calls.append(("send", (chat_id, text)))
        if self.fail_send:
            raise TelegramBadRequest(method=cast("Any", None), message="cannot send")
        return SimpleNamespace(message_id=_NOTICE_ID)

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append(("probe", (chat_id, user_id)))
        # Admin first: Telegram refuses to restrict an administrator, so
        # one is never reported back as ``restricted`` no matter what the
        # bot just tried. Asking about the expiry first would make the
        # captcha's own arming restrict hide the admin from the excuse
        # path that runs right after it.
        if user_id in self.admin_ids:
            return SimpleNamespace(status="administrator")
        if self.restricted_until is not None:
            return SimpleNamespace(status="restricted", until_date=self.restricted_until)
        return SimpleNamespace(status="member")

    async def ban_chat_member(self, chat_id: int, user_id: int, **kwargs: Any) -> None:
        self.calls.append(("ban", (chat_id, user_id, kwargs)))
        if self.fail_ban:
            raise TelegramBadRequest(method=cast("Any", None), message="no rights")

    async def unban_chat_member(self, chat_id: int, user_id: int, **kwargs: Any) -> None:
        self.calls.append(("unban", (chat_id, user_id, kwargs)))
        if self.fail_unbans > 0:
            self.fail_unbans -= 1
            raise TelegramBadRequest(method=cast("Any", None), message="flood")

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("delete", (chat_id, message_id)))

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> None:
        self.calls.append(("edit", (chat_id, message_id, text)))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakeCallback:
    """Duck-typed CallbackQuery for handle_captcha_confirm."""

    def __init__(self, presser_id: int) -> None:
        self.from_user = SimpleNamespace(id=presser_id, first_name="Joiner", username=None)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=_CHAT), message_id=_NOTICE_ID)
        self.answers: list[tuple[Any, ...]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


def _joiner(user_id: int = _USER) -> Any:
    return SimpleNamespace(
        id=user_id,
        first_name="Joiner",
        username=None,
        language_code="ru",
        is_bot=False,
    )


# ---------------------------------------------------------------------------
# Button-press path
# ---------------------------------------------------------------------------


async def test_confirm_right_user_lifts_and_edits() -> None:
    bot = _FakeBot()
    armed = await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    assert armed is True
    assert (_CHAT, _USER) in _PENDING_CAPTCHA
    task = _PENDING_CAPTCHA[(_CHAT, _USER)]

    cb = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "ru"
    )

    assert (_CHAT, _USER) not in _PENDING_CAPTCHA
    assert task.cancelling() or task.cancelled() or task.done()
    # restrict (arm) + send + restrict (lift) + edit
    restricts = [c for c in bot.calls if c[0] == "restrict"]
    assert len(restricts) == 2
    lift_perms = restricts[1][1][2]
    assert lift_perms.can_send_messages is True
    assert "edit" in bot.names()
    assert "ban" not in bot.names()
    assert len(cb.answers) == 1  # success ack


async def test_confirm_wrong_user_rejected() -> None:
    bot = _FakeBot()
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    before = list(bot.calls)

    cb = _FakeCallback(_OTHER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "ru"
    )

    assert (_CHAT, _USER) in _PENDING_CAPTCHA  # still pending
    assert bot.calls == before  # no lift, no edit, no kick
    assert len(cb.answers) == 1
    assert cb.answers[0][1].get("show_alert") is True


async def test_confirm_without_pending_timer_still_lifts() -> None:
    """Process restart dropped the timer — the button must still work."""
    bot = _FakeBot()
    cb = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "en"
    )
    assert "restrict" in bot.names()
    assert "edit" in bot.names()


async def test_confirm_without_timer_refuses_an_unexpiring_restriction() -> None:
    """#2027: epoch ``until_date`` is a moderator's "Forever", not ours.

    It used to read as the captcha's own, because the captcha's restrict
    passed no expiry and nothing else this bot does produces one. The
    premise held for the bot's own sanctions and the test was applied to
    every restriction there is — including the one a human moderator
    lands from Telegram's own UI, where "Forever" is the default.
    """
    bot = _FakeBot(restricted_until=datetime(1970, 1, 1, tzinfo=UTC))
    cb = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "en"
    )
    assert bot.names() == ["probe"]
    assert len(cb.answers) == 1
    assert cb.answers[0][1].get("show_alert") is True


async def test_confirm_without_timer_refuses_to_clear_a_timed_mute() -> None:
    """#342: a stale button is not a self-service unmute.

    No pending entry (restart), and the live restriction carries a future
    expiry — which only /mute and antiflood produce. The lift must not run.
    """
    bot = _FakeBot(restricted_until=datetime.now(UTC) + timedelta(minutes=10))
    cb = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "ru"
    )
    assert bot.names() == ["probe"]
    assert len(cb.answers) == 1
    assert cb.answers[0][1].get("show_alert") is True


async def test_confirm_with_live_timer_refuses_to_clear_a_timed_mute() -> None:
    """#671: the sanction the button must not clear usually lands *early*.

    An earlier revision only consulted :func:`_restriction_is_captchas`
    when the in-memory entry was gone, on the reasoning that a pending
    entry is proof enough. It is not: a moderator can ``/mute`` a
    spamming joiner while the captcha window is still open — the most
    likely moment for it — and the still-rendered button then restored
    ``UNRESTRICTED_PERMS`` over the mute. Self-service unmute is exactly
    what #342 was filed against.
    """
    bot = _FakeBot(restricted_until=None)
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    # After the arming restrict, never before it: Telegram keeps one
    # restriction row, so a mute set first would simply be overwritten
    # by the captcha's own and the scenario would not be #671's.
    bot.restricted_until = datetime.now(UTC) + timedelta(minutes=10)
    before = [c for c in bot.calls if c[0] == "restrict"]
    cb = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "ru"
    )

    assert "probe" in bot.names()
    assert [c for c in bot.calls if c[0] == "restrict"] == before  # no lift
    assert "edit" not in bot.names()
    assert len(cb.answers) == 1
    assert cb.answers[0][1].get("show_alert") is True
    # The probe runs before the pop, so a refused press does not quietly
    # cancel the kick the joiner still has coming.
    assert (_CHAT, _USER) in _PENDING_CAPTCHA


async def test_confirm_with_live_timer_lifts_the_captchas_own_restriction() -> None:
    """The ordinary path still works, probe and all.

    Nothing is stipulated about the live restriction here: arming writes
    the deadline the fake then reports, which is the whole of what the
    probe matches against since #2027.
    """
    bot = _FakeBot(restricted_until=None)
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    cb = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", cb), CaptchaConfirm(user_id=_USER), cast("Bot", bot), "ru"
    )
    assert len([c for c in bot.calls if c[0] == "restrict"]) == 2
    assert "edit" in bot.names()
    assert (_CHAT, _USER) not in _PENDING_CAPTCHA


# ---------------------------------------------------------------------------
# Timeout helper
# ---------------------------------------------------------------------------


async def test_a_landed_kick_releases_the_onboarding_claim() -> None:
    """#672: kick-and-rejoin walked straight past the captcha.

    ``kick_member`` is a ban+unban, so it takes the mute away with the
    membership. The dedup claim outlived it, so a joiner who came back
    inside :data:`group_events._DEDUP_SEC` was dropped by
    ``_claim_joiners`` — no mute, no captcha, no notice. Reachable
    whenever ``captcha_timeout_sec`` is set below that window, and
    ``/modcfg`` allows values down to 10s.
    """
    bot = _FakeBot()
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    group_events._claim_joiners(_CHAT, [_joiner()])
    assert (_CHAT, _USER) in _RECENT_ONBOARDS

    await _expire_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _USER, _NOTICE_ID)

    assert (_CHAT, _USER) not in _RECENT_ONBOARDS


async def test_a_kick_that_never_landed_keeps_the_onboarding_claim() -> None:
    """The mute is still on, so the claim is still doing its job.

    #285 lifts the restriction only when the ban itself failed; here the
    joiner is still in the chat and still silenced, and releasing the
    claim would let the twin update onboard them a second time.
    """
    bot = _FakeBot(fail_ban=True)
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    group_events._claim_joiners(_CHAT, [_joiner()])

    await _expire_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _USER, _NOTICE_ID)

    assert (_CHAT, _USER) in _RECENT_ONBOARDS


async def test_expire_kicks_and_deletes_when_pending() -> None:
    bot = _FakeBot()
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    await _expire_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _USER, _NOTICE_ID)
    assert (_CHAT, _USER) not in _PENDING_CAPTCHA
    names = bot.names()
    assert "ban" in names
    assert "unban" in names
    assert "delete" in names


async def test_expire_noop_when_already_confirmed() -> None:
    bot = _FakeBot()
    await _expire_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _USER, _NOTICE_ID)
    assert bot.calls == []


# ---------------------------------------------------------------------------
# Arming helper failure modes
# ---------------------------------------------------------------------------


async def test_start_captcha_restrict_failure_skips() -> None:
    bot = _FakeBot(fail_restrict=True)
    armed = await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    assert armed is False
    assert (_CHAT, _USER) not in _PENDING_CAPTCHA
    assert "send" not in bot.names()


async def test_start_captcha_send_failure_rolls_back_restriction() -> None:
    bot = _FakeBot(fail_send=True)
    armed = await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    assert armed is False
    assert (_CHAT, _USER) not in _PENDING_CAPTCHA
    restricts = [c for c in bot.calls if c[0] == "restrict"]
    assert len(restricts) == 2  # arm + rollback
    assert restricts[1][1][2].can_send_messages is True


async def test_captcha_config_read_failure_degrades_to_off() -> None:
    class _BoomRegistry:
        pass

    enabled, timeout = await group_events._captcha_config_for(cast("Any", _BoomRegistry()), _CHAT)
    assert enabled is False
    assert timeout == 120


async def test_rejoin_rearms_without_letting_the_old_timer_kick() -> None:
    """A rejoin inside the window must not inherit the previous timer.

    ``_expire_captcha`` pops by key alone — it does not check that the
    timer firing is the one the entry belongs to. So a stale timer left
    running across a re-arm consumes the *fresh* entry and kicks a joiner
    who still had time on the clock, while the fresh timer later finds
    nothing to do. The first arm below uses a zero-second window: were it
    left armed, a single event-loop turn is all it needs to ban.
    """
    bot = _FakeBot()
    assert await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 0) is True
    stale = _PENDING_CAPTCHA[(_CHAT, _USER)]

    assert await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120) is True
    fresh = _PENDING_CAPTCHA[(_CHAT, _USER)]
    assert fresh is not stale

    await asyncio.sleep(0.05)

    assert "ban" not in bot.names()
    assert stale.done()
    assert _PENDING_CAPTCHA[(_CHAT, _USER)] is fresh


# ---------------------------------------------------------------------------
# Join ordering (#245(e))
# ---------------------------------------------------------------------------


#: Marker planted in the group's custom welcome template so the welcome
#: card is tellable from the captcha notice — both leave the handler as
#: ``bot.send_message``, and what is under test here is their *order*.
_WELCOME_MARK = "WELCOME-CARD"


def _fake_message(bot: _FakeBot, *joiners: Any) -> Any:
    """Duck-typed service message carrying ``new_chat_members``."""
    return SimpleNamespace(
        new_chat_members=list(joiners),
        chat=SimpleNamespace(id=_CHAT, type="supergroup", title="Chat"),
    )


def _ordered(bot: _FakeBot) -> list[str]:
    """Call names with the welcome card's ``send`` renamed to ``welcome``."""
    return [
        "welcome" if name == "send" and len(args) > 1 and _WELCOME_MARK in str(args[1]) else name
        for name, args in bot.calls
    ]


def _wire_join_path(
    monkeypatch: pytest.MonkeyPatch,
    bot: _FakeBot,
    *,
    captcha: tuple[bool, int] = (True, 120),
) -> None:
    """Replace the join path's DB reaches with ordered log entries.

    The three stand-ins are the only non-Telegram work
    :func:`handle_new_members` does, and each is recorded under its own
    name so a test can assert *where* it sits relative to the mute. The
    real ones need a live registry and answer nothing this test cares
    about.
    """

    async def _config(*_a: Any, **_k: Any) -> tuple[bool, int]:
        bot.calls.append(("config", ()))
        return captcha

    async def _record(_registry: Any, chat_id: int, humans: Any, **_k: Any) -> None:
        bot.calls.append(("record", (chat_id, tuple(u.id for u in humans))))

    async def _username(*_a: Any, **_k: Any) -> str:
        return "my_test_bot"

    async def _template(*_a: Any, **_k: Any) -> str:
        return _WELCOME_MARK + " {user}"

    monkeypatch.setattr(group_events, "_captcha_config_for", _config)
    monkeypatch.setattr(group_events, "_record_joins", _record)
    monkeypatch.setattr(group_events, "_bot_username", _username)
    monkeypatch.setattr(group_events, "_custom_template_for", _template)


async def test_the_mute_lands_before_any_other_join_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#245(e): everything between the join and the mute is an open window.

    The joiner can post the moment they are in the chat, so the only
    ordering that makes the captcha worth having is one where the
    restriction is the first outbound call. Two pieces of work used to
    sit ahead of it: the ``user_group_joins`` write, and a live
    ``get_chat_member`` per joiner spent on a question the mute does not
    depend on — Telegram refuses to restrict an admin regardless.

    The assertion is positional, not merely "all four happened": the old
    order produced the same four entries, just with the mute third.
    """
    bot = _FakeBot()
    _wire_join_path(monkeypatch, bot)

    await handle_new_members(
        cast("Any", _fake_message(bot, _joiner())),
        cast("Bot", bot),
        cast("Any", object()),
    )

    names = _ordered(bot)
    assert names.index("restrict") < names.index("record")
    assert names.index("restrict") < names.index("probe")
    # Config has to precede the mute — it is what decides there is one.
    assert names.index("config") < names.index("restrict")
    # And the notice is armed, so the joiner has a button to press.
    assert names.index("send") > names.index("probe")
    assert (_CHAT, _USER) in _PENDING_CAPTCHA


async def test_an_admin_is_muted_first_and_excused_afterwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe moved after the mute, so a false positive costs a lift.

    That is the price of the reorder and it is worth pinning: an admin
    must end up unrestricted, with no captcha notice and no pending
    timer, and must still get the ordinary welcome card. On a real admin
    the mute is a no-op for Telegram anyway — the lift exists for the
    case where the probe was right and the mute was not a no-op.
    """
    bot = _FakeBot(admin_ids={_USER})
    _wire_join_path(monkeypatch, bot)

    await handle_new_members(
        cast("Any", _fake_message(bot, _joiner())),
        cast("Bot", bot),
        cast("Any", object()),
    )

    names = _ordered(bot)
    assert names.index("restrict") < names.index("probe")
    # Mute then lift: two restricts, the second one permissive.
    restricts = [c for c in bot.calls if c[0] == "restrict"]
    assert len(restricts) == 2
    assert restricts[0][1][2].can_send_messages is False
    assert restricts[1][1][2].can_send_messages is True
    assert "send" not in names  # no captcha notice for an admin
    assert (_CHAT, _USER) not in _PENDING_CAPTCHA
    assert "welcome" in names


async def test_captcha_off_leaves_the_join_path_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reorder must not have made the mute unconditional.

    With the feature off there is nothing to arm, so no restriction and
    no probe should happen at all — the joiner is recorded and greeted,
    exactly as before L-55 existed.
    """
    bot = _FakeBot()
    _wire_join_path(monkeypatch, bot, captcha=(False, 120))

    await handle_new_members(
        cast("Any", _fake_message(bot, _joiner())),
        cast("Bot", bot),
        cast("Any", object()),
    )

    names = _ordered(bot)
    assert "restrict" not in names
    assert "probe" not in names
    assert names.index("record") < names.index("welcome")


async def test_a_join_the_bot_cannot_mute_is_still_recorded_and_greeted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No restrict rights means no captcha, not a swallowed join.

    The mute moving to the front puts a failure-prone call ahead of the
    bookkeeping write, so the fail-soft path is worth pinning from the
    join handler and not only from the arming helper: a rights error must
    leave the membership recorded and the welcome sent, with no probe
    spent on a joiner that was never muted.
    """
    bot = _FakeBot(fail_restrict=True)
    _wire_join_path(monkeypatch, bot)

    await handle_new_members(
        cast("Any", _fake_message(bot, _joiner())),
        cast("Bot", bot),
        cast("Any", object()),
    )

    names = _ordered(bot)
    assert "probe" not in names
    assert "send" not in names
    assert names.index("record") < names.index("welcome")
    assert (_CHAT, _USER) not in _PENDING_CAPTCHA


def _fake_join_event(status_from: str = "left", status_to: str = "member") -> Any:
    """Duck-typed ``chat_member`` transition about a human joiner."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=_CHAT, type="supergroup", title="Chat"),
        old_chat_member=SimpleNamespace(status=status_from, is_member=False),
        new_chat_member=SimpleNamespace(status=status_to, is_member=True, user=_joiner()),
    )


async def test_an_invite_link_join_arms_the_captcha_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#245(d): the join with no service message gets the same treatment.

    An invite-link follow or an approved join request reaches the bot
    only as a ``chat_member`` transition. It has to run the identical
    sequence — mute first, then the bookkeeping and the probe — because
    a captcha that covers only the "someone added me" arrival covers the
    less common half of real joins.
    """
    bot = _FakeBot()
    _wire_join_path(monkeypatch, bot)

    await handle_member_joined(
        cast("Any", _fake_join_event()), cast("Bot", bot), cast("Any", object())
    )

    names = _ordered(bot)
    assert names.index("restrict") < names.index("record")
    assert names.index("restrict") < names.index("probe")
    assert (_CHAT, _USER) in _PENDING_CAPTCHA


async def test_the_second_update_about_one_join_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain "add a friend" join fires both updates (#245(d)).

    The membership write is idempotent so it would hide the duplication;
    the captcha would not — arming twice posts two notices for one
    arrival. Fed in both orders, because Telegram promises nothing about
    which update lands first and the claim has to work from either.
    """
    bot = _FakeBot()
    _wire_join_path(monkeypatch, bot)

    await handle_new_members(
        cast("Any", _fake_message(bot, _joiner())),
        cast("Bot", bot),
        cast("Any", object()),
    )
    first_pass = list(bot.calls)
    await handle_member_joined(
        cast("Any", _fake_join_event()), cast("Bot", bot), cast("Any", object())
    )
    assert bot.calls == first_pass

    _RECENT_ONBOARDS.clear()
    _PENDING_CAPTCHA.clear()
    bot.calls.clear()

    await handle_member_joined(
        cast("Any", _fake_join_event()), cast("Bot", bot), cast("Any", object())
    )
    reversed_pass = list(bot.calls)
    await handle_new_members(
        cast("Any", _fake_message(bot, _joiner())),
        cast("Bot", bot),
        cast("Any", object()),
    )
    assert bot.calls == reversed_pass


@pytest.mark.parametrize(
    ("status_from", "status_to"),
    [("member", "restricted"), ("restricted", "member"), ("member", "administrator")],
)
async def test_the_captcha_mute_does_not_look_like_a_join(
    monkeypatch: pytest.MonkeyPatch, status_from: str, status_to: str
) -> None:
    """The handler must not be re-entered by its own side effects.

    Muting a joiner emits ``member`` -> ``restricted``; lifting the mute
    emits the reverse. Were "ended up a member" the test for a join, the
    lift would start a fresh onboarding — a loop with the bot on both
    ends of it.
    """
    bot = _FakeBot()
    _wire_join_path(monkeypatch, bot)

    await handle_member_joined(
        cast("Any", _fake_join_event(status_from, status_to)),
        cast("Bot", bot),
        cast("Any", object()),
    )

    assert bot.calls == []
    assert _RECENT_ONBOARDS == {}


async def test_a_bot_arriving_by_link_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bots are filtered on the ``chat_member`` path as well.

    Same reason as the service-message path: an account that will never
    have a profile has no membership worth recording and no captcha
    worth showing it.
    """
    bot = _FakeBot()
    _wire_join_path(monkeypatch, bot)
    event = _fake_join_event()
    event.new_chat_member.user.is_bot = True

    await handle_member_joined(cast("Any", event), cast("Bot", bot), cast("Any", object()))

    assert bot.calls == []
