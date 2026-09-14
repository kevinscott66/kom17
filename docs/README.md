# Project notes

Durable facts about the project. This is not a second copy of the code —
the truth about the code is the code, and its history is in git. What
lives here is the reasoning that the code cannot carry: why a rate is
what it is, what a subsystem was meant to do, what is still missing.

| Topic | Where |
|---|---|
| Economy, rates and the reasoning behind them | `ECONOMY_RATE_AUDIT.md`, `RICHNESS_REGRESSIONS.md` |
| Subsystem designs | `DESIGN_P2P.md`, `DESIGN_RANKS.md`, `CUSTOM_EMOJI_VIP_SPEC.md` |
| Relationships and marriage | `RELATIONSHIPS_AND_MARRIAGE_LOGIC.md` |
| Backlogs | `LOST_FEATURES_BACKLOG.md`, `REMAINING_WORK.md` |

The move from the monolithic `bot.py` to `src/telegram_invite_bot/` is
described in [`CUTOVER.md`](../CUTOVER.md) at the repository root.

Documents are partly in Russian — the product's primary community
language. The operational half of these notes (production topology,
deployment, disaster recovery, live database schemas) is not part of the
public snapshot.
