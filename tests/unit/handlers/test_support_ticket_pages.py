"""#120 — the two ticket listings are bounded, not merely short.

``/my_tickets`` prints ten rows and ``/admin_tickets`` twenty, each row
carrying an 80-character preview of user-written text plus, on the admin
side, a 32-character username. Neither overflows today: measured in the
length Telegram actually counts (parsed, not raw markup) the worst cases
come to roughly 1.2k and 3.1k of the 4096 ceiling. What they lacked was
a ceiling of their own — row count, preview width and the row copy are
constants, and the failure mode past the wall is the bad kind: Telegram
does not truncate an over-long message, it refuses it with a 400, so the
reader gets nothing at all rather than a shortened list.

Both now go through :func:`paginate_lines`, the same treatment
``/filter_list``, ``/warnings`` and ``/group_stats`` already carry. These
tests pin the ceiling, that nothing is dropped on the way to it, and
that the developer gate survived the rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from telegram_invite_bot.handlers.support import (
    handle_admin_tickets,
    handle_my_tickets,
)
from telegram_invite_bot.utils.render import (
    PAGE_MAX,
    TELEGRAM_TEXT_LIMIT,
    parsed_length,
)

_USER = 555


class _Ticket:
    def __init__(self, ticket_id: int, *, text: str, status: str = "open") -> None:
        self.id = ticket_id
        self.user_id = _USER
        self.username = "u" * 32
        self.status = status
        self.text = text
        self.created_at = datetime(2026, 8, 13, tzinfo=UTC)


class _Repo:
    """Repo stand-in returning a fixed page of tickets."""

    def __init__(self, tickets: list[_Ticket]) -> None:
        self._tickets = tickets

    async def list_by_user(self, user_id: int, *, limit: int) -> list[_Ticket]:  # noqa: ARG002
        return self._tickets[:limit]

    async def list_open(self, *, limit: int) -> list[_Ticket]:
        return self._tickets[:limit]


class _User:
    id = _USER
    is_bot = False


class _Message:
    """Captures every outgoing chunk in send order."""

    def __init__(self) -> None:
        self.from_user = _User()
        self.sent: list[str] = []

    async def reply(self, text: str, **_: Any) -> None:
        self.sent.append(text)

    async def answer(self, text: str, **_: Any) -> None:
        self.sent.append(text)


class _Settings:
    def __init__(self, *, developer: bool = True) -> None:
        self.bot = self
        self._developer = developer

    def is_developer(self, user_id: int) -> bool:  # noqa: ARG002 — signature parity
        return self._developer


def _worst_case(count: int) -> list[_Ticket]:
    # 80 characters is the preview slice; Cyrillic because a preview of
    # ASCII understates nothing but a preview of markup-looking text is
    # what actually inflates the rendered string after escaping.
    return [_Ticket(10_000 + i, text="я<&>" * 40, status="in_progress") for i in range(count)]


@pytest.mark.asyncio
async def test_my_tickets_pages_stay_under_the_telegram_ceiling() -> None:
    message = _Message()
    await handle_my_tickets(cast("Any", message), cast("Any", _Repo(_worst_case(10))), "ru")
    assert message.sent
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in message.sent)


@pytest.mark.asyncio
async def test_admin_tickets_pages_stay_under_the_telegram_ceiling() -> None:
    message = _Message()
    await handle_admin_tickets(
        cast("Any", message),
        cast("Any", _Repo(_worst_case(20))),
        cast("Any", _Settings()),
        "ru",
    )
    assert message.sent
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in message.sent)


@pytest.mark.asyncio
async def test_admin_tickets_drops_no_row_on_the_way_to_the_ceiling() -> None:
    # A ceiling that silently loses tickets would be worse than the 400
    # it replaces: every id the repo returned must appear somewhere in
    # the pages, and the whole listing must stay inside PAGE_MAX.
    tickets = _worst_case(20)
    message = _Message()
    await handle_admin_tickets(
        cast("Any", message),
        cast("Any", _Repo(tickets)),
        cast("Any", _Settings()),
        "ru",
    )
    joined = "\n".join(message.sent)
    assert all(f"#{ticket.id}" in joined for ticket in tickets)
    assert len(message.sent) <= PAGE_MAX


@pytest.mark.asyncio
async def test_a_short_listing_is_still_one_message() -> None:
    # Pagination must not split a listing that never needed splitting —
    # three tickets is the common case and it stays a single reply.
    message = _Message()
    await handle_my_tickets(cast("Any", message), cast("Any", _Repo(_worst_case(3))), "ru")
    assert len(message.sent) == 1


@pytest.mark.asyncio
async def test_admin_tickets_still_refuses_a_non_developer() -> None:
    # The pagination rewrite must not have moved the gate: ticket bodies
    # are user DMs and a chat admin is not a bot operator.
    message = _Message()
    await handle_admin_tickets(
        cast("Any", message),
        cast("Any", _Repo(_worst_case(20))),
        cast("Any", _Settings(developer=False)),
        "ru",
    )
    assert len(message.sent) == 1
    assert "администратор" in message.sent[0].lower()
