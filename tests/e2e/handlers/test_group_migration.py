"""End-to-end #110 — the supergroup upgrade is caught and repairable.

The service-level contract lives in
``tests/regression/test_group_migration.py``. What this file proves is
the part that regression test cannot: that the router is actually wired
into the live dispatcher, that both halves of Telegram's announcement
route, and that ``/admin_group_migrate`` is reachable, dev-gated and
honest about what it did.

Pins:

* Old-side announcement (``migrate_to_chat_id``, delivered in the group
  that is going away) moves the data.
* New-side announcement (``migrate_from_chat_id``, delivered in the new
  supergroup) does the same — the bot may only ever see this one.
* An ordinary group message is not mistaken for either.
* ``/admin_group_migrate``: silent for non-developers, usage card on bad
  arguments, refusal on a positive id, success card that reports the
  real row counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.admin.group_migrate import _parse
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram.types import Update

    from telegram_invite_bot.db.engines import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

pytestmark = pytest.mark.e2e


_OLD = -5_254_625_211
_NEW = -1_003_730_552_653
_DEV = 42

_GROUP_SETTINGS = UsersBase.metadata.tables["group_settings"]


def _service_update(
    *,
    chat_id: int,
    migrate_to: int | None = None,
    migrate_from: int | None = None,
    chat_type: str = "group",
    update_id: int = 7,
) -> Update:
    """A migration service message, shaped the way Telegram sends one.

    Deliberately built here rather than in the shared ``make_wired``
    conftest: a service message carries no ``text``, which every other
    helper in that file assumes.
    """
    from aiogram.types import Update as _Update

    message: dict[str, Any] = {
        "message_id": 1,
        "date": 1_700_000_000,
        "chat": {"id": chat_id, "type": chat_type, "title": "Тесты ком17"},
        "from": {"id": 5, "is_bot": False, "first_name": "U"},
    }
    if migrate_to is not None:
        message["migrate_to_chat_id"] = migrate_to
    if migrate_from is not None:
        message["migrate_from_chat_id"] = migrate_from
    return _Update.model_validate({"update_id": update_id, "message": message})


async def _seed(registry: EngineRegistry, group_id: int) -> None:
    async with session_for(registry, DBName.USERS) as session:
        await session.execute(
            sa.insert(_GROUP_SETTINGS).values(group_id=group_id, rules="кто первый встал")
        )


async def _group_ids(registry: EngineRegistry) -> list[int]:
    async with session_for(registry, DBName.USERS) as session:
        result = await session.execute(
            sa.select(_GROUP_SETTINGS.c.group_id).order_by(_GROUP_SETTINGS.c.group_id)
        )
        return [r[0] for r in result.all()]


# -------------------------------------------------- the service messages


async def test_old_side_announcement_moves_the_data(make_wired: WiredFactory) -> None:
    """``migrate_to_chat_id`` arrives in the group being retired, so the
    OLD id is ``chat.id`` and the new one is in the field."""
    _bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(registry, _OLD)

    await dispatcher.feed_update(_bot, _service_update(chat_id=_OLD, migrate_to=_NEW))

    assert await _group_ids(registry) == [_NEW]


async def test_new_side_announcement_moves_the_data(make_wired: WiredFactory) -> None:
    """``migrate_from_chat_id`` arrives in the new supergroup, so the
    roles are reversed. A bot added after the upgrade — or one whose
    webhook was down for that minute — sees only this half.
    """
    _bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(registry, _OLD)

    await dispatcher.feed_update(
        _bot, _service_update(chat_id=_NEW, migrate_from=_OLD, chat_type="supergroup")
    )

    assert await _group_ids(registry) == [_NEW]


async def test_an_ordinary_group_message_is_not_a_migration(
    make_wired: WiredFactory,
) -> None:
    """Guard-the-guard: the filter has to be doing the selecting, not
    the handler catching everything and finding nothing to do."""
    _bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(registry, _OLD)

    await dispatcher.feed_update(
        _bot, make_message_update("привет", chat_id=_OLD, chat_type="group", user_id=5)
    )

    assert await _group_ids(registry) == [_OLD]


# ------------------------------------------------- /admin_group_migrate


async def test_admin_command_is_silent_for_non_developers(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    await _seed(registry, _OLD)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/admin_group_migrate {_OLD} {_NEW}", user_id=_DEV, chat_type="private"
        ),
    )

    assert sent == []
    assert await _group_ids(registry) == [_OLD]


async def test_admin_command_repairs_a_stranded_group(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The backlog case: a group that upgraded before the bot could
    notice has no second announcement coming, so a developer runs the
    remap by hand."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed(registry, _OLD)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/admin_group_migrate {_OLD} {_NEW}", user_id=_DEV, chat_type="private"
        ),
    )

    assert await _group_ids(registry) == [_NEW]
    text = sent[0]["text"]
    assert str(_OLD) in text
    assert str(_NEW) in text
    # One column value actually moved — a card that says "0" after a
    # successful remap would be worse than no card.
    assert "Перенесено строк: <code>1</code>" in text


async def test_admin_command_keeps_its_arguments_under_a_photo_caption(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#105 again: a command typed as a photo caption still routes, and
    a handler reading ``message.text`` would parse zero arguments and
    show the usage card instead of doing the work."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed(registry, _OLD)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/admin_group_migrate {_OLD} {_NEW}",
            user_id=_DEV,
            chat_type="private",
            as_caption=True,
        ),
    )

    assert "Использование" not in sent[0]["text"]
    assert await _group_ids(registry) == [_NEW]


@pytest.mark.parametrize("body", ["", f"{_OLD}", f"{_OLD} {_NEW} extra", "abc def"])
async def test_admin_command_shows_usage_on_bad_arguments(
    make_wired: WiredFactory, capture_outgoing: Any, body: str
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed(registry, _OLD)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/admin_group_migrate {body}".strip(), user_id=_DEV, chat_type="private"
        ),
    )

    assert "Использование" in sent[0]["text"]
    assert await _group_ids(registry) == [_OLD]


async def test_admin_command_refuses_a_positive_id(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """A user id where a group id belongs is the mistake that would do
    the most damage, and the one a hand-typed command is most likely to
    make. The refusal has to reach the operator, not just the log."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    await _seed(registry, _OLD)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            f"/admin_group_migrate {_OLD} 12345", user_id=_DEV, chat_type="private"
        ),
    )

    assert "must be negative" in sent[0]["text"]
    assert await _group_ids(registry) == [_OLD]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("-1 -2", (-1, -2)),
        ("  -1   -2  ", (-1, -2)),
        ("-1", None),
        ("-1 -2 -3", None),
        ("-1 x", None),
        ("-1 ٢", None),  # Arabic-Indic digit — .isdigit() true, int() no
        ("-1 --2", None),
    ],
)
def test_parse_accepts_exactly_two_integers(body: str, expected: tuple[int, int] | None) -> None:
    assert _parse(body) == expected
