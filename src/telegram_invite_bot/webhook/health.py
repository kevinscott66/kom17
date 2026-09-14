"""Liveness/readiness probes — one schema probe per database.

``/readyz`` returns 200 only when every engine answers *and* the file it
opened actually holds a schema. The second half is the whole point: an
earlier revision ran a bare ``SELECT 1``, which SQLite answers happily
on a file it has just created, so a wrong ``DATABASE_DIR`` or an
unmounted volume produced five empty files, an all-green ``/readyz``,
and a bot that died with ``no such table`` on the first real query. The
orchestrator kept such a process in rotation because readiness said it
was fine.

Two claims that used to sit here were also false and are gone (#682):
the probe does *not* "exercise the pragma listener" on every call —
``db/engines.py`` builds an ``AsyncAdaptedQueuePool``, so the ``connect``
event fires once per pooled connection, not once per probe — and it does
not "fail fast" on a missing file, because SQLite creates one.

``count(*) FROM sqlite_master`` is the cheapest signal that survives
both: a migrated deployment always has at least ``alembic_version``,
while a freshly-conjured file has nothing.

M-I-7: ``/healthz`` is now a pure liveness probe (process alive, event
loop responsive) and never touches the DB. The DB-touching probe moved
to ``/readyz`` so a transient DB lock doesn't make Kubernetes / systemd
kill-restart the process — it only takes the pod out of the load
balancer until the DB recovers.

The public anonymised view (:func:`check_databases_anonymised`) hides
the concrete DB file names behind ``db1`` .. ``dbN`` slots so a probe
response body can't leak the deployment's storage layout. The slot
order is the static :data:`telegram_invite_bot.db.names.ALL_DBS` tuple,
which is stable across restarts — operators correlate by slot index,
never by file name.
"""

from __future__ import annotations

from sqlalchemy import text

from telegram_invite_bot.db import EngineRegistry
from telegram_invite_bot.db.names import ALL_DBS, DBName

#: Connectivity *and* schema in one round-trip. A DB that answers with
#: zero tables is a file SQLite invented for us, not a database.
_SCHEMA_PROBE = text("SELECT count(*) FROM sqlite_master WHERE type = 'table'")


async def check_databases(registry: EngineRegistry) -> dict[DBName, bool]:
    """Probe every engine; return per-DB ok/fail. Internal use only —
    callers exposing this over HTTP must wrap with
    :func:`check_databases_anonymised` so file names don't leak.

    ``False`` means one of two things, deliberately collapsed: the
    engine would not answer at all, or it answered from an empty file.
    Both are "do not send this process traffic", and the route only ever
    reports a boolean per slot anyway.

    Note ``fsm.db`` is not covered — :data:`ALL_DBS` is the five-member
    :class:`DBName` tuple and the FSM store is built separately
    (``di/providers.py``). A broken FSM store is therefore invisible to
    readiness; that predates this function and is left alone here.
    """
    results: dict[DBName, bool] = {}
    for db in ALL_DBS:
        engine = registry.engine(db)
        try:
            async with engine.connect() as conn:
                tables = (await conn.execute(_SCHEMA_PROBE)).scalar()
            results[db] = bool(tables)
        except Exception:  # probe must never raise — surface as False
            results[db] = False
    return results


async def check_databases_anonymised(registry: EngineRegistry) -> dict[str, bool]:
    """Return the same per-DB readiness map keyed by anonymised slot
    names (``db1`` .. ``dbN``) rather than the concrete DB file names.

    Used by the ``/readyz`` HTTP endpoint where exposing
    ``users.db`` / ``economy.db`` / ``moderation.db`` would leak
    the storage layout to any unauthenticated caller. Slot order
    matches the static :data:`ALL_DBS` tuple so operators can still
    correlate slot ↔ DB through the source.
    """
    raw = await check_databases(registry)
    return {f"db{idx + 1}": raw[db] for idx, db in enumerate(ALL_DBS)}
