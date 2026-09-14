"""Alembic environment — multi-DB dispatcher driven by ``-x db=<name>``.

Why not the canonical "multidb" template?
    The template loops over all DBs in one invocation, which breaks
    autogenerate (a model only exists in one ``MetaData``) and forces a
    shared ``script_location``. Our pattern keeps one ``versions/<db>/``
    tree per DB and runs migrations one DB at a time — simpler to reason
    about during the strangler migration when only some DBs have models.

Usage::

    alembic -x db=users upgrade head
    alembic -x db=economy revision --autogenerate -m "add foo"
    alembic -x db=users stamp head    # baseline an existing prod schema
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from telegram_invite_bot.config.settings import get_settings
from telegram_invite_bot.db.engines import build_url, resolve_db_path
from telegram_invite_bot.db.models.base import (
    ActivityBase,
    EconomyBase,
    MessageStatsBase,
    ModerationBase,
    UsersBase,
)
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from sqlalchemy import Connection
    from sqlalchemy.sql.schema import MetaData

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


_METADATA: dict[DBName, MetaData] = {
    DBName.USERS: UsersBase.metadata,
    DBName.ECONOMY: EconomyBase.metadata,
    DBName.ACTIVITY: ActivityBase.metadata,
    DBName.MODERATION: ModerationBase.metadata,
    DBName.MESSAGE_STATS: MessageStatsBase.metadata,
}


def _selected_db() -> DBName:
    raw = context.get_x_argument(as_dictionary=True).get("db")
    if not raw:
        raise RuntimeError(
            f"alembic invocation missing '-x db=<name>'. Known: {[d.value for d in DBName]}"
        )
    try:
        return DBName(raw)
    except ValueError as exc:
        raise RuntimeError(f"unknown db '{raw}'") from exc


def _configure(db: DBName) -> tuple[str, MetaData, Path]:
    settings = get_settings()
    path = resolve_db_path(settings.paths, db)
    versions_dir = Path(__file__).resolve().parent / "versions" / db.value
    versions_dir.mkdir(parents=True, exist_ok=True)
    config.set_main_option("version_locations", str(versions_dir))
    config.set_main_option("sqlalchemy.url", build_url(path))
    return build_url(path), _METADATA[db], path


def run_migrations_offline() -> None:
    db = _selected_db()
    url, metadata, _ = _configure(db)
    context.configure(
        url=url,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection, metadata: MetaData) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    db = _selected_db()
    _, metadata, _ = _configure(db)
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations, metadata)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
