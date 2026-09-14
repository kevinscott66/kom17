"""#1877: a saved ticket is durable before the user is told it is.

All three write sites — ``/feedback``, the inline-arg ``/support <text>``
form and the FSM free-text form — INSERT the row and then make up to two
Telegram round-trips (the acknowledgement, then the admin DM). The admin
notification is documented as best-effort, but that contract only holds
if the row has already been committed: ``BaseSessionMiddleware`` commits
after the handler returns (``middlewares/base.py:131-132``), so until
this ticket a failure anywhere below the INSERT could roll back a report
the user had just been told was saved. The INSERT also holds
``BEGIN IMMEDIATE`` for the whole of both calls.

The FSM form has a second reason: its ``state.clear()`` and its row must
agree. Committing between the clear and the reply is what makes "your
text is gone AND you are out of the flow" impossible.

Stubs, not a session — the contract under test is the ORDER of
``checkpoint()`` against the first outgoing message.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiogram.filters import CommandObject

from telegram_invite_bot.handlers.support import (
    handle_feedback,
    handle_support_command,
    handle_support_text,
)

_UID = 777


class _FakeMessage:
    def __init__(self, text: str = "всё сломалось") -> None:
        self.chat = SimpleNamespace(id=_UID, type="private")
        self.from_user = SimpleNamespace(id=_UID, username="user", first_name="Alice", is_bot=False)
        self.text = text
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _TicketsRepoStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_open_ticket(self, **_kw: Any) -> int:  # noqa: ANN401
        self.calls.append("create_open_ticket")
        return 12


class _StateStub:
    """Only ``clear`` is reached on the happy FSM path."""

    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self) -> None:
        self.cleared += 1


def _recorder(message: _FakeMessage) -> tuple[Any, list[int]]:
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    return checkpoint, fired


async def test_feedback_commits_before_the_acknowledgement() -> None:
    """``/feedback <text>``: row first, ack second."""
    message = _FakeMessage()
    repo = _TicketsRepoStub()
    checkpoint, fired = _recorder(message)

    await handle_feedback(
        message,  # type: ignore[arg-type]
        CommandObject(prefix="/", command="feedback", args="всё сломалось"),
        None,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        0,
        "ru",
        checkpoint,
    )
    assert repo.calls == ["create_open_ticket"]
    assert fired == [0]
    assert len(message.replies) == 1


async def test_an_empty_feedback_never_reaches_the_checkpoint() -> None:
    """Refused before the INSERT: no row, no lock, nothing to commit."""
    message = _FakeMessage()
    repo = _TicketsRepoStub()
    checkpoint, fired = _recorder(message)

    await handle_feedback(
        message,  # type: ignore[arg-type]
        CommandObject(prefix="/", command="feedback", args=None),
        None,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        0,
        "ru",
        checkpoint,
    )
    assert repo.calls == []
    assert fired == []
    assert len(message.replies) == 1


async def test_the_inline_support_form_commits_before_the_ack() -> None:
    """``/support <text>`` takes the same path with a bigger cap."""
    message = _FakeMessage()
    repo = _TicketsRepoStub()
    checkpoint, fired = _recorder(message)

    await handle_support_command(
        message,  # type: ignore[arg-type]
        CommandObject(prefix="/", command="support", args="всё сломалось"),
        None,  # type: ignore[arg-type]
        _StateStub(),  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        0,
        None,  # type: ignore[arg-type]
        "ru",
        checkpoint,
    )
    assert repo.calls == ["create_open_ticket"]
    assert fired == [0]
    assert len(message.replies) == 1


async def test_the_fsm_form_commits_the_row_and_the_cleared_state_together() -> None:
    """The clear runs first, then the commit, then the ack — so the row
    and the FSM can never disagree about whether this happened."""
    message = _FakeMessage()
    repo = _TicketsRepoStub()
    state = _StateStub()
    checkpoint, fired = _recorder(message)

    await handle_support_text(
        message,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        0,
        None,  # type: ignore[arg-type]
        "ru",
        checkpoint,
    )
    assert repo.calls == ["create_open_ticket"]
    assert state.cleared == 1
    assert fired == [0]
    assert len(message.replies) == 1
