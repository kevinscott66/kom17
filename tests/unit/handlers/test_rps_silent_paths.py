"""The /cpc paths that used to end in silence (#122).

Two families, both in ``handlers/rps.py``:

**Legacy callback shims.** The deploy-window shims match on prefix +
colon count (``rps_acc:``/2 colons, ``rps_dec:``/1, ``rps_mv:``/2) and
are registered after the new-format handlers, so a payload that gets
this far belongs to nobody else. When the id inside it doesn't parse,
or the card's message reference is gone, the shims used to ``return``
without answering — and an unanswered ``callback_query`` leaves
Telegram's little clock spinning on the button until it times out.
The tapper sees a frozen card and no reason for it.

**Timeout sweepers.** Both expiry callbacks used to bail at the first
line when ``opponent_id`` was missing from the FSM data, leaving the
challenger — whose id the storage key carries unconditionally —
waiting on a match that had already expired. Only the opponent-side
half genuinely needs the id.

The same tests now also pin the #126-fp posture: the callback owns no
lock-registry bookkeeping at all. Both the drop these callbacks used
to do (too early — it popped the slot while the FSM was still
populated and the callback still doing Telegram I/O, long enough for
a click to build a second lock over a live match) and the
``after_clear`` hook that briefly replaced it are gone. The registry
is a refcounted :class:`KeyedLocks`; the slot belongs to whoever is
inside :func:`rps._match_lock_cm`, and these callbacks are not.

Both families are exercised with duck-typed stubs: the shims touch
only ``.data`` / ``.answer`` / ``.message`` before their early return,
and the sweepers only ``bot.id`` / ``.send_message`` /
``.edit_message_reply_markup``.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.fsm.storage.base import StorageKey

from telegram_invite_bot.handlers import rps as rps_mod
from telegram_invite_bot.i18n import t

handle_rps_accept_legacy: Any = rps_mod.handle_rps_accept_legacy
handle_rps_decline_legacy: Any = rps_mod.handle_rps_decline_legacy
handle_rps_move_legacy: Any = rps_mod.handle_rps_move_legacy
on_expire_awaiting_acceptance: Any = rps_mod.on_expire_awaiting_acceptance
on_expire_awaiting_moves: Any = rps_mod.on_expire_awaiting_moves

BOT_ID = 42
GROUP_CHAT_ID = -1001
CHALLENGER_ID = 100
OPPONENT_ID = 200


# ── Fakes ────────────────────────────────────────────────────────────


class FakeCallback:
    """Callback stub: records ``answer`` calls, carries a payload.

    ``message`` defaults to ``None`` — the inaccessible-message case.
    ``_legacy_chat_id`` narrows with ``isinstance(msg, Message)``, so a
    stub can only ever exercise the ``None`` branch; the happy path is
    covered end-to-end in ``tests/e2e/handlers/test_rps.py``.
    """

    def __init__(self, data: str) -> None:
        self.data = data
        self.message: Any = None
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **_kw: Any) -> None:
        self.answers.append((text, show_alert))


class FakeBot:
    """Bot stub: records DMs and keyboard drops."""

    id = BOT_ID

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.dropped: list[tuple[int, int]] = []

    async def send_message(self, chat_id: int, text: str, **_kw: Any) -> None:
        self.sent.append((chat_id, text))

    async def edit_message_reply_markup(
        self, *, chat_id: int, message_id: int, reply_markup: Any = None
    ) -> None:
        self.dropped.append((chat_id, message_id))


def _key() -> StorageKey:
    return StorageKey(bot_id=BOT_ID, chat_id=GROUP_CHAT_ID, user_id=CHALLENGER_ID)


def _legacy_kwargs(handler_name: str) -> dict[str, Any]:
    """Injected arguments for one legacy shim, all of them inert.

    Every case below returns from the shim's OWN guards — an unparseable
    payload, a card that is gone, a new-format payload the shim must not
    answer — so none of these ever reaches the core that would use them.
    ``None`` is the point: a stub with behaviour would let a shim that
    silently fell through look like one that refused correctly.

    ``game_limit_service`` joined the accept shim in #1664, when
    accepting a challenge started costing the acceptor a slot of the
    shared per-user game budget.
    """
    kwargs: dict[str, Any] = {"bot": FakeBot(), "fsm_storage": None, "lang": "ru"}
    if handler_name == "handle_rps_accept_legacy":
        kwargs |= {"game_limit_service": None}
    if handler_name == "handle_rps_move_legacy":
        kwargs |= {"rps_service": None, "bonds_write_repo": None}
    return kwargs


# ── Legacy shims: a payload nobody else will answer ───────────────────


@pytest.mark.parametrize(
    ("handler_name", "payload"),
    [
        ("handle_rps_accept_legacy", "rps_acc:notanint:50"),
        ("handle_rps_accept_legacy", "rps_acc:100:notanint"),
        ("handle_rps_decline_legacy", "rps_dec:notanint"),
        ("handle_rps_move_legacy", "rps_mv:notanint:rock"),
    ],
)
async def test_unparseable_legacy_payload_gets_a_toast(handler_name: str, payload: str) -> None:
    callback = FakeCallback(payload)
    handler: Any = getattr(rps_mod, handler_name)
    kwargs = _legacy_kwargs(handler_name)

    await handler(callback, **kwargs)

    assert callback.answers == [(t("h_rps_match_not_found", "ru"), False)]


@pytest.mark.parametrize(
    ("handler_name", "payload"),
    [
        ("handle_rps_accept_legacy", "rps_acc:100:50"),
        ("handle_rps_decline_legacy", "rps_dec:100"),
        ("handle_rps_move_legacy", "rps_mv:100:rock"),
    ],
)
async def test_lost_message_reference_gets_a_toast(handler_name: str, payload: str) -> None:
    """A parseable payload whose card is gone can't be resolved either —
    the legacy format derives ``chat_id`` from the message it hangs on.
    """
    callback = FakeCallback(payload)
    handler: Any = getattr(rps_mod, handler_name)
    kwargs = _legacy_kwargs(handler_name)

    await handler(callback, **kwargs)

    assert callback.answers == [(t("h_rps_match_not_found", "ru"), False)]


@pytest.mark.parametrize(
    ("handler_name", "payload"),
    [
        ("handle_rps_accept_legacy", "rps_acc:100:50:-1001"),
        ("handle_rps_decline_legacy", "rps_dec:100:-1001"),
        ("handle_rps_move_legacy", "rps_mv:100:rock:-1001"),
    ],
)
async def test_new_format_payload_stays_silent_in_the_shim(handler_name: str, payload: str) -> None:
    """The colon-count guard must NOT toast.

    Through the router this is unreachable — the registration filters on
    the legacy colon count, and the new-format ``CallbackData`` handler
    is registered first. Called directly it must still do nothing, or a
    future re-registration would double-answer every live card.
    """
    callback = FakeCallback(payload)
    handler: Any = getattr(rps_mod, handler_name)
    kwargs = _legacy_kwargs(handler_name)

    await handler(callback, **kwargs)

    assert callback.answers == []


# ── Sweepers: the challenger's half never depended on opponent_id ─────


@pytest.mark.parametrize(
    ("sweeper_name", "notice_key"),
    [
        ("on_expire_awaiting_acceptance", "h_rps_timeout_accept_challenger"),
        ("on_expire_awaiting_moves", "h_rps_timeout_moves_challenger"),
    ],
)
async def test_missing_opponent_id_still_tells_the_challenger(
    sweeper_name: str, notice_key: str
) -> None:
    bot = FakeBot()
    sweeper: Any = getattr(rps_mod, sweeper_name)

    await sweeper(bot, _key(), {"lang": "ru"})

    assert bot.sent == [(CHALLENGER_ID, t(notice_key, "ru"))]


@pytest.mark.parametrize(
    "sweeper_name",
    ["on_expire_awaiting_acceptance", "on_expire_awaiting_moves"],
)
async def test_non_int_opponent_id_is_treated_as_missing(sweeper_name: str) -> None:
    """FSM data is untyped storage — a string id must not crash the sweep."""
    bot = FakeBot()
    sweeper: Any = getattr(rps_mod, sweeper_name)

    await sweeper(bot, _key(), {"lang": "ru", "opponent_id": "200"})

    assert [chat_id for chat_id, _ in bot.sent] == [CHALLENGER_ID]


@pytest.mark.parametrize(
    ("sweeper_name", "challenger_key", "opponent_key"),
    [
        (
            "on_expire_awaiting_acceptance",
            "h_rps_timeout_accept_challenger",
            "h_rps_timeout_accept_opponent",
        ),
        (
            "on_expire_awaiting_moves",
            "h_rps_timeout_moves_challenger",
            "h_rps_timeout_moves_opponent",
        ),
    ],
)
async def test_both_seats_notified_when_opponent_id_is_present(
    sweeper_name: str, challenger_key: str, opponent_key: str
) -> None:
    bot = FakeBot()
    sweeper: Any = getattr(rps_mod, sweeper_name)

    await sweeper(bot, _key(), {"lang": "ru", "opponent_id": OPPONENT_ID})

    assert bot.sent == [
        (CHALLENGER_ID, t(challenger_key, "ru")),
        (OPPONENT_ID, t(opponent_key, "ru")),
    ]


async def test_moves_sweeper_drops_the_challengers_own_keyboard_without_opponent() -> None:
    """The challenger's move buttons hang on a message id the FSM knows
    regardless of the opponent — dropping them was collateral damage of
    the old early bailout.
    """
    bot = FakeBot()

    await on_expire_awaiting_moves(bot, _key(), {"lang": "ru", "challenger_move_message_id": 555})

    assert bot.dropped == [(CHALLENGER_ID, 555)]


@pytest.mark.parametrize(
    "sweeper_name",
    ["on_expire_awaiting_acceptance", "on_expire_awaiting_moves"],
)
async def test_the_sweeper_callbacks_touch_no_lock_registry_slot(
    sweeper_name: str,
) -> None:
    """#126-fp: the drop AND the hook that replaced it are both gone.

    The callbacks run under :func:`rps.expiry_guard`, which is the one
    thing holding the slot; called bare, as here, they must neither
    create a slot (a leak — one ``asyncio.Lock`` per abandoned
    challenge, alive until restart) nor need one to exist.
    """
    bot = FakeBot()
    sweeper: Any = getattr(rps_mod, sweeper_name)

    await sweeper(bot, _key(), {"lang": "ru", "opponent_id": OPPONENT_ID})

    assert len(rps_mod._match_locks) == 0
