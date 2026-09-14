"""#110 — a supergroup upgrade must carry the group's data to the new id.

Telegram changes a group's ``chat_id`` when it is upgraded to a
supergroup. Every table in this codebase keys group state by that
number, so without a remap the group's settings, warnings, filters,
staff roles, donations and rating are stranded under an id no update
will ever carry again — and, worse, stay *live* in every list built
from a table scan.

What this file pins:

* **Coverage.** The discovery query must find every group-naming
  column, including the qualified spellings
  (``transcription_log_chat_id``, ``current_group_id``) that a
  ``name in ("group_id", "chat_id")`` check would miss. Asserted
  against a hand-written list taken from the production schema, not
  re-derived from the implementation's own pattern — a test that
  computes its expectation the way the code does passes for every
  possible pattern.
* **The data actually moves**, across databases and across both
  spellings, including two group-naming columns in the same table.
* **Nothing else moves**: a bystander group and non-group columns
  holding the same numbers are left exactly as they were.
* **Idempotence.** Telegram announces the upgrade twice, so the second
  run must be a no-op rather than a second, different outcome.
* **Collision.** When the new id already holds an equivalent row the
  new one survives and the stale one is dropped — never a crash, never
  a duplicate. For *configuration* that is the whole story. For an
  accumulator (``group_xp``, ``total_donated``, a day's message
  ``count``) or for paid time (``vip_till``, ``expires_at``) it is not:
  the loser's value is folded into the winner before the drop, summed
  or kept-at-maximum, so an upgrade cannot cost a group its rating
  score or a member the VIP days they bought.
* **The id guard.** Non-negative or equal ids are refused, because the
  column match is deliberately wide and a positive id would rewrite
  rows keyed by a *user*.
* **Per-database isolation.** One unusable database must not cost the
  other four their remap; it must be named in the result instead.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import (
    ActivityBase,
    EconomyBase,
    MessageStatsBase,
    ModerationBase,
    UsersBase,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.services import group_migration_service as gms
from telegram_invite_bot.services.group_migration_service import (
    _DISCOVER_COLUMNS,
    migrate_group_id,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from telegram_invite_bot.db.engines import EngineRegistry

pytestmark = pytest.mark.integration


_OLD = -5_254_625_211
"""The real pre-upgrade id of the "Тесты ком17" test group."""

_NEW = -1_003_730_552_653
"""…and the supergroup id Telegram gave it."""

_BYSTANDER = -100_999_888_777
"""An unrelated group that must come out of the remap untouched."""

_MEMBER = 77
"""A member of the migrating group — the ``users`` row every seeded
user-keyed row hangs off, since ``user_settings.user_id`` is the one
foreign key in the whole schema and SQLite enforces it here."""

_EARLY = dt.datetime(2021, 3, 4)  # noqa: DTZ001 — naive, as the column is
"""A tenure date old enough that new-wins losing it is visible."""

_LATE = dt.datetime(2026, 9, 1)  # noqa: DTZ001 — naive, as the column is
"""…and one from the days the bot spent writing under the new id."""

_SEEN = dt.datetime(2026, 9, 5)  # noqa: DTZ001 — naive, as the column is
"""The newer id's ``last_seen``. Unlike the join date, this one is
supposed to end up on the survivor."""


_BASES = {
    DBName.USERS: UsersBase,
    DBName.ECONOMY: EconomyBase,
    DBName.ACTIVITY: ActivityBase,
    DBName.MODERATION: ModerationBase,
    DBName.MESSAGE_STATS: MessageStatsBase,
}


# Written out by hand from the deployed schema (``sqlite_master`` on the
# production databases), NOT generated from the service's own pattern.
# That independence is the point: this list is the contract, and the
# regex is one implementation of it. Only the qualified spellings and a
# couple of plain ones per database are listed — enough to catch a
# narrowed pattern, short enough to stay readable.
_MUST_DISCOVER: dict[DBName, frozenset[tuple[str, str]]] = {
    DBName.USERS: frozenset(
        {
            ("group_settings", "group_id"),
            # Qualified spelling #1 — dropped by a bare-name check.
            ("group_settings", "transcription_log_chat_id"),
            # Qualified spelling #2, and on a table whose *other*
            # columns are user-keyed.
            ("user_settings", "current_group_id"),
            ("marriages", "chat_id"),
            ("user_group_nicknames", "chat_id"),
            ("voice_transcriptions", "group_id"),
        }
    ),
    DBName.ECONOMY: frozenset(
        {
            ("inventory", "group_id"),
            ("pvp_offers", "chat_id"),
            ("user_group_vip", "group_id"),
        }
    ),
    DBName.MODERATION: frozenset(
        {
            ("warnings", "chat_id"),
            ("welcome_config", "group_id"),
            ("word_filters", "group_id"),
        }
    ),
    DBName.MESSAGE_STATS: frozenset({("message_counts", "chat_id")}),
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("1:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    """All five databases, each with its real ORM schema."""
    reg = build_registry(_settings(tmp_path))
    for db, base in _BASES.items():
        async with reg.engine(db).begin() as conn:
            await conn.run_sync(base.metadata.create_all)
    users = UsersBase.metadata.tables["users"]
    async with reg.engine(DBName.USERS).begin() as conn:
        await conn.execute(sa.insert(users).values(**_row(users, user_id=_MEMBER)))
    try:
        yield reg
    finally:
        await reg.dispose()


def _sample(column: sa.Column[Any]) -> Any:
    """A schema-valid filler value for a NOT NULL column we don't care about.

    The seeded rows exist to be *found*, not to be meaningful, so every
    column outside the one under test gets whatever its type accepts.
    """
    try:
        python_type = column.type.python_type
    except NotImplementedError:  # pragma: no cover — no such column today
        return None
    if python_type is bool:
        return False
    if python_type in (int, float):
        return 1
    if python_type is dt.datetime:
        return dt.datetime(2026, 1, 1)  # noqa: DTZ001 — filler for a column nothing reads
    if python_type is dt.date:
        return dt.date(2026, 1, 1)
    return "x"


def _row(table: sa.Table, **values: Any) -> dict[str, Any]:
    """A minimal insertable row: the given values plus required filler."""
    row = dict(values)
    for column in table.columns:
        if column.name in row:
            continue
        if column.nullable or column.default is not None:
            continue
        if column.server_default is not None:
            continue
        if column.primary_key and column.autoincrement is not False:
            continue
        row[column.name] = _sample(column)
    return row


def _table(db: DBName, name: str) -> sa.Table:
    return _BASES[db].metadata.tables[name]


async def _insert(registry: EngineRegistry, db: DBName, name: str, **values: Any) -> None:
    table = _table(db, name)
    async with session_for(registry, db) as session:
        await session.execute(sa.insert(table).values(**_row(table, **values)))


async def _fetch(
    registry: EngineRegistry, db: DBName, name: str, *columns: str
) -> list[tuple[Any, ...]]:
    table = _table(db, name)
    async with session_for(registry, db) as session:
        result = await session.execute(
            sa.select(*(table.c[c] for c in columns)).order_by(*(table.c[c] for c in columns))
        )
        return [tuple(r) for r in result.all()]


# ---------------------------------------------------------------- coverage


@pytest.mark.parametrize("db", sorted(_MUST_DISCOVER, key=lambda d: d.value))
async def test_discovery_finds_every_group_naming_column(
    registry: EngineRegistry, db: DBName
) -> None:
    """The discovery query must return the hand-written contract in full.

    The failure this guards against is a narrowed pattern — someone
    "simplifying" the LIKE clauses down to the two bare names and
    silently orphaning ``transcription_log_chat_id`` and
    ``current_group_id``. Those two columns are the whole reason the
    pattern is shaped the way it is.
    """
    async with session_for(registry, db) as session:
        found = {tuple(r) for r in (await session.execute(_DISCOVER_COLUMNS)).all()}
    missing = sorted(_MUST_DISCOVER[db] - found)
    assert not missing, f"{db.value}: discovery missed {missing}"


async def test_discovery_ignores_columns_that_do_not_name_a_group(
    registry: EngineRegistry,
) -> None:
    """Guard-the-guard: the pattern is wide, not unbounded.

    ``users.user_id`` and ``group_settings.language`` are exactly the
    shape of thing a pattern gone sloppy (``LIKE '%id%'``, or dropping
    the anchor) would start rewriting.
    """
    async with session_for(registry, DBName.USERS) as session:
        found = {tuple(r) for r in (await session.execute(_DISCOVER_COLUMNS)).all()}
    assert ("users", "user_id") not in found
    assert ("group_settings", "rules") not in found
    assert all(not t.startswith("sqlite_") for t, _ in found)


# ------------------------------------------------------------- the remap


async def _seed_full(registry: EngineRegistry, group_id: int) -> None:
    """One row per representative table, all keyed to ``group_id``."""
    await _insert(
        registry,
        DBName.USERS,
        "group_settings",
        group_id=group_id,
        transcription_log_chat_id=group_id,
    )
    await _insert(
        registry, DBName.USERS, "user_settings", user_id=_MEMBER, current_group_id=group_id
    )
    await _insert(registry, DBName.USERS, "marriages", chat_id=group_id)
    await _insert(registry, DBName.ECONOMY, "inventory", group_id=group_id, user_id=_MEMBER)
    await _insert(registry, DBName.MODERATION, "warnings", chat_id=group_id, user_id=_MEMBER)
    await _insert(
        registry, DBName.MESSAGE_STATS, "message_counts", chat_id=group_id, user_id=_MEMBER
    )


async def test_every_seeded_row_lands_on_the_new_id(registry: EngineRegistry) -> None:
    await _seed_full(registry, _OLD)

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert result.failed == ()
    assert await _fetch(registry, DBName.USERS, "group_settings", "group_id") == [(_NEW,)]
    # Both group-naming columns of the same table move — the remap is
    # per column, not per table.
    assert await _fetch(registry, DBName.USERS, "group_settings", "transcription_log_chat_id") == [
        (_NEW,)
    ]
    assert await _fetch(registry, DBName.USERS, "user_settings", "current_group_id") == [(_NEW,)]
    assert await _fetch(registry, DBName.USERS, "marriages", "chat_id") == [(_NEW,)]
    assert await _fetch(registry, DBName.ECONOMY, "inventory", "group_id") == [(_NEW,)]
    assert await _fetch(registry, DBName.MODERATION, "warnings", "chat_id") == [(_NEW,)]
    assert await _fetch(registry, DBName.MESSAGE_STATS, "message_counts", "chat_id") == [(_NEW,)]
    # Seven column values across four databases.
    assert result.moved == 7
    assert result.dropped == 0


async def test_a_bystander_group_is_untouched(registry: EngineRegistry) -> None:
    """The WHERE clause has to do real work — prove it does.

    Without it every group on the instance would be collapsed onto the
    new id, which is the worst outcome this module could produce and
    the one a passing "the row moved" assertion would not notice.
    """
    await _seed_full(registry, _OLD)
    await _insert(registry, DBName.MODERATION, "warnings", chat_id=_BYSTANDER, user_id=_MEMBER)

    await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert set(await _fetch(registry, DBName.MODERATION, "warnings", "chat_id")) == {
        (_BYSTANDER,),
        (_NEW,),
    }


async def test_a_user_keyed_column_holding_the_same_number_is_untouched(
    registry: EngineRegistry,
) -> None:
    """Only group-naming columns are rewritten, never every column that
    happens to hold the id."""
    await _insert(
        registry, DBName.MODERATION, "warnings", chat_id=_OLD, user_id=_OLD, admin_id=_OLD
    )

    await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.MODERATION, "warnings", "chat_id") == [(_NEW,)]
    assert await _fetch(registry, DBName.MODERATION, "warnings", "user_id") == [(_OLD,)]


async def test_running_twice_changes_nothing_the_second_time(
    registry: EngineRegistry,
) -> None:
    """Telegram announces the upgrade in both chats; both land here."""
    await _seed_full(registry, _OLD)

    first = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)
    before = await _fetch(registry, DBName.USERS, "group_settings", "group_id")
    second = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert first.moved > 0
    assert second.moved == 0
    assert second.dropped == 0
    assert second.failed == ()
    # Column discovery is unconditional, so the visit count is stable
    # even when there is nothing left to move.
    assert second.columns == first.columns
    assert await _fetch(registry, DBName.USERS, "group_settings", "group_id") == before


async def test_on_collision_the_new_id_wins(registry: EngineRegistry) -> None:
    """Both ids can already hold a row — the bot may have written to the
    supergroup before the announcement was processed. The row that
    matches the group as it is *now* has to be the survivor."""
    await _insert(registry, DBName.USERS, "group_settings", group_id=_OLD, rules="stale")
    await _insert(registry, DBName.USERS, "group_settings", group_id=_NEW, rules="current")

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    # One row survives, and it is the one that was already on the new id.
    assert await _fetch(registry, DBName.USERS, "group_settings", "group_id", "rules") == [
        (_NEW, "current")
    ]
    assert result.dropped == 1


# ------------------------------------------------------- merged collisions
#
# #1086. The tests above cover configuration, where "the new id wins"
# is the right answer. These cover the tables where it is not: a
# running total or an expiry on the losing row is value that was really
# earned or really paid for, and dropping the row would destroy it
# silently. Every table named here is in ``gms._MERGE_RULES``.


async def test_group_xp_is_summed_when_both_ids_hold_a_rating_row(
    registry: EngineRegistry,
) -> None:
    """The donation rating ranks on ``group_xp`` (``total_donations`` is
    frozen at zero by design), so this is the column an upgrade would
    have quietly zeroed. ``groups_donations`` keys on ``group_id``
    alone, which makes the collision unconditional: both rows exist,
    therefore both collide."""
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_OLD, group_xp=645)
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_NEW, group_xp=100)

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.ECONOMY, "groups_donations", "group_id", "group_xp") == [
        (_NEW, 745)
    ]
    assert result.dropped == 1
    assert result.merged == 1


async def test_a_rating_row_with_no_counterpart_is_moved_not_doubled(
    registry: EngineRegistry,
) -> None:
    """The fold must not touch a row that is not colliding. If it ran
    unconditionally the score would be added to itself on every remap,
    and the remap runs twice for real — Telegram announces the upgrade
    in both chats."""
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_OLD, group_xp=645)

    first = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)
    await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.ECONOMY, "groups_donations", "group_id", "group_xp") == [
        (_NEW, 645)
    ]
    assert first.merged == 0


async def test_per_member_donation_totals_are_summed_within_the_member(
    registry: EngineRegistry,
) -> None:
    """``group_top_donators`` keys on (group_id, user_id), so the fold
    has to match on the member too — summing across members would move
    one donor's total onto another's name."""
    other = _MEMBER + 1
    for gid, mine, theirs in ((_OLD, 300, 40), (_NEW, 25, 7)):
        await _insert(
            registry,
            DBName.ECONOMY,
            "group_top_donators",
            group_id=gid,
            user_id=_MEMBER,
            total_donated=mine,
        )
        await _insert(
            registry,
            DBName.ECONOMY,
            "group_top_donators",
            group_id=gid,
            user_id=other,
            total_donated=theirs,
        )

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(
        registry, DBName.ECONOMY, "group_top_donators", "user_id", "total_donated"
    ) == [(_MEMBER, 325), (other, 47)]
    assert result.merged == 2


async def test_daily_message_counts_are_summed_within_the_day(
    registry: EngineRegistry,
) -> None:
    """``message_counts`` keys on (user_id, chat_id, date). Two rows for
    the same member on the same day are the same day's activity seen
    under two ids; two different days are not."""
    for gid, count in ((_OLD, 12), (_NEW, 5)):
        await _insert(
            registry,
            DBName.MESSAGE_STATS,
            "message_counts",
            chat_id=gid,
            user_id=_MEMBER,
            date="2026-08-29",
            count=count,
        )
    await _insert(
        registry,
        DBName.MESSAGE_STATS,
        "message_counts",
        chat_id=_OLD,
        user_id=_MEMBER,
        date="2026-08-28",
        count=9,
    )

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.MESSAGE_STATS, "message_counts", "date", "count") == [
        ("2026-08-28", 9),
        ("2026-08-29", 17),
    ]
    assert result.merged == 1
    assert result.dropped == 1


@pytest.mark.parametrize(("old_till", "new_till"), [(9_000.0, 100.0), (100.0, 9_000.0)])
async def test_vip_keeps_the_later_expiry_whichever_id_holds_it(
    registry: EngineRegistry, old_till: float, new_till: float
) -> None:
    """Paid time. Summing two expiry timestamps would be nonsense, and
    letting the new id win outright can SHORTEN a privilege someone
    bought — so the fold keeps the maximum, from either side."""
    for gid, till in ((_OLD, old_till), (_NEW, new_till)):
        await _insert(
            registry,
            DBName.ECONOMY,
            "user_group_vip",
            group_id=gid,
            user_id=_MEMBER,
            vip_till=till,
        )

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.ECONOMY, "user_group_vip", "group_id", "vip_till") == [
        (_NEW, 9_000.0)
    ]
    assert result.merged == 1


@pytest.mark.parametrize(("old_joined", "new_joined"), [(_EARLY, _LATE), (_LATE, _EARLY)])
async def test_a_members_join_date_is_the_earliest_of_the_two_ids(
    registry: EngineRegistry, old_joined: dt.datetime, new_joined: dt.datetime
) -> None:
    """#2019: the one column in this schema where new-wins runs backwards.

    ``user_group_joins`` is PK ``(user_id, chat_id)``, so an upgrade
    collides on it whenever the member was seen under BOTH ids — which
    is what happens when the upgrade lands while the bot has no handler
    for it and the activity middleware creates fresh rows in the
    supergroup for days before an admin runs the remap.

    ``joined_at`` is the one value here that must move *backwards*:
    ``UserGroupJoins`` exists to hold the FIRST time we saw a member
    (``repositories/user_group_joins_repo.record_join`` writes the
    invariant down — "a rejoin must not push that date forward and
    shrink every since-join counter derived from it"), and the profile
    card renders it as "in the group since ...". New-wins turned a
    member of five years into someone who joined this morning, on the
    very path that exists to keep the group's history. ``last_seen``
    still goes the other way, which is why both directions are pinned
    on one row.
    """
    for gid, joined, seen in (
        (_OLD, old_joined, dt.datetime(2026, 8, 1)),  # noqa: DTZ001 — naive, as the column is
        (_NEW, new_joined, _SEEN),  # noqa: DTZ001 — naive, as the column is
    ):
        await _insert(
            registry,
            DBName.USERS,
            "user_group_joins",
            chat_id=gid,
            user_id=_MEMBER,
            joined_at=joined,
            last_seen=seen,
        )

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(
        registry, DBName.USERS, "user_group_joins", "chat_id", "joined_at", "last_seen"
    ) == [(_NEW, _EARLY, _SEEN)]
    assert result.merged == 1
    assert result.dropped == 1


async def test_a_member_seen_under_only_one_id_keeps_their_own_dates(
    registry: EngineRegistry,
) -> None:
    """No counterpart, no fold — the row just moves, dates intact."""
    await _insert(
        registry,
        DBName.USERS,
        "user_group_joins",
        chat_id=_OLD,
        user_id=_MEMBER,
        joined_at=_EARLY,
        last_seen=None,
    )

    await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(
        registry, DBName.USERS, "user_group_joins", "chat_id", "joined_at", "last_seen"
    ) == [(_NEW, _EARLY, None)]


async def test_two_members_with_no_last_seen_do_not_gain_a_zero_timestamp(
    registry: EngineRegistry,
) -> None:
    """The fold must not invent a value for a column both sides left NULL.

    ``last_seen`` is a TIMESTAMP, and the ``MAX(COALESCE(x, 0), ...)``
    shape the paid-time rules use would write the integer ``0`` into it
    when neither side has a date — a value the ORM then cannot load back
    as a datetime. The two ``maxes`` columns that predate this rule are
    both ``REAL NOT NULL``, so they never reached that branch and the
    hazard stayed invisible until a nullable TIMESTAMP joined them.
    """
    for gid in (_OLD, _NEW):
        await _insert(
            registry,
            DBName.USERS,
            "user_group_joins",
            chat_id=gid,
            user_id=_MEMBER,
            joined_at=_EARLY,
            last_seen=None,
        )

    await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.USERS, "user_group_joins", "last_seen") == [(None,)]


async def test_a_privilege_keeps_the_later_expiry_per_privilege_type(
    registry: EngineRegistry,
) -> None:
    """``user_privileges`` keys on (user_id, privilege_type, group_id):
    a longer ``vip`` must not extend an unrelated ``custom_title``."""
    for gid, vip, title in ((_OLD, 9_000.0, 10.0), (_NEW, 100.0, 500.0)):
        await _insert(
            registry,
            DBName.ECONOMY,
            "user_privileges",
            group_id=gid,
            user_id=_MEMBER,
            privilege_type="vip",
            expires_at=vip,
        )
        await _insert(
            registry,
            DBName.ECONOMY,
            "user_privileges",
            group_id=gid,
            user_id=_MEMBER,
            privilege_type="custom_title",
            expires_at=title,
        )

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(
        registry, DBName.ECONOMY, "user_privileges", "privilege_type", "expires_at"
    ) == [("custom_title", 500.0), ("vip", 9_000.0)]
    assert result.merged == 2


async def test_a_bystander_groups_totals_are_never_folded(
    registry: EngineRegistry,
) -> None:
    """The fold is two correlated subqueries against the same table. A
    missing ``group_id`` term in either one would pull an unrelated
    group's score into the winner."""
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_OLD, group_xp=10)
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_NEW, group_xp=20)
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_BYSTANDER, group_xp=999)

    await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    # Ordered by group_id, and _NEW is the more negative of the two.
    assert await _fetch(registry, DBName.ECONOMY, "groups_donations", "group_id", "group_xp") == [
        (_NEW, 30),
        (_BYSTANDER, 999),
    ]


def test_config_tables_are_still_decided_by_new_wins() -> None:
    """Guard-the-guard for :data:`gms._MERGE_RULES`: it is an allowlist,
    and a table that drifts into it by accident would start doing
    arithmetic on columns where the newest value is the correct one."""
    assert "group_settings" not in gms._MERGE_RULES
    assert set(gms._MERGE_RULES) == {
        "groups_donations",
        "group_top_donators",
        "message_counts",
        "user_group_joins",
        "user_group_vip",
        "user_privileges",
    }


async def test_a_merge_rule_naming_an_absent_column_degrades_to_new_wins(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One table name can carry two schemas — ``message_counts`` exists
    in both activity and message_stats. A rule that does not fit must
    fall back to the pre-existing behaviour rather than raise, because
    raising would cost that whole database its remap."""
    monkeypatch.setitem(
        gms._MERGE_RULES,
        "groups_donations",
        gms._MergeRule(group_col="group_id", sums=("no_such_column",)),
    )
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_OLD, group_xp=645)
    await _insert(registry, DBName.ECONOMY, "groups_donations", group_id=_NEW, group_xp=100)

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert await _fetch(registry, DBName.ECONOMY, "groups_donations", "group_id", "group_xp") == [
        (_NEW, 100)
    ]
    assert result.merged == 0
    assert result.dropped == 1
    assert result.failed == ()


# --------------------------------------------------------------- guards


@pytest.mark.parametrize(
    ("old_id", "new_id"),
    [
        (5_254_625_211, _NEW),  # a user id passed as the source
        (_OLD, 1_003_730_552_653),  # …or as the destination
        (0, _NEW),
        (_OLD, 0),
    ],
)
async def test_non_negative_ids_are_refused(
    registry: EngineRegistry, old_id: int, new_id: int
) -> None:
    """A positive id is a user id, and the column match is wide enough
    that accepting one would rewrite that user's rows across every
    database. Refuse loudly instead."""
    await _seed_full(registry, _OLD)
    with pytest.raises(ValueError, match="must be negative"):
        await migrate_group_id(registry, old_id=old_id, new_id=new_id)
    assert await _fetch(registry, DBName.USERS, "group_settings", "group_id") == [(_OLD,)]


async def test_identical_ids_are_refused(registry: EngineRegistry) -> None:
    with pytest.raises(ValueError, match="nothing to migrate"):
        await migrate_group_id(registry, old_id=_OLD, new_id=_OLD)


async def test_a_valid_pair_is_not_refused(registry: EngineRegistry) -> None:
    """Guard-the-guard: the id check must pass for the right reason, or
    the four refusals above would also pass with ``raise ValueError``
    unconditionally at the top of the function."""
    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)
    assert result.columns > 0
    assert result.failed == ()


# ------------------------------------------------------ failure isolation


async def test_one_broken_database_does_not_cost_the_others(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five files, five transactions — cross-file atomicity is not on
    offer. Aborting on the first failure would only make the partial
    remap smaller, so the contract is: do the rest, name the casualty,
    and let a rerun finish the job."""
    real_session_for = gms.session_for

    def _broken(reg: EngineRegistry, db: DBName) -> Any:
        if db is DBName.ECONOMY:
            msg = "database is locked"
            raise sa.exc.OperationalError(msg, None, Exception(msg))
        return real_session_for(reg, db)

    await _seed_full(registry, _OLD)
    monkeypatch.setattr(gms, "session_for", _broken)

    result = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)

    assert result.failed == (DBName.ECONOMY.value,)
    # economy.db kept its stale row…
    assert await _fetch(registry, DBName.ECONOMY, "inventory", "group_id") == [(_OLD,)]
    # …and every other database still migrated.
    assert await _fetch(registry, DBName.USERS, "group_settings", "group_id") == [(_NEW,)]
    assert await _fetch(registry, DBName.MODERATION, "warnings", "chat_id") == [(_NEW,)]

    # A rerun with the database working again completes the remap.
    monkeypatch.setattr(gms, "session_for", real_session_for)
    retry = await migrate_group_id(registry, old_id=_OLD, new_id=_NEW)
    assert retry.failed == ()
    assert await _fetch(registry, DBName.ECONOMY, "inventory", "group_id") == [(_NEW,)]
