"""#1948: a /warn the admin was told failed must not leave a warning.

``ModerationRepo.add_warning`` is a three-step write — INSERT the
warning, ``flush`` for its id, COUNT the active rows, then queue the
audit-log row. ``handlers.moderation.handle_warn`` wraps the call in
``except Exception``, replies ``h_mod_warn_fail`` and returns NORMALLY
(moderation.py:1807). :class:`BaseSessionMiddleware` rolls back only on
a RAISED exception, so a return leaves whatever the failed call already
flushed sitting in the transaction the middleware then COMMITS.

Two failure modes, both measured rather than assumed:

* anything raising after the ``flush`` — the count, or the audit row —
  leaves the ``warnings`` INSERT pending. A Core statement failure does
  NOT deactivate the SQLAlchemy transaction (unlike a failed *flush*),
  so the middleware's commit succeeds and the user really is warned,
  with no audit-log row and an admin who was told nothing happened.
* a failed ``flush`` DOES deactivate it, and then the middleware's
  ``commit()`` raises ``PendingRollbackError`` on the way out — an
  unhandled crash logged for an update the handler had already
  reported cleanly, taking any other moderation-DB write of the same
  update with it.

The fix is the house R15 idiom, spelled out at
``support_tickets_repo.create_open_ticket`` for exactly this reason:
put the write in a SAVEPOINT so raising is all it does. That closes
both modes at once and keeps the handler's swallow honest.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.handlers import moderation as moderation_mod
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo

_CHAT = -1001
_ADMIN = 7001
_TARGET = 8002


@pytest.fixture
async def session() -> Any:
    """A real ``moderation.db`` session — the bug is about what commits.

    Built straight on ``create_async_engine`` rather than through
    ``EngineRegistry``: this file asserts on transaction state, and the
    registry's pragmas and write-promotion listener are not part of the
    contract under test.
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(ModerationBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened:
        yield opened
    await engine.dispose()


async def _warnings(session: AsyncSession) -> list[int]:
    rows = await session.execute(text("SELECT user_id FROM warnings"))
    return [int(v) for v in rows.scalars().all()]


async def _modlog(session: AsyncSession) -> list[str]:
    rows = await session.execute(text("SELECT action FROM moderation_log"))
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# Repository contract
# ---------------------------------------------------------------------------


async def test_a_failure_after_the_flush_keeps_no_warning(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corruption case: warned in fact, "failed" on screen.

    ``_record_action_internal`` stands in for anything that can raise
    between the INSERT and the end of the call — in production the
    likelier candidate is the COUNT, which is a plain SELECT and so
    leaves the transaction perfectly committable.
    """

    async def boom(*_a: Any, **_kw: Any) -> None:
        msg = "audit row could not be queued"
        raise RuntimeError(msg)

    repo = ModerationRepo(session)
    monkeypatch.setattr(repo, "_record_action_internal", boom)

    with pytest.raises(RuntimeError):
        await repo.add_warning(user_id=_TARGET, chat_id=_CHAT, admin_id=_ADMIN, reason="спам")

    # The handler swallows and returns; the middleware then commits.
    await session.commit()
    assert await _warnings(session) == []
    assert await _modlog(session) == []


async def test_a_failed_write_leaves_the_session_committable(
    session: AsyncSession,
) -> None:
    """The crash case: the flush itself fails.

    Dropping the table is the bluntest way to make ``flush`` raise, and
    it is not hypothetical here — ``moderation.db`` is the file whose
    schema drifted on prod. Without the SAVEPOINT the session is
    deactivated and the middleware's commit raises
    ``PendingRollbackError``.
    """
    await session.execute(text("DROP TABLE warnings"))
    repo = ModerationRepo(session)

    with pytest.raises(Exception, match="warnings"):
        await repo.add_warning(user_id=_TARGET, chat_id=_CHAT, admin_id=_ADMIN, reason="спам")

    # No assertion on the row — the table is gone. The point is that the
    # next thing the middleware does must not blow up.
    await session.commit()


async def test_the_happy_path_still_writes_both_rows(session: AsyncSession) -> None:
    """Control: the SAVEPOINT must not swallow the write it wraps."""
    repo = ModerationRepo(session)
    warning_id, count = await repo.add_warning(
        user_id=_TARGET, chat_id=_CHAT, admin_id=_ADMIN, reason="спам"
    )
    await session.commit()

    assert warning_id > 0
    assert count == 1
    assert await _warnings(session) == [_TARGET]
    assert await _modlog(session) == ["warn"]


# ---------------------------------------------------------------------------
# The handler that swallows
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=_CHAT, type="supergroup")
        self.from_user = SimpleNamespace(id=_ADMIN, language_code="ru", is_bot=False)
        self.text = "/warn спам"
        self.caption = None
        self.reply_to_message = None
        self.sender_chat = None
        self.entities = None
        self.caption_entities = None
        self.outgoing: list[str] = []

    async def reply(self, text_: str, **_kw: object) -> None:
        self.outgoing.append(text_)

    async def answer(self, text_: str, **_kw: object) -> None:
        self.outgoing.append(text_)


class _ConfigRepoStub:
    async def get_or_default(self, _group_id: int) -> Any:  # noqa: ANN401
        return SimpleNamespace(max_warns=3, autoban_enabled=False)


@pytest.fixture
def _waved_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the permission/target gates — none of them touch the write."""

    async def lang(*_a: Any, **_kw: Any) -> str:
        return "ru"

    async def allowed(*_a: Any, **_kw: Any) -> Any:  # noqa: ANN401
        return SimpleNamespace(rank_level=0)

    async def ok(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(moderation_mod, "_resolve_lang", lang)
    monkeypatch.setattr(moderation_mod, "_require_moderation", allowed)
    monkeypatch.setattr(moderation_mod, "_check_target_ok", ok)
    monkeypatch.setattr(moderation_mod, "_check_rank_target_ok", ok)
    monkeypatch.setattr(
        moderation_mod,
        "_target_from_reply",
        lambda _m: (_TARGET, "Нарушитель", False),
    )


@pytest.mark.usefixtures("_waved_through")
async def test_the_swallowed_warn_commits_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the handler, since that is where the swallow is.

    Asserting only "the admin saw the failure card" would pass on the
    old code too — the card was always sent. What was wrong is what the
    database held afterwards.
    """
    repo = ModerationRepo(session)

    async def boom(*_a: Any, **_kw: Any) -> None:
        msg = "audit row could not be queued"
        raise RuntimeError(msg)

    monkeypatch.setattr(repo, "_record_action_internal", boom)
    message = _FakeMessage()

    await moderation_mod.handle_warn(
        message,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repo,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        _ConfigRepoStub(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )

    # Pin WHICH card: a test that only counted messages would also pass
    # on a run that never reached the write at all.
    assert message.outgoing == [t("h_mod_warn_fail", "ru")]
    await session.commit()  # what BaseSessionMiddleware does next
    assert await _warnings(session) == []
