# KOM17

[![CI](https://github.com/kevinscott66/kom17/actions/workflows/ci.yml/badge.svg)](https://github.com/kevinscott66/kom17/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

A Telegram community platform, and a strangler-fig refactor carried out on a
bot that never stopped serving live groups.

KOM17 runs invites, moderation, an in-chat economy with real payments, games,
an AI assistant, statistics and an admin panel. The interesting part is not the
feature list — it is that a 45,000-line single-file `telebot` monolith was
replaced module by module with a typed, dependency-injected `aiogram 3`
application, without a cutover weekend, while it kept serving live groups.

## The strangler migration

`bot.py` is the original monolith; `src/telegram_invite_bot/` is the
replacement that grew out of it. For most of the migration the two pipelines
ran side by side behind a bridge, and a single environment variable decided
which of them owned an incoming update: unset, everything went to the monolith;
set, each update was offered to the aiogram dispatcher first and fell back to
the monolith when no router claimed it. That asymmetry is what made the port
reversible — one variable and one restart, in either direction — and it is why
the work could proceed one feature at a time with no feature freeze.

The bridge is gone. The cutover completed in May 2026: every handler that
mattered had been ported and pinned, so the fallback had stopped firing.
Today an update takes one path.

```
Telegram ──▶ FastAPI webhook ──▶ aiogram 3 Dispatcher ──▶ routers
                                                             │
                                       dishka DI ──▶ services
                                                             │
                                repositories ──▶ SQLAlchemy 2 (async)
                                                             │
                                            5 independent SQLite engines
```

[`CUTOVER.md`](CUTOVER.md) is the record of how that was done, and of why
`bot.py` is still in the tree: every migrated handler is pinned by a parity
test against the legacy behaviour it replaced — including the behaviour that
was merely accidental — and those tests cite `bot.py` line numbers for the
rules they preserve. The monolith is the evidence, not a fallback.

## What it does

| Area | Highlights |
|------|-----------|
| Community | Invite tracking, referrals, group registry, welcome and events |
| Moderation | Anti-flood, warnings, mutes, bans, staff ranks, audit log |
| Economy | Coins, shop, transfers with tax, group treasuries, bonds, promo codes |
| Payments | Crypto Pay, YooKassa, Stripe and RollyPay, each behind a signed webhook |
| Games | Duels, PvP, rock-paper-scissors, card games, achievements, leaderboards |
| AI | DeepSeek and OpenAI assistants, Whisper transcription, TTS voice replies |
| Social | Relationships, marriage, couple activities, profiles, ranks |
| Web | Public command guide, offer and privacy pages, contact form (`cms/`) |
| Ops | Health checks, Prometheus-style counters, Sentry, structured logging |

Interface is bilingual (RU/EN) through `i18n/`.

## Economy safety

An in-chat currency that converts to USDT is an abuse surface, so the payout
path is bounded in several independent places rather than one:

- **Minting is rate-limited.** Passive per-message earning is gated by
  cooldown, per-minute ceiling, duplicate-text window, minimum length and a
  per-user daily cap.
- **Cash-out requires real money in.** An account can only withdraw once at
  least one `purchase_*` ledger row exists.
- **Lifetime payout is capped** at a configurable ratio of lifetime deposits,
  because "did money ever come in" is a threshold, not a bound.
- **Rolling daily and monthly withdrawal limits**, derived live from request
  timestamps so the window self-resets.
- **Per-user AI quotas**, because `/ai`, `/ask` and `/voice` cost the user
  nothing and bill the operator's provider key.

Every knob has a documented default and a documented "off" value. The reasoning
behind each is in `docs/ECONOMY_RATE_AUDIT.md`.

## Stack

Python 3.11+ · aiogram 3 · FastAPI · uvicorn · dishka (DI) · pydantic v2 +
pydantic-settings · SQLAlchemy 2 async · aiosqlite · Alembic · httpx · loguru ·
pytest · Docker · systemd · nginx

148k lines in `src/`, 186k lines of tests across 578 test modules (9 212 tests),
42 Alembic migrations across five independently versioned databases.

## Quick start

```bash
uv sync                 # or: pip install -r requirements.txt
cp .env.example .env    # fill in BOT_TOKEN, WEBHOOK_URL, ADMIN_CHAT_ID

# Five databases, five independent Alembic lineages. The wrapper points
# `version_locations` at migrations/versions/<db>/ before the script
# directory is built — plain `alembic upgrade head` silently no-ops here.
for db in users economy activity moderation message_stats; do
  python -m scripts.alembic_run -x db=$db upgrade head
done

python -m telegram_invite_bot --mode=polling      # local development
python -m telegram_invite_bot --mode=webhook      # production
```

Polling and webhook are the two run modes, and they are mutually exclusive per
bot token: Telegram refuses `getUpdates` while a webhook is set, so a dev
instance needs its own token. In webhook mode the app serves FastAPI on
`HOST:PORT` and calls `setWebhook` on startup — behind a tunnel
(`ngrok http 8080`) or a reverse proxy, with the public HTTPS URL in
`WEBHOOK_URL`.

```bash
pytest                  # full suite
ruff check . && mypy src
```

## Layout

```
src/telegram_invite_bot/   aiogram 3 application: handlers, services,
                           repositories, DI, i18n, CMS, scheduler, webhook
bot.py                     legacy monolith, kept as the parity tests' source
tests/                     unit, integration, e2e, regression, parity
migrations/                Alembic, five separate database lineages
docs/                      design notes, economy audit, backlogs
```

## Notes on this repository

This is a published snapshot of a working private repository, not the working
repository itself. Server host names, IP addresses and deployment identifiers
have been removed rather than masked; the product's own public domain stays,
because it is public. No credentials are committed — everything is supplied at
runtime through the environment, and `.env.example` lists every variable the
application reads.

Deployment scripts, operational runbooks, the production schema dump, backups
and internal audit reports are deliberately not part of this repository, so a
few references in the code point at files that are not here:

- `docs/prod_schemas.sql` — the schema dump the migrations and repository tests
  were checked against;
- `docs/DEPLOY.md` — the deploy runbook the admin status card links to;
- `audits/*.md`, and finding ids such as `M-E-4` or `SEV-2` — internal review
  reports.

The files stay private; the code they explain is here in full.

### Reading the references

Comments and tests carry short ids so that a rule can be traced back to the
reason it exists:

| Id | Meaning |
|----|---------|
| `T-nnn` | a task of the strangler port — a feature moved or a piece of the bridge removed |
| `R-FIX-nnn` | a defect found in review of the ported code, and the fix that pins it |
| `L-nn` | an entry in [`docs/LOST_FEATURES_BACKLOG.md`](docs/LOST_FEATURES_BACKLOG.md) — legacy behaviour that did not survive the port |
| `R1`–`R12` | a recommendation of the economy audit, [`docs/ECONOMY_RATE_AUDIT.md`](docs/ECONOMY_RATE_AUDIT.md) |
| `#nnnn` | an issue in the private tracker |
| `bot.py:N` | the legacy line a parity test preserves; checked by `tests/regression/test_source_citations.py` |

Documentation and configuration comments are partly in Russian — the product's
primary community language.

## Licence

MIT — see [LICENSE](LICENSE).
