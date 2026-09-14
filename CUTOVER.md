# Strangler cutover: Flask + telebot → FastAPI + aiogram 3

**Historical record. Completed 2026-05-26 (T-011). Nothing here is an
instruction.**

For most of the migration the two pipelines ran side by side behind a
bridge. A single environment variable, `ENABLE_NEW_PIPELINE`, decided
which of them owned an incoming update:

```
Telegram ──▶ webhook ──▶ compatibility_bridge ──┬─▶ aiogram 3 routers
                                                └─▶ legacy telebot (bot.py)
```

Unset, every update went to the legacy monolith. Set, each update was
offered to the aiogram dispatcher first and fell back to the monolith
only when no router claimed it. That asymmetry is what made the cutover
reversible: the flip was one variable and one restart in either
direction, and the fallback meant a missing router degraded to the old
behaviour instead of dropping the update.

**T-011 removed the bridge.** Every handler that mattered had been
ported and pinned by a parity test, so the fallback had stopped firing.
The entry point is now `python -m telegram_invite_bot --mode=webhook`,
the `ENABLE_NEW_PIPELINE` setting no longer exists, and there is no
legacy service left to fall back to on any host.

`bot.py` is still in the tree, and deliberately so: the parity tests
cite its line numbers for the behaviour they preserve — including the
behaviour that was merely accidental. It is the evidence for the
migration, not a runnable artefact.

`tests/regression/test_legacy_process_is_gone.py` is the guard that
keeps this document true: it fails if the bridge, the setting or the
legacy entry point reappear in `src/`.
