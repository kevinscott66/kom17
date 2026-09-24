# KOM17

[English](README.md) · [Русский](README.ru.md)

[![CI](https://github.com/kevinscott66/kom17/actions/workflows/ci.yml/badge.svg)](https://github.com/kevinscott66/kom17/actions/workflows/ci.yml) · [MIT](LICENSE)

A Telegram community platform with invites, moderation, an in-chat economy, payments and AI features. The central engineering work was replacing a monolith one subsystem at a time.

**Status:** Operating product · public snapshot. Cutover completed 26 May 2026.

[ Case study ](https://dobropalm.tech/case-studies/kom17/) · [Portfolio](https://dobropalm.tech) · [Live product](https://t.me/kom17bot)

![The actual post-migration request path, shown as an architecture illustration rather than a screenshot of private community chats.](https://dobropalm.tech/assets/media/kom17-architecture.svg)

_The actual post-migration request path, shown as an architecture illustration rather than a screenshot of private community chats._

## Problem & outcome

A running community already has rules, accumulated data and familiar workflows. A full rewrite with one big switch makes every missing detail a user problem.

On 26 May 2026, the cutover completed to a single FastAPI → aiogram 3 path. The bridge is no longer a runtime fallback. A regression test guards against restoring the old entry point; the monolith remains evidence for parity tests.

## My contribution

I created KOM17 as my own product: the first version was a monolith, then I split it into modules. I own the community and economy logic, both architecture stages and the migration requirements: which workflows must survive, where module responsibilities sit and when the old execution path can be removed.

I use AI tools in development; product and architectural decisions are my responsibility.

## Engineering highlights

- **Migrate behavior, not just code.** Economy and moderation rules are pinned before replacement. A cleaner implementation can otherwise silently change the product.
- **Finish the transition.** A bridge is useful while rollback depends on it. After cutover, a regression test guards its removal so temporary architecture does not become permanent.
- **Migrations follow data ownership.** Five SQLite databases have separate Alembic lineages. A wrapper explicitly selects each database.

## Architecture & stack

| Layer | Implementation |
|---|---|
| Frontend | Telegram; public web pages and administration |
| Backend | Python, aiogram 3, FastAPI, dishka |
| Data | SQLAlchemy 2 async, five SQLite databases, Alembic |
| Infrastructure / AI | Docker / systemd; metrics, Sentry; text and speech providers |

users, economy, activity, moderation and message_stats have independent schema versions. scripts.alembic_run selects the migration lineage. A plain Alembic command without selecting the database does not replace migrating all five stores.

## Quick start

```bash
# Requires Python 3.11+ and uv.
git clone https://github.com/kevinscott66/kom17.git
cd kom17
uv sync --all-extras --dev
uv run pytest tests/regression/test_legacy_process_is_gone.py -q --no-cov
```

This checks the public snapshot without connecting a bot. For local operation, copy `.env.example` to `.env`, configure a separate test bot and local database paths, apply each database’s migrations through `scripts.alembic_run`, then use polling mode. A live token with a webhook must not also be used for polling.

```bash
# After configuring local .env and database paths:
for db in users economy activity moderation message_stats; do
  uv run python -m scripts.alembic_run -x db=$db upgrade head
done
uv run python -m telegram_invite_bot --mode=polling
```

## Checks

```bash
uv run ruff check .
uv run mypy src
uv run pytest -q --no-cov
```

The badge links to the actual workflow. Listing a command does not claim every check ran for each README edit.

## Deployment, observability & API

The webhook checks Telegram’s secret header before dispatch. /healthz, /readyz and /metrics provide separate signals for process health, database readiness and application behavior. Payment integrations have their own validation and replay accounting.

```bash
# For a locally configured webhook server; substitute its configured port:
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
curl --fail http://127.0.0.1:8000/metrics
```

Liveness and readiness are intentionally separate. Metrics and operational endpoints should be protected according to your deployment. The examples do not send Telegram messages or payment requests.

## Security & limits

The public repository is a snapshot, not the complete development history. Local operation requires a separate bot and configuration. End-to-end exactly-once is not claimed for every handler; redelivery safety belongs to the individual operation.

Incremental replacement needs more compatibility code and tests than starting over. In return, changes can be checked in smaller steps. Separate databases establish data ownership but complicate migrations and cross-database consistency.

Disclosure policy: [SECURITY.md](SECURITY.md).

## History & documentation

KOM17 is my own product: it began as a monolith and was later split into modules. This repository is a public snapshot of the working project, not the full private history. The cutover completed on 26 May 2026. `bot.py` remains evidence for parity tests, not the active runtime.

- [Completed cutover record](CUTOVER.md)
- [Guard against legacy entry points](tests/regression/test_legacy_process_is_gone.py)
- [Health and delivery semantics](src/telegram_invite_bot/webhook/server.py)
- [Migration wrapper](scripts/alembic_run.py)
- [Public snapshot and scope](README.md)

## License

MIT - [LICENSE](LICENSE).
