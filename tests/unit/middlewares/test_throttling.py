"""Unit tests for the per-user token-bucket throttling middleware.

The middleware is the only thing standing between a flood-bot and the
business-logic handlers (and the outbound Telegram API rate limit the
business logic shares with every other user). Regressions here are
silent — a broken throttle just lets traffic through — so the tests
pin the observable contract directly:

* burst up to ``capacity`` passes; the next event is dropped
* dropped events bump ``tib_throttled_total`` with the right
  ``event_type`` label
* a sleep equal to ``1/refill_per_second`` re-admits exactly one event
* anonymous events (no ``from_user``) bypass entirely
* per-user isolation — Alice flooding does not throttle Bob
* the LRU cap evicts the oldest entry, not the latest one
* a dropped ``CallbackQuery`` still gets answered (#217) so the
  inline button stops spinning, while a dropped message stays silent
* a ``successful_payment`` message is never dropped (#1814) — by the
  time it arrives Telegram has already taken the money
* nor is a ``refunded_payment`` (#1996) — it is the same kind of
  one-shot report, and dropping it silently reopens #1987
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    RefundedPayment,
    SuccessfulPayment,
    User,
)

from telegram_invite_bot.config.settings import ThrottlingConfig
from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
from telegram_invite_bot.webhook.metrics import THROTTLED_TOTAL


@dataclass(frozen=True, slots=True)
class _StubConfig:
    """Bypass ``ThrottlingConfig``'s pydantic floors (``max_tracked_users
    >= 100``, ``capacity >= 1``) so a single test can exercise the LRU
    boundary with a 3-slot map. Duck-typed against the attribute
    surface the middleware reads — nothing more.
    """

    enabled: bool = True
    capacity: int = 3
    refill_per_second: float = 1.0
    max_tracked_users: int = 100


class _StubUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _StubMessage:
    """Minimal duck-typed Message — aiogram doesn't validate the type
    of ``event`` at the middleware level, only its attribute surface.
    """

    def __init__(self, user_id: int | None) -> None:
        self.from_user = _StubUser(user_id) if user_id is not None else None


def _counter_value(event_type: str) -> float:
    for metric in THROTTLED_TOTAL.collect():
        for sample in metric.samples:
            if sample.labels.get("event_type") == event_type and sample.name.endswith("_total"):
                return float(sample.value)
    return 0.0


def _config(**overrides: Any) -> Any:
    # Use the stub so tests can probe boundary cases (tiny LRU cap,
    # sub-second refill) that the real ThrottlingConfig validator
    # rejects on purpose. The middleware itself reads the same
    # attribute surface either way.
    return _StubConfig(**overrides)


async def _noop_handler(_event: Any, _data: dict[str, Any]) -> str:
    return "ran"


def _callback(user_id: int) -> CallbackQuery:
    """A REAL ``CallbackQuery``, not a duck-typed stub.

    The middleware decides whether to answer with ``isinstance`` — a
    stub would silently take the message branch and the test would
    pass against a broken implementation.
    """
    return CallbackQuery(
        id=f"cbq-{user_id}",
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        chat_instance=f"ci-{user_id}",
        data="noop",
    )


def _message(user_id: int) -> Message:
    """A REAL ``Message``, for the same reason ``_callback`` is real: the
    middleware branches on ``isinstance``, so only a real type proves
    which branch a message takes.
    """
    return Message(
        message_id=user_id,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        text="hi",
    )


def _record_answers(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture ``CallbackQuery.answer`` calls without a Bot session.

    Patching the class (rather than the instance) is what a pydantic
    model allows, and it also catches an implementation that answers
    some other callback than the one it was handed.
    """
    calls: list[dict[str, Any]] = []

    async def fake_answer(
        self: CallbackQuery,
        text: str | None = None,
        **kwargs: Any,
    ) -> bool:
        calls.append({"id": self.id, "text": text, **kwargs})
        return True

    monkeypatch.setattr(CallbackQuery, "answer", fake_answer)
    return calls


@pytest.mark.asyncio
async def test_burst_within_capacity_all_pass() -> None:
    mw = ThrottlingMiddleware(_config(capacity=3))
    event = _StubMessage(user_id=42)
    results = [await mw(_noop_handler, event, {}) for _ in range(3)]
    # All three landed in the handler — bucket starts full and we charge
    # one per event.
    assert results == ["ran", "ran", "ran"]


@pytest.mark.asyncio
async def test_exhausted_bucket_drops_and_increments_metric() -> None:
    mw = ThrottlingMiddleware(_config(capacity=2, refill_per_second=0.001))
    event = _StubMessage(user_id=43)

    before = _counter_value("_stubmessage")
    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) == "ran"
    # Third event consumes from an empty bucket → dropped (returns None).
    assert await mw(_noop_handler, event, {}) is None
    after = _counter_value("_stubmessage")
    assert after == before + 1.0


@pytest.mark.asyncio
async def test_refill_readmits_after_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the time source instead of sleeping — keeps the test fast
    AND verifies the refill math (``elapsed * rate``) rather than
    just "long enough elapsed → fine".
    """
    clock = {"t": 1000.0}

    def fake_monotonic() -> float:
        return clock["t"]

    import telegram_invite_bot.middlewares.throttling as mod

    monkeypatch.setattr(mod, "monotonic", fake_monotonic)

    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=2.0))
    event = _StubMessage(user_id=44)

    # Capacity 1 → second immediate call drops.
    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) is None

    # 0.5s @ 2 tokens/sec = 1.0 token replenished — exactly enough.
    clock["t"] += 0.5
    assert await mw(_noop_handler, event, {}) == "ran"
    # And the next one drops again — refill is continuous, not stepped.
    assert await mw(_noop_handler, event, {}) is None


@pytest.mark.asyncio
async def test_anonymous_event_bypasses_throttle() -> None:
    """Channel posts, edited messages from deleted accounts, etc. have
    no ``from_user``. Throttling them by a synthetic key would punish
    the next genuine sender to share that key.
    """
    mw = ThrottlingMiddleware(_config(capacity=1))
    event = _StubMessage(user_id=None)
    # Many calls in a row — none drop.
    results = [await mw(_noop_handler, event, {}) for _ in range(10)]
    assert all(r == "ran" for r in results)


@pytest.mark.asyncio
async def test_per_user_isolation() -> None:
    """A flooding Alice must not consume Bob's tokens."""
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    alice = _StubMessage(user_id=100)
    bob = _StubMessage(user_id=200)

    assert await mw(_noop_handler, alice, {}) == "ran"
    assert await mw(_noop_handler, alice, {}) is None  # Alice exhausted
    # Bob still has a full bucket.
    assert await mw(_noop_handler, bob, {}) == "ran"


@pytest.mark.asyncio
async def test_disabled_short_circuits() -> None:
    mw = ThrottlingMiddleware(_config(enabled=False, capacity=1))
    event = _StubMessage(user_id=300)
    # Way past capacity, all pass — the middleware should be a no-op.
    results = [await mw(_noop_handler, event, {}) for _ in range(20)]
    assert all(r == "ran" for r in results)


@pytest.mark.asyncio
async def test_lru_evicts_oldest_when_cap_exceeded() -> None:
    """The bucket map is bounded — under a churn of unique user IDs we
    evict the least-recently-seen, not the freshest. Without this the
    process leaks memory in long-running group chats.
    """
    mw = ThrottlingMiddleware(_config(capacity=1, max_tracked_users=3))
    for uid in (1, 2, 3):
        await mw(_noop_handler, _StubMessage(user_id=uid), {})
    # Now insert a 4th: user 1 (oldest) must be evicted.
    await mw(_noop_handler, _StubMessage(user_id=4), {})
    assert 1 not in mw._buckets  # noqa: SLF001 — invariant check
    assert {2, 3, 4} == set(mw._buckets.keys())


@pytest.mark.asyncio
async def test_throttled_user_does_not_self_evict() -> None:
    """Flooding does not bypass the bucket via LRU eviction.

    Bug shape we're pinning against: if a throttled event skipped the
    ``move_to_end`` call, a single flooding bot under churn would
    eventually fall off the LRU and re-enter with a full bucket.
    """
    mw = ThrottlingMiddleware(_config(capacity=1, max_tracked_users=2))
    flooder = _StubMessage(user_id=500)
    other = _StubMessage(user_id=501)
    # Interleave: flooder, other, flooder (throttled, must still touch
    # recency), other, flooder (throttled). Without ``move_to_end`` on
    # the throttled path, the flooder would age past ``other`` and get
    # evicted on the next insert — then the next flooder event would
    # bypass the throttle entirely by re-initialising a full bucket.
    assert await mw(_noop_handler, flooder, {}) == "ran"
    assert await mw(_noop_handler, other, {}) == "ran"
    assert await mw(_noop_handler, flooder, {}) is None  # throttled
    assert await mw(_noop_handler, other, {}) is None  # throttled
    assert await mw(_noop_handler, flooder, {}) is None  # still throttled
    # And now a third user arriving should evict ``other`` (older
    # recency stamp), not the freshly-touched ``flooder``.
    await mw(_noop_handler, _StubMessage(user_id=502), {})
    assert 500 in mw._buckets  # noqa: SLF001
    assert 501 not in mw._buckets  # noqa: SLF001


def test_throttling_config_defaults_match_doc() -> None:
    """The real ``ThrottlingConfig`` (with all its validators) must
    instantiate with no env vars and produce the operator-facing
    defaults we documented in the config-module docstring. If somebody
    moves a number, the docstring and this test fail together.
    """
    cfg = ThrottlingConfig()
    assert cfg.enabled is True
    assert cfg.capacity == 10
    assert cfg.refill_per_second == 2.0
    assert cfg.max_tracked_users == 10_000


@pytest.mark.asyncio
async def test_uses_event_from_user_data_key_when_present() -> None:
    """aiogram populates ``event_from_user`` in middleware ``data``;
    prefer that over reaching into the event directly so we work
    correctly when the event type doesn't expose ``.from_user``
    (e.g. ChatJoinRequest in future extensions).
    """
    mw = ThrottlingMiddleware(_config(capacity=1))
    # Event with NO from_user attribute, but data carries the user.
    event = object()
    data = {"event_from_user": _StubUser(600)}
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) is None


@pytest.mark.asyncio
async def test_high_cost_handler_drains_bucket_faster() -> None:
    """M-I-3: a handler that declares ``throttle_cost=5`` consumes 5
    tokens per call. With capacity 10, the second call must drop
    (10 → 5 → -5 underflow → reject), whereas the default-cost (1)
    path takes 11 calls to drop.
    """
    mw = ThrottlingMiddleware(_config(capacity=10, refill_per_second=0.001))
    event = _StubMessage(user_id=700)

    data_high = {"throttle_cost": 5}
    # First call: 10 → 5 (allowed)
    assert await mw(_noop_handler, event, data_high) == "ran"
    # Second call: 5 → 0 (allowed)
    assert await mw(_noop_handler, event, data_high) == "ran"
    # Third call: not enough tokens → dropped
    assert await mw(_noop_handler, event, data_high) is None


@pytest.mark.asyncio
async def test_default_cost_is_one_per_call() -> None:
    """M-I-3 (negative): handlers without the ``throttle_cost`` flag
    keep the legacy uniform cost of 1. Burst of ``capacity`` passes;
    the next event drops — same as before the fix.
    """
    mw = ThrottlingMiddleware(_config(capacity=3, refill_per_second=0.001))
    event = _StubMessage(user_id=701)
    # No throttle_cost in data → default 1
    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) is None


@pytest.mark.asyncio
async def test_cost_clamped_to_capacity() -> None:
    """M-I-3: a handler that demands more tokens than total capacity
    is clamped — otherwise users would be permanently locked out by
    a single oversized call. First call still passes (cold start);
    second drops immediately.
    """
    mw = ThrottlingMiddleware(_config(capacity=3, refill_per_second=0.001))
    event = _StubMessage(user_id=702)
    data = {"throttle_cost": 100}
    assert await mw(_noop_handler, event, data) == "ran"
    # Bucket drained to 3 - 3 (clamped) = 0; next call drops.
    assert await mw(_noop_handler, event, data) is None


@pytest.mark.parametrize(
    ("cost", "label"),
    [(float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf")],
)
@pytest.mark.asyncio
async def test_non_finite_cost_falls_back_to_one(cost: float, label: str) -> None:
    """Every non-finite cost must read as the default cost of 1 — a
    burst of ``capacity`` passes and the next event drops.

    ``inf`` was the live defect: ``_allow`` clamps with
    ``max(1.0, min(cost, capacity))``, and ``min(inf, capacity)`` is
    ``capacity``, so one call drained the whole bucket and the user was
    locked out for the rest of the window. ``nan`` and ``-inf`` came out
    at 1.0 already — ``max(1.0, nan)`` is ``1.0`` because ``nan > 1.0``
    is ``False`` — but only by accident of that clamp's argument order;
    they are pinned here so a future rewrite of ``_allow`` can't quietly
    let ``nan`` through into the bucket, where every later comparison
    would be ``False`` and the user would never be throttled again.
    """
    mw = ThrottlingMiddleware(_config(capacity=3, refill_per_second=0.001))
    event = _StubMessage(user_id=hash(label) % 1000 + 7100)
    data = {"throttle_cost": cost}
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) is None


@pytest.mark.asyncio
async def test_absurd_finite_cost_falls_back_to_one() -> None:
    # Past the ceiling the flag is a decorator typo, not a real price.
    mw = ThrottlingMiddleware(_config(capacity=3, refill_per_second=0.001))
    event = _StubMessage(user_id=7200)
    data = {"throttle_cost": 1e12}
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) == "ran"
    assert await mw(_noop_handler, event, data) is None


@pytest.mark.asyncio
async def test_throttled_callback_is_answered_so_the_spinner_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#217: a dropped callback query must still be answered.

    Telegram keeps the inline button in a loading state for about
    fifteen seconds when a callback query goes unanswered, so the
    silent drop this middleware applies to messages reads as a frozen
    bot when applied to a button tap.

    The ack is deliberately language-neutral: the throttle is a
    dispatcher-level outer middleware (mounted in
    ``AppProvider.dispatcher``) and runs before ``LanguageMiddleware``
    (mounted in ``build_main_router``) has put ``lang`` into ``data``,
    so there is no locale to render into.

    Deliberately no ``cache_time``: the client would cache the answer
    and swallow a later, legitimate tap on the same button once the
    bucket has refilled — a transient limit would leave a permanently
    dead-looking button.
    """
    calls = _record_answers(monkeypatch)
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    event = _callback(user_id=7300)

    before = _counter_value("callbackquery")
    assert await mw(_noop_handler, event, {}) == "ran"
    # The allowed tap reaches its handler, which owns the answer — the
    # middleware must not have answered on that path.
    assert calls == []

    assert await mw(_noop_handler, event, {}) is None
    assert _counter_value("callbackquery") == before + 1.0
    assert len(calls) == 1
    assert calls[0]["id"] == "cbq-7300"
    assert calls[0]["text"] == "⏳"
    assert "cache_time" not in calls[0]


@pytest.mark.asyncio
async def test_throttled_message_stays_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The #217 exception is scoped to callbacks only.

    A reply to a flooding client is a genuine amplifier and nothing in
    the protocol is waiting on it, so messages keep the silent drop the
    module docstring argues for.
    """
    calls = _record_answers(monkeypatch)
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    event = _message(user_id=7301)

    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) is None
    assert calls == []


@pytest.mark.asyncio
async def test_failed_ack_does_not_break_the_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A callback query that expired between the tap and the ack raises
    ``TelegramBadRequest``. The drop is the contract; the ack is
    cosmetic, so the error must not escape the middleware — and the
    metric must still tick.
    """

    async def failing_answer(self: CallbackQuery, *_args: Any, **_kwargs: Any) -> bool:
        raise TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id=self.id),
            message="query is too old",
        )

    monkeypatch.setattr(CallbackQuery, "answer", failing_answer)
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    event = _callback(user_id=7302)

    before = _counter_value("callbackquery")
    assert await mw(_noop_handler, event, {}) == "ran"
    assert await mw(_noop_handler, event, {}) is None
    assert _counter_value("callbackquery") == before + 1.0


def _paid_message(user_id: int, *, stars: int = 100) -> Message:
    """A REAL ``Message`` carrying a REAL ``SuccessfulPayment``.

    Duck-typing would defeat the point here: the bypass has to key off
    the attribute an actual Telegram update carries, and a stub that
    merely *has* an attribute named ``successful_payment`` would pass
    against an implementation that keys off something else entirely.
    """
    return Message(
        message_id=user_id,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=stars,
            invoice_payload=f"stars:{stars}",
            telegram_payment_charge_id=f"tg-{user_id}",
            provider_payment_charge_id=f"pp-{user_id}",
        ),
    )


@pytest.mark.asyncio
async def test_successful_payment_is_never_dropped() -> None:
    """#1814: by the time this update exists, the money is already gone.

    ``successful_payment`` is Telegram's report of a completed charge,
    not a request we are free to refuse. It arrives on the ``message``
    observer like any other message, so the empty bucket that a user
    drains by mashing the topup keyboard — every tap on the same shared
    bucket, capacity 10 and refill 2/s on prod — is empty exactly when
    the payment lands a moment later. Dropped, the chain never reaches
    ``handlers/topup.py``: Telegram keeps the Stars, the buyer gets no
    coins, and nothing alerts the owner, because the alert is a thing
    the handler sends. There is no redelivery — Telegram sends this
    update once.
    """
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    payer = 7310

    # Drain the bucket the way a real payer does: with ordinary traffic
    # on the same key, not with the payment itself.
    assert await mw(_noop_handler, _message(payer), {}) == "ran"
    assert await mw(_noop_handler, _message(payer), {}) is None

    before = _counter_value("message")
    assert await mw(_noop_handler, _paid_message(payer), {}) == "ran"
    # Not merely delivered — not counted as backpressure either. A
    # nonzero ``tib_throttled_total`` is the operator's flood alarm,
    # and a payment must not show up in it.
    assert _counter_value("message") == before


@pytest.mark.asyncio
async def test_successful_payment_bypass_does_not_refund_the_bucket() -> None:
    """The bypass must be an exemption, not a bucket reset.

    Implementing it as "clear this user's bucket" would deliver the
    payment *and* hand a flooding client a full bucket back, on an
    update the client can trigger for the price of one Star. So the
    ordinary message that follows a bypassed payment must still be
    dropped.
    """
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    payer = 7311

    assert await mw(_noop_handler, _message(payer), {}) == "ran"
    assert await mw(_noop_handler, _message(payer), {}) is None
    assert await mw(_noop_handler, _paid_message(payer), {}) == "ran"
    assert await mw(_noop_handler, _message(payer), {}) is None


@pytest.mark.asyncio
async def test_successful_payment_survives_a_bucket_drained_by_taps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realistic drain is callbacks, not messages.

    ``di/providers.py`` mounts ONE ``ThrottlingMiddleware`` instance on
    both ``dispatcher.message`` and ``dispatcher.callback_query`` — on
    purpose, so a single human shares one bucket across both. That is
    also what makes this reachable: the buyer empties the bucket by
    tapping through the topup keyboard, then the payment arrives on the
    message side a moment later against a bucket somebody else's event
    type drained.
    """
    _record_answers(monkeypatch)
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    payer = 7312

    assert await mw(_noop_handler, _callback(payer), {}) == "ran"
    assert await mw(_noop_handler, _callback(payer), {}) is None
    assert await mw(_noop_handler, _paid_message(payer), {}) == "ran"


def _refunded_message(user_id: int, *, stars: int = 100) -> Message:
    """A REAL ``Message`` carrying a REAL ``RefundedPayment``.

    Same reasoning as :func:`_paid_message`: the exemption has to key
    off the attribute a real Telegram update carries. ``provider_
    payment_charge_id`` is omitted because Telegram omits it for Stars
    — the shape ``tests/e2e/handlers/test_stars_refund.py`` already
    pins.
    """
    return Message(
        message_id=user_id,
        date=datetime(2026, 1, 1, tzinfo=UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T"),
        refunded_payment=RefundedPayment(
            currency="XTR",
            total_amount=stars,
            invoice_payload=f"stars:{stars}",
            telegram_payment_charge_id=f"tg-{user_id}",
        ),
    )


@pytest.mark.asyncio
async def test_refunded_payment_is_never_dropped() -> None:
    """#1996: the reversal is a one-shot report too, and it is the
    only thing that undoes a credit.

    #1814 exempted ``successful_payment`` with an argument that reads
    identically for the other half of the same charge: Telegram has
    already acted, the update is delivered exactly once, nothing
    redelivers it, and everything the bot does about it happens in the
    handler this middleware stands in front of. Dropped here,
    ``handlers/topup.py::handle_refunded_payment`` never runs, so
    ``processed_webhooks.reversed_at`` stays NULL and
    ``TransactionsRepo.lifetime_deposits`` keeps counting refunded
    money as a deposit toward the withdrawal gate — the exact loss its
    own docstring names, "refund, withdraw, repeat". No owner alert is
    sent either, because the alert is a thing that handler sends, so
    the failure is silent on both ends.

    Reachable without flooding intent, and by the one party who
    benefits: the payer controls both halves. Drain your own bucket
    with ordinary taps, ask Telegram for the refund, keep the bucket
    empty for the seconds it takes to arrive.
    """
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    payer = 7313

    assert await mw(_noop_handler, _message(payer), {}) == "ran"
    assert await mw(_noop_handler, _message(payer), {}) is None

    before = _counter_value("message")
    assert await mw(_noop_handler, _refunded_message(payer), {}) == "ran"
    # Same as the payment: not counted as backpressure either, or the
    # operator's flood alarm learns to ring for ordinary refunds.
    assert _counter_value("message") == before


@pytest.mark.asyncio
async def test_refunded_payment_bypass_does_not_refund_the_bucket() -> None:
    """An exemption, not a bucket reset — the twin of the payment case.

    A refund is even cheaper to provoke than a payment (it costs the
    client nothing at all), so handing back a full bucket here would
    be a free throttle reset on demand.
    """
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    payer = 7314

    assert await mw(_noop_handler, _message(payer), {}) == "ran"
    assert await mw(_noop_handler, _message(payer), {}) is None
    assert await mw(_noop_handler, _refunded_message(payer), {}) == "ran"
    assert await mw(_noop_handler, _message(payer), {}) is None


@pytest.mark.asyncio
async def test_refunded_payment_survives_a_bucket_drained_by_taps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The realistic drain is callbacks here too.

    One ``ThrottlingMiddleware`` instance spans ``dispatcher.message``
    and ``dispatcher.callback_query`` (``di/providers.py``), so the
    bucket the refund meets is the one the payer emptied by tapping.
    """
    _record_answers(monkeypatch)
    mw = ThrottlingMiddleware(_config(capacity=1, refill_per_second=0.001))
    payer = 7315

    assert await mw(_noop_handler, _callback(payer), {}) == "ran"
    assert await mw(_noop_handler, _callback(payer), {}) is None
    assert await mw(_noop_handler, _refunded_message(payer), {}) == "ran"
