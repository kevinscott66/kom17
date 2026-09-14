# Lost / Not-Ported Features — Consolidated Migration Backlog

> Источник: сплошной аудит легаси-монолита (`bot.py`,
> `command_aliases.py`, `translations.py`, side-modules) против нового aiogram-пайплайна
> (`src/telegram_invite_bot/`, регистрация в `routers/main_router.py`).
> Аудит по шести доменам дал **144 сырых пробела → 76 уникальных пунктов** после дедупликации.
>
> Замечание: живую Telegram-группу/переписку прочитать невозможно (нет доступа к Telegram);
> авторитетный источник фич — легаси-код, по нему и собран бэклог.

**76 items across 6 domains.**

## STATUS: BACKLOG CLOSED (2026-06-13)

**Every item implementable without operator input is DONE and deployed.** Final tally:
- **DONE (~60/76):** waves 0-2 batches 1-7 + ranks epic (perm/cmdcfg/rank/staff_me/bang/groupadmin/transfer_rights) + P2P epic (full marketplace) + tail sweep (games menu, ai_limits, accept/decline, stake roll/flip with EN heads/tails, coins toggle, mute-protection item).
- **RETIRED:** L-18/L-19 (group-bet cpc, pvp_dice/pvp_coin — superseded by /duel); L-74/L-75 wiki/CMS (decommissioned).
- **DEFERRED with design blockers (documented in code):** L-21 partial — unwarn-item (which chat's warn?), xp_boost item (needs catalog+persistence), custom_title activation FSM.
- **BLOCKED on operator:** payments providers L-80..84/89-91/93-99/101 (need Stars/CryptoPay/YooKassa/Stripe keys + commission decisions); voice STT L-70-72 (needs whisper/ffmpeg infra decision).

## PROGRESS (updated 2026-06-10)

**~43/76 done and deployed to prod** across 4 batches (each: four disjoint file sets, shared files merged last, full suite green, then deploy):

- **Wave 0 verify** (commit 770ce19): L-39, L-68, L-69, L-73, L-11, L-13, L-14.
- **Batch 1** (0d4227a): L-01, L-03, L-04, L-05, L-06, L-07, L-30, L-46, L-85, L-86, L-87, L-88, L-96, L-100.
- **Batch 2** (629c6b4): L-24, L-25, L-26, L-37, L-38, L-92.
- **Batch 3** (b77f189): L-33, L-34, L-35, L-36, L-62, L-63, L-64, L-65, L-66, L-67, L-76.
- **Batch 4** (c84723e): L-43, L-47, L-52, L-57.
- L-29 (couple activities) was already done (FEAT-COUPLE).

**Deferred — need design / external keys (NOT auto-portable):**
- **Rank epic** (design needed): L-42, L-44, L-45, L-48, L-50, L-51, L-53, L-59. (L-48 /staff_me blocked on L-51.)
- **Payments / external keys**: L-80, L-81, L-82, L-83, L-84, L-89, L-90, L-91, L-93, L-94, L-95, L-97, L-98, L-99, L-101.
- **Voice STT infra** (ffmpeg + whisper): L-70, L-71, L-72.
- **P2P marketplace epic**: L-77, L-78, L-79.
- **Wiki/CMS: DECOMMISSIONED 2026-06-12** (L-74, L-75) — per recommendation, accepted under the standing «по твоей рекомендации» mandate: the Flask guide site + Telegraph publishing are not re-ported. Rationale: lowest ROI in the backlog, zero in-bot dependency (the /help button already degrades gracefully when TELEGRAPH_COMMANDS_URL is unset), and guides can be maintained as a hand-written Telegraph page whose URL is supplied via the existing TELEGRAPH_COMMANDS_URL env. Revisit only if the operator asks for an editable web CMS.
- **Finish-the-port (mechanical, schedulable)**: L-16, L-17, L-18, L-19, L-20, L-22, L-23, L-27, L-28, L-31, L-32, L-40, L-41, L-54, L-55, L-56, L-58, L-60, L-61.
- **Follow-up**: /modcfg toggles are persisted but not yet consumed by the warn/mute paths.

---


---

## Domain 1 — Command Surface (Telegram aliases / registration)

| ID | Name | What it did | Status | Size |
|----|------|-------------|--------|------|
| L-01 | cpc_cancel / кнб_отмена | Cancel an active rock-paper-scissors session | absent | S |
| L-02 | accept / decline (commands) | Standalone slash commands to accept/decline a game challenge (callback-only now) | absent | M |
| L-03 | marriage / my_marriage alias | Standalone command for own marriage status card | absent | S |
| L-04 | marry_top_on / marry_top_off | Toggle marriage inclusion in group rating (in_top flag) | absent | S |
| L-05 | marry_extend | Paid renewal extending marriage by N days (10 coins/day) | absent | S |
| L-06 | marry_auto_divorce | Configure auto-divorce mode one/two/off | absent | S |
| L-07 | marry_other | Look up another user's marriage by reply/mention | absent | S |
| L-08 | city / город | City info + timezone/weather geocoder (folded into /weather) | absent | M |
| L-09 | voice_settings_ru | Russian alias for voice settings (EN only registered) | absent | S |
| L-10 | ai_limits | Show remaining AI request quota | absent | S |
| L-11 | rate_stats (user-facing) | User-facing currency-rate update stats (admin path exists) | half | S |
| L-12 | shop_prices (user-facing) | User-facing shop price editor (admin path exists) | half | M |
| L-13 | botstats (user-facing) | User-facing bot-wide stats (admin path exists) | half | M |
| L-14 | check_groups (user-facing) | User-facing group-membership audit (admin path exists) | half | S |
| L-15 | games menu (command) | Interactive games command menu (folded into /shop keyboard) | half | S |

## Domain 2 — Economy & Games

| ID | Name | What it did | Status | Size |
|----|------|-------------|--------|------|
| L-16 | Group-form /duel + accept timer | Group PvP dice with reply/accept flow and live message edit | half | M |
| L-17 | /roll & /flip bet variants | Stake-based roll/flip touching economy.games, transactions, treasury commission | half | M |
| L-18 | Group-form bet /cpc | Group RPS-with-stakes, 5-min timeout, challenge-message edit (private ported) | half | L |
| L-19 | Legacy /pvp_dice & /pvp_coin | Pre-unification PvP dice/coin group challenge flow (superseded by /duel) | absent | L |
| L-20 | Game stats & leaderboards (/duel_stats) | Per-user games_played/won, win-rate, duel-specific stats table | half | M |
| L-21 | Shop inventory effects (double_daily, luck, color_nick) | Post-purchase auto-apply VIP bonuses/busters/effects | half | M |
| L-22 | Transfer tax & commission mechanics | Deduct transfer fee, route to bot/referrer; treasury commission splits | half | M |
| L-23 | Daily streak tracking & bonuses | Track consecutive claims, bonus % per streak, admin adjust/reset | half | M |
| L-24 | Gift/give coins (admin grant) | Admin grants coins/items without purchase | absent | S |
| L-25 | Anti-abuse game limits | Cooldown 180s, 8/hr, 25/day caps; admin-adjustable (partial in-memory limiter) | half | S |
| L-26 | Inventory auto-expire & cleanup | Scheduled removal of expired inventory items | half | S |
| L-27 | Achievement awarding for games | Award on game win, transfer/balance milestones | half | M |
| L-28 | Group treasury (wallet, /group_pay, splits) | Group wallets; donation splits to group/bot/referrer; payout FSM | half | L |
| L-29 | Paid couples joint-activities (💑) | Married pairs do joint activities for pair XP + timed effects (FEAT-COUPLE) | half | M |
| L-30 | Checks (coin-code vouchers) — create UI | Generate redemption codes; claim ported, create UI incomplete | half | S |
| L-31 | Admin economics panel | Per-game bet limits, roulette multiplier, streak config, price overrides | half | M |
| L-32 | Commission & referral payouts | Earnings from referral bonuses + transfer commissions | half | M |

## Domain 3 — Social

| ID | Name | What it did | Status | Size |
|----|------|-------------|--------|------|
| L-33 | /marriage status card | Personal marriage card (partner, date, level, XP) with activity-menu + history buttons | absent | M |
| L-34 | Marriage activity menu + history | Inline buttons on marriage card: 6 paid activities + last-15 history (card UI gone) | stub | M |
| L-35 | Relationship activity menu + history | Inline buttons on relationship card: 16 paid level-gated activities + history (card UI gone) | stub | L |
| L-36 | VIP profile effects (color_nick, custom_title, legend) | VIP-shop effects on mentions/profile; DB+repo exist, display layer not wired | half | M |
| L-37 | Referral depth/chain & earnings | Referral link, referred-list, cumulative commission chain analysis | half | S |
| L-38 | Donation ratings write-side | Admin group include/exclude toggles + rating-position recalc + history snapshots | half | S |
| L-39 | Marriage proposal accept/decline callbacks | marry_accept_<id>/marry_decline_<id> proposal-ID resolution (schema changed, verify) | half | S |
| L-40 | RPS spouse-PvP marriage XP bonus | Married users playing RPS earn couple XP instead of coins | absent | M |

## Domain 4 — Group Management, Moderation, Roles/Permissions

| ID | Name | What it did | Status | Size |
|----|------|-------------|--------|------|
| L-41 | /group_pay treasury withdrawal | Owner withdraws from group treasury (казна), min-amount + ownership gate | absent | M |
| L-42 | /groupadmin menu | Gateway menu to all group config: mod stats, word filter, staff, settings | absent | L |
| L-43 | /modcfg moderation config | Toggle auto-mod, profanity filter, auto-ban, max warns, mute duration | absent | M |
| L-44 | /perm /rankperm constructor | List/set per-rank permissions (can_warn/ban/mute/clear) for ranks 1-5 | absent | M |
| L-45 | /cmdcfg command-access control | Per-command minimum-rank requirement (0=all, 6=disabled) | absent | M |
| L-46 | /setrules write form | Admin form to set group rules text (read-side /rules ported) | half | S |
| L-47 | /clear chat cleanup | Delete last N messages or all from a user; owner-gated | absent | M |
| L-48 | /staff_me auto staff promotion | TG admin DMs bot to self-promote to MODERATOR rank | absent | S |
| L-49 | /transfer_rights ownership transfer | DM-only, confirmed, rate-limited owner transfer (changes owner_user_id) | absent | S |
| L-50 | !rank bang-rank promotion | !повысить/!!понизить/!!!strip shorthand to manage ranks | absent | M |
| L-51 | Group roles/rank architecture | Multi-tier GUEST→DEVELOPER ranks with per-rank permission matrix | stub | L |
| L-52 | Word/profanity filter UI | Admin add/remove banned words, auto-check every message | absent | M |
| L-53 | Group settings constructor UI | Inline forms: language, welcome, daily bonus, captcha, antiflood, economy mode | stub | L |
| L-54 | Group economy config | Treasury balance, min-withdrawal, per-group economy enable/disable | half | M |
| L-55 | Captcha on join | Optional per-group captcha for new members | absent | M |
| L-56 | Antiflood / rate limiting | Detect message bursts, auto-mute/warn/ban; per-group thresholds | absent | M |
| L-57 | Welcome message on join | Custom welcome on bot-add / new-member with templating | half | M |
| L-58 | Multi-group admin panel (mygroups) | Switch between managed groups, per-group stats/settings | half | M |
| L-59 | /cfg_button group-button customizer | FSM to customize group buttons/menu (entire customization subsystem) | absent | L |
| L-60 | alias / aliases (per-group dynamic) | Create custom command aliases per group (static routing only now) | absent | M |
| L-61 | ad / ads / reklama | Owner-only ads/announcements feature | absent | M |

## Domain 5 — AI, Voice, Wiki/CMS

| ID | Name | What it did | Status | Size |
|----|------|-------------|--------|------|
| L-62 | AI multi-mode dialog (7 modes) | default/chat/party/help/creative/code/expert role prompts, VIP-gated, per-user state | half/stub | M |
| L-63 | AI conversation history & per-chat context | 10-message rolling window per (user,chat); /reset; export/snapshot | absent | M |
| L-64 | AI group context injection | Inject group title + recent message window into system prompt | absent | S |
| L-65 | AI response caching (MD5) | Cache short historyless questions to skip re-querying DeepSeek | absent | S |
| L-66 | AI dynamic system context (time/city/weather/dice) | Inject local time, user city, weather, dice-roll into every request | absent | M |
| L-67 | AI extra_system reply-target instruction | Tell model which replied-to user to address in group | absent | S |
| L-68 | AI per-minute anti-flood (8/min, 5s pause) | Per-user rate limit distinct from daily quota (verify new limits match) | half | S |
| L-69 | AI daily quota (30/day non-VIP, unlimited VIP) | Daily request cap with readable error (verify config parity) | half | S |
| L-70 | Voice transcription (STT) | DONE — ported to OpenAI Whisper API (whisper-1, no ffmpeg/local model); group F.voice handler + target routing; degrades w/o OPENAI_API_KEY | DONE | L |
| L-71 | Voice settings UI (group admin) | DONE — /voice_settings menu: toggle/target/language, edit-in-place (model/device/auto-delete/only-admins dropped: no setters) | DONE | M |
| L-72 | Voice transcription stats | DONE — 📊 callback inside /voice_settings (total/avg-ms/last/preview); legacy TTS /voice_stats untouched | DONE | S |
| L-73 | /voice_vip info command | Explain VIP voice benefits/costs to non-VIP (verify /voice coverage) | half | S |
| L-74 | Guide site Flask routes (/commands, /commands/en, /commands/edit) | Markdown→HTML guide rendering + web editor (GUIDES_EDIT_SECRET) | absent | L |
| L-75 | Telegraph auto-publish + embedded links | Publish guide to Telegraph.ph; embed URL buttons in /help, /start, /admin | absent | M |
| L-76 | AI multi-turn private "Войти в Ком" mode | FSM mode where every plain message goes to the model | absent | M |

## Domain 6 — Payments, P2P, Withdrawal, VIP, Checks, Events

| ID | Name | What it did | Status | Size |
|----|------|-------------|--------|------|
| L-77 | P2P marketplace — sell orders | FSM to create sell orders (amount→currency→price→limits) in p2p_sell_orders | absent | L |
| L-78 | P2P marketplace — buy orders & order book | Browse/filter book, express buy, matching, seller stats, paging | absent | L |
| L-79 | P2P marketplace — escrow & trade lifecycle | Buyer-paid→seller-confirm→payout OR dispute→admin resolve; escrow hold | absent | L |
| L-80 | Telegram Stars payment | Buy coins via TG native Stars (pre_checkout → successful_payment) | absent | M |
| L-81 | CryptoPay invoices | BTC/TON/USDT invoices; webhook credits on payment | half | M |
| L-82 | Yookassa (RUB top-ups) | RUB checkout session; webhook credits coins from metadata | half | M |
| L-83 | Stripe (USD top-ups) | USD checkout session; webhook credits coins | half | M |
| L-84 | Buy coins menu (/buy) full flow | Multi-method top-up menu (Stars/Crypto/Yookassa/Stripe/custom) | half | M |
| L-85 | Check min_age gate | Reject claim if account age < threshold | half | S |
| L-86 | Check min_activity gate | Reject claim if user_history count < threshold | half | S |
| L-87 | Check allowed_countries gate | Geo-gate claim by country list (no real IP resolution) | half | S |
| L-88 | Check blocked_users gate | Reject claim if user_id in blacklist | half | S |
| L-89 | VIP custom emoji (purchase & display) | Buy emoji slots, set personal emoji, admin emoji panel | stub | M |
| L-90 | VIP voice quota & unlimited tiers | Tier-based voice daily quota / unlimited flag (no quota enforcement yet) | half | M |
| L-91 | VIP perks definition & expiry cleanup | Apply perks with duration; background expiry check + 3-day notice + auto-remove | half | M |
| L-92 | Withdrawal limits reset (daily/monthly) | Per-user withdrawal quotas reset on cycle; dispute-history rating | stub | S |
| L-93 | Payment provider runtime config | Admin hot-swaps provider API keys via encrypted runtime_secrets | half | S |
| L-94 | Owner broadcasts (/broadcast) | Mass message (text/photo/video) FSM: draft→preview→send + stats | absent | M |
| L-95 | Scheduled background tasks | Daemon threads: hourly economy cleanup, 5-min duel cleanup, VIP/check expiry | absent | M |
| L-96 | Promo / gift codes | Dedicated promo system (legacy used checks; no promo_codes table) | absent | S |
| L-97 | Admin withdrawal management | Admin approve/reject withdrawal requests; multi-method payout | half | M |
| L-98 | Withdrawal direct (bank card RUB) | User enters card details → request → admin approve/transfer | absent | M |
| L-99 | Withdraw instant (crypto direct, EOL) | Fast crypto withdrawal to wallet from bot wallet (deprecated) | absent | M |
| L-100 | Check rate calculations | Pre-calc average payout & total hold for random checks; balance validation | half | S |
| L-101 | Seasonal events & time-limited offers | Limited-time offers, event-triggered bonuses, time-gated content (never existed) | absent | M |

---

## SPECIAL CALLOUTS (user-requested)

### (a) AI agents — выполняют ли действия / function-calling / tool-use? Что потеряно?

**Вердикт: настоящей агентности никогда не было, а «псевдо-агентная» симуляция, которая была, — потеряна.**
Легаси-AI — это одиночный DeepSeek-completion, НЕ агент с function-calling/tool-use. Не было ни tool-call-цикла,
ни автономного выполнения действий, ни JSON-schema-диспетчеризации функций. Вместо этого легаси *симулировал*
агентность, предвычисляя данные в Python и инжектируя их в system-prompt:

- **Кубик (L-66):** бот ловил «брось кубик», кидал кость в Python и встраивал результат в промпт, чтобы модель его *озвучила*. Модель не вызывала инструмент — это делал хост. **Полностью потеряно.**
- **Погода (L-66):** детект интента → Python-запрос погоды → инжект в промпт. **Полностью потеряно** — теперь вопросы о погоде галлюцинируют/отказывают.
- **Время/город/дата (L-66):** локальное время, город, день недели предвычислялись и инжектились. **Потеряно.**
- **Reply-target (L-67):** модели сообщалось, к какому пользователю обращаться. **Потеряно.**
- **Контекст группы (L-64):** заголовок группы + недавние сообщения. **Потеряно** — ответы теперь «слепые».
- **Память (L-63):** окно 10 сообщений на (user,chat) + экспорт/снапшот. **Потеряно** — каждый запрос stateless.
- **Режимы/персоны (L-62):** 7 VIP-персон. **Half/stub** — текст справки рендерится, UI/callbacks смены режима нет.

**Итог:** если цель — «AI-агенты, которые *делают действия*», такой способности **никогда не было**, и это net-new работа
(реальный function-calling-цикл над DeepSeek/Claude с tool-определениями: кубик, погода, баланс и т. д.).
Конкретно регрессировала *симуляция через инжект контекста* (L-62…L-67, L-76) — комбо-опыт «ком, кинь кубик и скажи погоду»
сломан полностью. Восстановить паритет = переписать инжект-хелперы; *апгрейд* до реальной агентности = новый tool-use-цикл,
рекомендуется как отдельный эпик, а не «порт».

### (b) Вики / CMS — что портировано vs отсутствует?

**Вердикт: не портировано ничего. Вся CMS-подсистема отсутствует.** (L-74, L-75)

- **Отсутствует — рендер гайда:** Flask-приложение `guide_site.py`, отдающее `/commands` (RU) и `/commands/en` (EN),
  Markdown→HTML (h1-h4, bold/italic/code/списки/ссылки) из `telegraph_guide_*.md` / `guides_markdown_override_*`. В новом дереве `handlers/` веб-приложения нет.
- **Отсутствует — веб-редактор:** аутентифицированный `/commands/edit` (`GUIDES_EDIT_SECRET`) для живого редактирования гайдов.
- **Отсутствует — публикация в Telegraph:** авто-публикация HTML на telegraph.ph и встраивание URL-кнопок в `/help`, `/start`, `/admin`. `admin_telegraph_update` помечен intentionally-legacy; ссылки опущены.
- **Портировано:** только in-bot AI Markdown→HTML рендер-фикс (отдельный, уже готов — не часть CMS).

Это фактически **greenfield-re-port**, если CMS-гайд нужен; иначе явно вывести из эксплуатации и перенести гайды в статичный in-bot текст.

---

## RECOMMENDED PORT ORDER (Waves)

### Wave 0 — Verify-and-close (≈ноль усилий; модели/хендлеры уже есть; подтвердить паритет)
L-68, L-69 (AI rate-limit/quota parity), L-39 (marriage proposal callback IDs), L-73 (voice_vip coverage),
L-11, L-13, L-14 (admin-path-команды — решить, нужен ли user-facing алиас). В основном подтверждение, возможно no-op.

### Wave 1 — Quick wins (S, модели есть, высокая ценность)
L-01, L-03, L-04, L-05, L-06, L-07 (marriage command surface), L-30 (check create UI), L-24 (admin gift coins),
L-25, L-26 (game limits/cleanup), L-37, L-38 (referral/rating read-write), L-48 (staff_me), L-46 (setrules write form),
L-85–L-88, L-100 (check gates — схема есть), L-92 (withdrawal limits reset), L-96 (promo codes).

### Wave 2 — Medium features (M; доделать частичные порты или замкнутые новые флоу)
L-02, L-08, L-09, L-10, L-15 (command surface). L-16, L-17, L-20, L-21, L-22, L-23, L-27, L-29, L-31, L-32 (economy finish).
L-33, L-34, L-35, L-36, L-40 (social card-UI). L-43, L-44, L-45, L-47, L-50, L-52, L-54, L-55, L-56, L-57, L-58, L-60, L-61
(moderation/roles/group config). L-62–L-67, L-76 (AI context-injection parity). L-71, L-72 (voice settings/stats).
L-80–L-84, L-89, L-90, L-91, L-93, L-94, L-95, L-97, L-98, L-99, L-101 (payments/VIP/withdrawal/events).

### Wave 3 — Large epics (L; подсистемы; ставить поздно и ресурсировать)
- **P2P marketplace:** L-77 → L-78 → L-79 (sell → book → escrow, в этом порядке).
- **Group-admin:** L-42 (gateway) + L-51 (rank architecture) + L-53 (settings constructor) + L-59 (cfg_button) —
  взаимозависимы; сначала ядро ранг/прав, потом конструкторы.
- **Treasury:** L-28 + L-41 (group wallet + payout).
- **AI/Voice:** L-70 (group STT, faster-whisper) — тяжёлая инфра (ffmpeg, хостинг модели).
- **Wiki/CMS:** L-74 + L-75 — решить *порт vs decommission* до вложений; вероятно низший ROI.
- **Stake-game:** L-18, L-19 (group bet /cpc, legacy pvp_* — или формально ретайр в пользу /duel).

**Логика последовательности:** Waves 0–1 возвращают максимум видимой поверхности за минимум усилий.
Wave 2 — основная инженерная масса (доделать половинчатые флоу). Wave 3 — эпики (особенно P2P и ядро ролей/прав)
скоупить как отдельные проекты со своими дизайн-доками: схема, FSM, money/permission-safety.
