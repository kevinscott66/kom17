# DESIGN: Ranks & Permissions epic (L-42/44/45/48/50/51/53)

> Status: **SHIPPED.** Kept as an ADR — this is the design the
> implementation was built from.
> Source of truth: deep-read of legacy (every claim cited in the research
> log; key anchors: RankLevel bot.py:6537-6549, storage bot.py:6051,
> matrix bot.py:2611-2712, precedence bot.py:7555-7577, can_moderate
> bot.py:7580-7622, bang-commands bot.py:31324-31441, /cmdcfg
> bot.py:42808-42894, /staff_me bot.py:41462-41513, staff-sync
> bot.py:7479-7519).

## 1. What legacy actually had (facts)

- **Ranks are GLOBAL per user** (`users.rank INTEGER DEFAULT 0`), not
  per-group: −1 BANNED, 0 USER, 1 JUNIOR_MOD, 2 MODERATOR, 3 SENIOR_MOD,
  4 ADMIN, 5 OWNER, 6 DEVELOPER (immutable, = DEVELOPER_IDS).
- **Permission matrix** `rank → {14 can_* flags}` lives in settings.json
  (`rank_permissions`), editable via `/perm set <rank> <perm> on|off`
  (owner-only).
- **Per-group staff overrides** in `group_roles` (group_id, user_id,
  role, permissions JSON) — local permissions take precedence over the
  global matrix when present.
- **Command access** `/cmdcfg`: per-command minimum rank (0=all …
  6=disabled) as `command_rank_overrides` in settings.json over a
  hardcoded COMMAND_CATALOG of defaults.
- **Precedence:** DEVELOPER_IDS → Telegram admin with mod-rights
  (bypasses ranks entirely) → global rank (+ group_roles override) →
  none. `can_moderate` additionally blocks self-moderation, moderating
  the chat creator, and targets with rank ≥ actor rank.
- **Key power:** a ranked user WITHOUT Telegram adminship can
  warn/mute/ban — the BOT applies the action with its own admin rights.
- **Staff-sync:** periodic job demotes rank≥1 users who lost TG
  adminship to 0 and auto-promotes TG-admins-with-mod-rights to rank 2.
- **/staff_me:** a TG admin of the MAIN chat (CHAT_ID) DMs the bot and
  self-assigns rank 2 if unranked.
- **Bang-commands:** `!повысить`/`!!понизить`/`!!!разжаловать`
  (promote/demote/strip), bang count = level (1-5), targets via
  reply/@mention. Gated on `require_group_moderation(message,
  "can_manage_ranks")` (bot.py:31399) = TG-admin-of-this-group OR a
  rank grant — but `can_manage_ranks` is defined at **no** rank in the
  default matrix (bot.py:2611-2712), so the rank half is dead code and
  the gate resolved to "developer, or any TG admin of any group".
- **Dead code (NOT to port):** `command_permissions` DB table (never
  read), `role_checker.py` model (superseded), per-group command
  overrides (schema only).

## 2. New-pipeline design

### 2.1 Storage (3 pieces, all in existing DBs — 2 migrations)

1. **Global rank:** the `users.rank` column ALREADY EXISTS in the new
   schema (confirmed: PRAGMA on prod shows `rank` as the last users
   column) — **no migration**; add read/write methods to `UsersRepo`
   (`get_rank`, `set_rank` with the developer guard) + 300s TTL cache
   mirroring the language-middleware pattern (`invalidate_rank_cache`).
2. **Permission matrix:** new `rank_permissions` table in
   **moderation.db** (`rank INTEGER, permission TEXT, allowed INTEGER,
   PRIMARY KEY(rank, permission)`) seeded EMPTY — reads fall back to the
   in-code legacy default matrix (verbatim bot.py:2611-2712), writes via
   `/perm set` store only overrides (same "store-only-deltas" shape as
   legacy's settings.json). Migration `moderation 0009`.
3. **Command access:** new `command_rank_overrides` table in
   moderation.db (`command_key TEXT PRIMARY KEY, min_rank INTEGER`),
   same delta-only semantics over an in-code `COMMAND_CATALOG`
   (ported keys + default ranks). Migration shares `0009`.
   *Deviation from legacy:* legacy kept both in settings.json; we use
   moderation.db because the new pipeline has no settings.json writer
   and DB rows are transactional/auditable. Display-neutral.

   **Deferred (out of scope v1):** legacy `group_roles` per-group
   permission JSON. The global matrix + per-group `/modcfg` already
   cover the real use; per-group staff overrides come later if asked.

### 2.2 Enforcement (the contract)

New module `services/rank_service.py` exposing ONE seam:

```
await rank_service.check(actor_id, chat_id, "can_warn", bot, settings)
  -> RankVerdict(allowed, reason, actor_rank)
```

Precedence (legacy-exact): `settings.bot.is_developer` → live TG-admin
with mod-rights (the EXISTING `_require_admin`/`_is_user_admin` check —
reused, not reimplemented) → global rank vs matrix. Plus
`can_moderate(actor, target, chat)` (self/creator/rank-≥ guards,
bot.py:7580-7622 verbatim).

**Integration into moderation.py is ADDITIVE:** today's gate is
"TG admin only". The new gate becomes "TG admin OR ranked-with-
permission" — strictly widening, never narrowing, so current behavior
is preserved for every existing admin. The `can_moderate` target-guards
are added to warn/mute/ban/kick (they only ADD safety).

`check_command_access` middleware (the /cmdcfg half) is a root outer
message middleware (pattern: word-filter) that maps the incoming
command to its catalog key and rejects below-min-rank users with the
localized denial (dev bypass first). Default catalog = all commands at
their legacy default ranks (most 0; moderation 2; admin tools 5).

### 2.3 Command surface (all admin-gated as stated)

- `/perm list <rank>`, `/perm set <rank> <perm> on|off` (+`/rankperm`)
  — **developer-only** (legacy owner-only ≈ dev in new pipeline).
- `/cmdcfg list [category] | show <cmd> | set <cmd> <0-6> | reset
  <cmd|all>` — developer-only.
- `/staff_me` — DM-only; TG-admin-of-main-chat (settings.bot
  .main_chat_id) self-assigns rank 2 if unranked (legacy-exact).
- Bang-commands `!повысить/!!понизить/!!!разжаловать` (+EN
  promote/demote/strip) — requires `can_manage_mods`, targets via
  reply/@mention, developer-immutability guard **plus** a
  "no level >= your own rank" guard legacy did not have.
  **Verified at impl time, and the assumption above was wrong:**
  legacy's `can_manage_ranks` does *not* map to the matrix's
  `can_manage_mods` row — it maps to nothing (see §1), so the port's
  choice is a deliberate divergence in both directions: it drops
  legacy's TG-admin bypass (narrower) and grants the write to ranks
  4/5/6 (wider). Switching to `can_manage_ranks` would restore strict
  legacy parity, i.e. developer-only. Open owner decision — the full
  argument lives on `core.ranks.MANAGE_RANKS_PERMISSION`.
- `/rank [reply|@user]` — read-only rank card (legacy `your_rank`).
- **Staff-sync:** ported as part of the hourly EconomyCleanupSweeper
  cadence? NO — it needs per-chat getChatAdministrators; run it lazily
  per-chat with a 10-min cache when a ranked-but-not-TG-admin user
  invokes a moderation command (cheaper, same net effect), PLUS keep
  legacy's auto-promote OFF by default (it surprised operators) behind
  `RANK_AUTOSYNC=0|1` env (default 0 = demote-on-loss only).
  *This is the one deliberate behavior deviation — flagged for sign-off.*
- `/groupadmin` menu + `/cfg_button` constructor (L-42/53/59): **wave 2
  of the epic** — thin inline menu over the above primitives once they
  prove out.

### 2.4 i18n

Reuse legacy keys where byte-identical content survives
(`rank_level_0..6`, `rank_set_done`, `can_moderate_higher`, …) — they
are already in the yaml (legacy parity). New copy (denials, /cmdcfg UI)
gets `h_rank_*` / `h_cmdcfg_*` keys, RU+EN, HTML.

### 2.5 Implementation plan (one batch, 4 disjoint work units)

- **R1 core:** UsersRepo rank methods + cache, rank_permissions +
  command_rank_overrides tables/repos (migration moderation 0009),
  RankService (check/can_moderate/matrix-fallback), default matrix +
  COMMAND_CATALOG constants, unit+integration tests.
- **R2 management:** /perm, /cmdcfg, /rank handlers (on top of the R1
  API), tests.
- **R3 self-service:** /staff_me + bang-commands + lazy staff-sync,
  tests.
- **R4 enforcement:** widen moderation.py gate (TG-admin OR rank),
  can_moderate target-guards, command-access middleware + main_router
  attach, e2e tests incl. "ranked non-TG-admin can warn" and "rank
  cannot moderate creator".
- Integration pass: i18n merge, migration linearize, router/middleware
  attach, full suite, deploy, userbot verify (promote a second test
  account via bang-command and have it /warn).

## 3. Risks & mitigations

- **Security-sensitive:** every widening is covered by an e2e proving
  the OLD gate still passes and a NEW e2e proving rank-without-TG-admin
  works but cannot touch creator/higher ranks. The command-access
  middleware fails OPEN below rank-gate errors (a DB hiccup must not
  brick all commands) — matching word-filter's never-raise posture.
- **Cache staleness:** 300s rank cache + explicit invalidation on
  set_rank (same proven pattern as language cache, incl. the test-
  isolation clear() hook).
- **Bot privacy mode:** bang-commands are plain text — they only work
  where the bot is group admin (documented; already true for automod).
