"""Unit tests for the /ad advertiser-request flow (Cluster H4, L-61).

Covers the pure helpers (cooldown arithmetic, admin-DM formatting,
settings fallback) plus the async handler branches that encode the
legacy contract:

* group invocation → pointer reply, no FSM entry (``bot.py:36861``);
* cooldown rejection (``bot.py:36533-36546``), developer exemption;
* submit path: admin DM FIRST, cooldown stamped ONLY on success
  (``bot.py:36903-36905``) — a failed send must leave the user free
  to retry and must clear the FSM either way;
* no-admin-chat-configured → refusal, not a silent void-drop.

Telegram/sqlalchemy plumbing is faked with light stubs — the handlers
only touch ``.reply`` / ``.send_message`` / FSMContext-shaped objects.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramAPIError

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.fsm.ads import AdsStates
from telegram_invite_bot.handlers import ads as ads_mod
from telegram_invite_bot.handlers.ads import (
    _cooldown_hours,
    _cooldown_remaining_seconds,
    _format_admin_notification,
)
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

# ``Any``-typed aliases: the handlers are exercised with duck-typed
# fakes (FakeMessage/FakeBot/FakeState), and per-argument
# ``type: ignore`` noise at every call site obscures the assertions.
handle_ad_command: Any = ads_mod.handle_ad_command
handle_ads_text: Any = ads_mod.handle_ads_text

_TZ = ZoneInfo("UTC")


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeState:
    """Minimal FSMContext stand-in: state string + data dict."""

    def __init__(self, state: str | None = None, data: dict[str, Any] | None = None) -> None:
        self._state = state
        self._data = data or {}
        self.cleared = False

    async def get_state(self) -> str | None:
        return self._state

    async def set_state(self, state: Any) -> None:
        self._state = getattr(state, "state", state)

    async def get_data(self) -> dict[str, Any]:
        return dict(self._data)

    async def set_data(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    async def clear(self) -> None:
        self.cleared = True
        self._state = None
        self._data = {}


class FakeMessage:
    def __init__(
        self,
        *,
        chat_type: str = "private",
        text: str = "",
        user_id: int = 100,
    ) -> None:
        self.chat = SimpleNamespace(type=chat_type, id=user_id)
        self.from_user = SimpleNamespace(
            id=user_id, first_name="Алиса", username="alice", is_bot=False
        )
        self.text = text
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs: Any) -> None:
        self.replies.append(text)


class FakeBot:
    def __init__(self, *, fail_send: bool = False, member_count: int = 42) -> None:
        self.fail_send = fail_send
        self.member_count = member_count
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> None:
        if self.fail_send:
            raise TelegramAPIError(method="sendMessage", message="boom")  # type: ignore[arg-type]
        self.sent.append((chat_id, text))

    async def get_chat_member_count(self, _chat_id: int) -> int:
        return self.member_count


class FakeStatsRepo:
    async def active_user_count(self, _chat_id: int, *, days: int, today: Any) -> int:
        return {1: 5, 7: 20, 30: 70}.get(days, 0)


def _settings(*, admin_chat_id: int = 777, dev_ids: frozenset[int] = frozenset()) -> Any:
    bot_cfg = SimpleNamespace(
        admin_chat_id=admin_chat_id,
        main_chat_id=-100123,
        ads_request_cooldown_hours=24,
        is_developer=lambda uid: uid in dev_ids,
    )
    return SimpleNamespace(bot=bot_cfg, stats=SimpleNamespace(timezone="UTC"))


@pytest.fixture(autouse=True)
def _isolate_module_state() -> Any:
    """Each test starts with a clean in-memory cooldown map."""
    ads_mod._last_request_at.clear()
    yield
    ads_mod._last_request_at.clear()


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_cooldown_first_request_allowed() -> None:
    assert _cooldown_remaining_seconds(1, now=1000.0, cooldown_hours=24, exempt=False) == 0


def test_cooldown_blocks_within_window_and_reports_remaining() -> None:
    ads_mod._last_request_at[1] = 1000.0
    remaining = _cooldown_remaining_seconds(1, now=1000.0 + 3600, cooldown_hours=24, exempt=False)
    # 24h window minus 1h elapsed == 23h left.
    assert remaining == 23 * 3600


def test_cooldown_expires_after_window() -> None:
    ads_mod._last_request_at[1] = 1000.0
    assert (
        _cooldown_remaining_seconds(1, now=1000.0 + 24 * 3600 + 1, cooldown_hours=24, exempt=False)
        == 0
    )


def test_cooldown_developer_exempt() -> None:
    """Legacy ``bot.py:36534-36535``: DEVELOPER_IDS skip the cooldown."""
    ads_mod._last_request_at[1] = 1000.0
    assert _cooldown_remaining_seconds(1, now=1001.0, cooldown_hours=24, exempt=True) == 0


def test_cooldown_hours_matches_the_legacy_window_by_default() -> None:
    """An operator who sets nothing gets legacy's 24 h, not a new number."""
    assert BotConfig(_env_file=None, BOT_TOKEN="t").ads_request_cooldown_hours == 24
    assert _cooldown_hours(_settings()) == 24


def test_cooldown_hours_reads_settings_field() -> None:
    s = _settings()
    s.bot.ads_request_cooldown_hours = 6
    assert _cooldown_hours(s) == 6


def test_cooldown_hours_rejects_nonpositive() -> None:
    s = _settings()
    s.bot.ads_request_cooldown_hours = 0
    assert _cooldown_hours(s) == 24


def test_admin_notification_escapes_html() -> None:
    body = _format_admin_notification(
        req_id=7,
        uid=42,
        first_name="<script>",
        username="ev&il",
        text="buy <b>now</b>",
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "buy &lt;b&gt;now&lt;/b&gt;" in body
    assert "#7" in body
    assert "<code>42</code>" in body


def test_admin_notification_fits_telegram_even_for_an_emoji_wall() -> None:
    """``_REQUEST_MAX_LEN`` counts code points, Telegram counts UTF-16.

    2048 emoji is 2048 characters — under the 3000 cap, so nothing was
    truncated — but 4096 units, and the framing then pushed the DM past
    the ceiling. The admin DM is the ONLY delivery path for an ad
    request (there is no ads table), so the send failing meant the
    request was refused on every retry.
    """
    body = _format_admin_notification(
        req_id=7,
        uid=42,
        first_name="Ф" * 64,
        username="u" * 32,
        text="🔥" * 2048,
    )

    assert parsed_length(body) <= TELEGRAM_TEXT_LIMIT


# ── /ad command branches ─────────────────────────────────────────────────────


async def test_group_invocation_points_to_dm_and_sets_no_state() -> None:
    """Legacy ``bot.py:36861-36867``: in groups /ad only points to DMs."""
    msg = FakeMessage(chat_type="supergroup")
    state = FakeState()
    await handle_ad_command(
        msg,
        FakeBot(),
        state,
        FakeStatsRepo(),
        _settings(),
        _TZ,
        "ru",
    )
    assert len(msg.replies) == 1
    assert await state.get_state() is None


async def test_private_invocation_opens_form_and_enters_state() -> None:
    msg = FakeMessage()
    state = FakeState()
    await handle_ad_command(
        msg,
        FakeBot(member_count=42),
        state,
        FakeStatsRepo(),
        _settings(),
        _TZ,
        "ru",
    )
    assert await state.get_state() == AdsStates.awaiting_text.state
    data = await state.get_data()
    assert isinstance(data.get("ads_req_id"), int)
    assert "lang" in data
    # Exactly one form reply, and the audience numbers the handler just
    # collected actually reach the user: ``h_ads_form`` interpolates the
    # member count, so a form that renders without it is a broken form.
    assert len(msg.replies) == 1
    assert "42" in msg.replies[0]


async def test_private_invocation_blocked_by_cooldown() -> None:
    import time as _time

    ads_mod._last_request_at[100] = _time.time()
    msg = FakeMessage()
    state = FakeState()
    await handle_ad_command(
        msg,
        FakeBot(),
        state,
        FakeStatsRepo(),
        _settings(),
        _TZ,
        "ru",
    )
    assert await state.get_state() is None
    assert len(msg.replies) == 1


async def test_other_flow_state_not_hijacked() -> None:
    """User mid-withdraw must not have their FSM clobbered by /ad."""
    msg = FakeMessage()
    state = FakeState(state="WithdrawStates:awaiting_amount")
    await handle_ad_command(
        msg,
        FakeBot(),
        state,
        FakeStatsRepo(),
        _settings(),
        _TZ,
        "ru",
    )
    assert await state.get_state() == "WithdrawStates:awaiting_amount"


# ── Submit path ──────────────────────────────────────────────────────────────


async def test_submit_sends_admin_dm_then_stamps_cooldown() -> None:
    """Order contract (legacy ``bot.py:36903-36905``): cooldown only
    after a successful admin send."""
    msg = FakeMessage(text="Хочу рекламу, бюджет 5000")
    state = FakeState(state=AdsStates.awaiting_text.state, data={"ads_req_id": 3, "lang": "ru"})
    bot = FakeBot()
    await handle_ads_text(msg, state, bot, _settings(admin_chat_id=777), "ru")
    assert bot.sent and bot.sent[0][0] == 777
    assert "#3" in bot.sent[0][1]
    assert 100 in ads_mod._last_request_at
    assert state.cleared


async def test_submit_failure_does_not_stamp_cooldown() -> None:
    msg = FakeMessage(text="proposal")
    state = FakeState(state=AdsStates.awaiting_text.state, data={"ads_req_id": 4, "lang": "ru"})
    bot = FakeBot(fail_send=True)
    await handle_ads_text(msg, state, bot, _settings(admin_chat_id=777), "ru")
    assert 100 not in ads_mod._last_request_at
    assert state.cleared  # user is not stuck in FSM
    assert len(msg.replies) == 1


async def test_submit_without_admin_chat_refuses() -> None:
    """admin_chat_id == 0 → request must NOT be silently accepted."""
    msg = FakeMessage(text="proposal")
    state = FakeState(state=AdsStates.awaiting_text.state, data={"ads_req_id": 5, "lang": "ru"})
    bot = FakeBot()
    await handle_ads_text(msg, state, bot, _settings(admin_chat_id=0), "ru")
    assert not bot.sent
    assert 100 not in ads_mod._last_request_at
    assert state.cleared


# ── Cooldown table stays bounded ─────────────────────────────────────────────


def test_stamp_prunes_entries_whose_window_has_elapsed() -> None:
    """The table used to be write-only.

    Every accepted request added a ``user_id -> float`` and nothing ever
    removed one, in a process meant to run for months. An entry older
    than the cooldown can never block again — ``_cooldown_remaining_seconds``
    returns 0 for it forever — so it is pure leak, not stale state.
    """
    now = 1_000_000.0
    window = 24 * 3600
    # Three advertisers whose windows are long gone, one still live.
    ads_mod._last_request_at[1] = now - window - 1
    ads_mod._last_request_at[2] = now - window * 10
    ads_mod._last_request_at[3] = now - window  # exactly elapsed
    ads_mod._last_request_at[4] = now - 60  # live

    ads_mod._stamp_cooldown(5, now=now, cooldown_hours=24)

    assert set(ads_mod._last_request_at) == {4, 5}
    # The surviving live cooldown keeps its own timestamp, not the new one.
    assert ads_mod._last_request_at[4] == now - 60


def test_stamp_never_prunes_a_live_cooldown() -> None:
    """Pruning must not hand anyone a free request.

    The whole point of the table is the 24 h gate; an over-eager prune
    would silently disable it for the pruned user.
    """
    now = 1_000_000.0
    ads_mod._last_request_at[1] = now - 3600  # 1 h into a 24 h window

    ads_mod._stamp_cooldown(2, now=now, cooldown_hours=24)

    assert _cooldown_remaining_seconds(1, now=now, cooldown_hours=24, exempt=False) == 23 * 3600


def test_restamping_moves_the_user_to_the_end_of_the_table() -> None:
    """Insertion order must equal stamp order.

    ``dict`` keeps a re-assigned key in its ORIGINAL position, so a
    plain ``d[uid] = now`` would leave a frequently-re-stamped user at
    the head — and the ceiling eviction below, which drops the head,
    would then evict a LIVE cooldown while a staler one survived.
    """
    now = 1_000_000.0
    ads_mod._stamp_cooldown(1, now=now, cooldown_hours=24)
    ads_mod._stamp_cooldown(2, now=now, cooldown_hours=24)
    ads_mod._stamp_cooldown(1, now=now + 1, cooldown_hours=24)

    assert list(ads_mod._last_request_at) == [2, 1]


def test_table_is_capped_even_when_every_cooldown_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning alone is not a bound.

    If enough distinct users hold a live cooldown at once (or the
    cooldown is misconfigured to years, so nothing ever elapses), the
    prune removes nothing. The hard ceiling is what keeps the dict
    finite in that case; the oldest stamp is evicted first.
    """
    monkeypatch.setattr(ads_mod, "_MAX_TRACKED_USERS", 3)
    now = 1_000_000.0

    for uid in range(1, 6):
        ads_mod._stamp_cooldown(uid, now=now + uid, cooldown_hours=24)

    assert list(ads_mod._last_request_at) == [3, 4, 5]
