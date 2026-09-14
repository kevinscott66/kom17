"""Where the couple-activity money path ends its transaction (#1869).

``economy.hold`` is a guarded ``UPDATE``, so it takes the economy write
lock even on the branch where it matches no row and returns ``None``.
Both refusal branches then talk to Telegram — a ``show_alert`` popup —
while that lock, and the users.db slot the bond lookup opened, are still
held. Nothing is owed on either branch (the no-coins branch escrowed
nothing; the bond-gone branch already handed the escrow back), so the
transaction can and should end before the popup.

The success path is the deliberate exception and the last test here
pins it: an undeliverable result card must refund the whole activity,
which only works while the transaction is still open. See
``tests/e2e/handlers/test_couple_activities.py``'s
``test_undeliverable_result_rolls_back_the_whole_activity``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from telegram_invite_bot.handlers import couple_activities as mod
from telegram_invite_bot.keyboards.builders import CoupleActivity

if TYPE_CHECKING:
    import pytest

    from telegram_invite_bot.db import Checkpoint

_CHAT = -100
_USER = 10
_PARTNER = 20


class _FakeUser:
    id = _USER
    first_name = "Alice"


class _FakeChat:
    id = _CHAT


class _FakeMessage:
    chat = _FakeChat()


class _FakeCallback:
    """Records the popups; that is the only wire traffic these branches make."""

    def __init__(self) -> None:
        self.from_user = _FakeUser()
        self.message = _FakeMessage()
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, **_kw: Any) -> None:
        self.answers.append(text)


class _Bond:
    """Enough of a marriage/relationship row for both helpers."""

    user1_id = _USER
    user2_id = _PARTNER
    experience = 10_000


class _BondsStub:
    def __init__(self, *, xp_result: int | None) -> None:
        self.xp_result = xp_result
        self.calls: list[str] = []

    async def get_marriage(self, *_a: object) -> _Bond:
        return _Bond()

    async def get_relationship(self, *_a: object) -> _Bond:
        return _Bond()

    def _rel_xp_to_level(self, _experience: int) -> int:
        # Above every activity's ``min_level`` so the gate is never the
        # reason a test stops early.
        return 11

    async def add_marriage_xp(self, *_a: object) -> int | None:
        self.calls.append("add_marriage_xp")
        return self.xp_result

    async def add_relationship_xp(self, *_a: object) -> int | None:
        self.calls.append("add_relationship_xp")
        return self.xp_result

    async def log_marriage_activity(self, *_a: object) -> None:
        self.calls.append("log_marriage_activity")

    async def log_relationship_activity(self, *_a: object) -> None:
        self.calls.append("log_relationship_activity")

    async def get_first_name(self, _uid: int) -> str:
        self.calls.append("get_first_name")
        return "Bob"


class _EconomyStub:
    def __init__(self, *, funded: bool) -> None:
        self.funded = funded
        self.calls: list[str] = []

    async def hold(self, *_a: object, **_kw: object) -> object | None:
        self.calls.append("hold")
        return object() if self.funded else None

    async def release(self, *_a: object, **_kw: object) -> object:
        self.calls.append("release")
        return object()

    async def settle_hold(self, *_a: object, **_kw: object) -> None:
        self.calls.append("settle_hold")


def _recorder(callback: _FakeCallback) -> tuple[Any, list[int]]:
    """A checkpoint that records how many popups had gone out when it fired."""
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(callback.answers))

    return checkpoint, fired


async def _run(
    callback: _FakeCallback,
    bonds: _BondsStub,
    economy: _EconomyStub,
    checkpoint: Any,
    *,
    kind: str,
    key: str,
) -> None:
    data = CoupleActivity(kind=kind, key=key, partner_id=_PARTNER, owner_id=_USER)
    await mod.handle_couple_activity(
        cast("Any", callback),
        data,
        cast("Any", bonds),
        cast("Any", economy),
        "ru",
        cast("Checkpoint", checkpoint),
    )


# ---------------------------------------------------------------------------
# Refusal 1: the wallet could not cover the activity
# ---------------------------------------------------------------------------


async def test_a_marriage_refusal_commits_before_the_popup() -> None:
    """``hold`` locked economy.db to tell us "no" — let go before the alert."""
    callback = _FakeCallback()
    bonds = _BondsStub(xp_result=0)
    economy = _EconomyStub(funded=False)
    checkpoint, fired = _recorder(callback)

    await _run(callback, bonds, economy, checkpoint, kind="marry", key="dinner")

    assert economy.calls == ["hold"]  # nothing was escrowed
    assert fired == [0]  # committed before the popup
    assert len(callback.answers) == 1


async def test_a_relationship_refusal_commits_before_the_popup() -> None:
    """The relationship branch takes the same lock for the same refusal."""
    callback = _FakeCallback()
    bonds = _BondsStub(xp_result=0)
    economy = _EconomyStub(funded=False)
    checkpoint, fired = _recorder(callback)

    await _run(callback, bonds, economy, checkpoint, kind="rel", key="cinema")

    assert economy.calls == ["hold"]
    assert fired == [0]
    assert len(callback.answers) == 1


# ---------------------------------------------------------------------------
# Refusal 2: the bond vanished between the gate and the XP grant
# ---------------------------------------------------------------------------


async def test_the_marriage_refund_is_committed_before_the_popup() -> None:
    """The escrow is handed back, and the audit pair is made durable first.

    Rolling this branch back would net to zero as well, but it would
    erase both ledger legs — a refunded charge would become
    indistinguishable from one that never happened.
    """
    callback = _FakeCallback()
    bonds = _BondsStub(xp_result=None)  # the pair row is gone
    economy = _EconomyStub(funded=True)
    checkpoint, fired = _recorder(callback)

    await _run(callback, bonds, economy, checkpoint, kind="marry", key="dinner")

    assert economy.calls == ["hold", "release"]  # never settled
    assert fired == [0]  # after the refund, before the popup
    assert len(callback.answers) == 1


async def test_the_relationship_refund_is_committed_before_the_popup() -> None:
    """Same shape on the relationship side."""
    callback = _FakeCallback()
    bonds = _BondsStub(xp_result=None)
    economy = _EconomyStub(funded=True)
    checkpoint, fired = _recorder(callback)

    await _run(callback, bonds, economy, checkpoint, kind="rel", key="cinema")

    assert economy.calls == ["hold", "release"]
    assert fired == [0]
    assert len(callback.answers) == 1


# ---------------------------------------------------------------------------
# The success path keeps its rollback window on purpose
# ---------------------------------------------------------------------------


async def test_the_success_path_never_reaches_a_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committing here would silently drop the undeliverable-card refund.

    Charging a couple for an activity nobody can see is the worst of the
    three possible endings, and the only thing preventing it is the
    session middleware rolling the spend and the XP back together when
    the card cannot be delivered. That needs the transaction still open,
    so the success path deliberately carries no ``await checkpoint()``.
    Add one and this test fails while the four above still pass.
    """
    drawn: list[str] = []

    async def _fake_render(_callback: object, **kwargs: object) -> None:
        drawn.append(str(kwargs["kind"]))

    monkeypatch.setattr(mod, "_render_done", _fake_render)

    callback = _FakeCallback()
    bonds = _BondsStub(xp_result=500)
    economy = _EconomyStub(funded=True)
    checkpoint, fired = _recorder(callback)

    await _run(callback, bonds, economy, checkpoint, kind="marry", key="dinner")

    # The whole success path really ran — escrow settled, activity
    # logged, card drawn — so the empty ``fired`` below is not passing
    # because the handler bailed out early.
    assert economy.calls == ["hold", "settle_hold"]
    assert "log_marriage_activity" in bonds.calls
    assert drawn == ["marry"]
    assert fired == []
