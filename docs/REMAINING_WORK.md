# Remaining work & finalization plan

Status snapshot: the strangler migration (telebot → aiogram 3) is done — the
legacy bridge is removed, all core handlers are ported, the suite is green, and
the service runs in production. What follows is the residual backlog, split by
whether it's executable locally, blocked on an explicit owner decision, or an
unported/half-ported feature. Deployment topology, runbooks and recovery
procedure are operational and are not part of this snapshot.

## A. Blocked on the user (NOT started without confirmation)

1. ~~**Production deploy**~~ — done. The blue/green cutover ran on the old box
   in May 2026 and the service was rebuilt on the current host after that box
   died; `docs/DEPLOY.md` now describes the routine ship-a-build procedure that
   replaced it. **The hard constraint stands: prod is never touched without an
   explicit go-ahead.**
2. **Free-form NLP** weather/time matchers (`какая погода в…` without a slash).
   Held: a fuzzy group-message matcher risks stealing ordinary chatter — needs
   an explicit "yes, restore it" before wiring.
3. **Delete `translations.py`?** — the legacy dict *is* still in this repo
   (283 KB at the root) and `tests/unit/i18n/test_legacy_parity.py` really
   runs against it: it asserts every legacy `key → value` pair is byte-for-byte
   the YAML, minus a short allowlist of deliberate behavioural divergences.
   An earlier revision of this list claimed the file was gone and the test
   self-skipping — both wrong, and acting on it would have removed a guard
   that still fires. It caught the #154 copy fix on 2026-08-19: the string
   was patched in the YAML, the parity test failed, and `translations.py`
   was patched to match rather than exempted.

   That is the whole value of keeping it: the YAML is a *derived* copy, and
   this is the only thing that notices when the two drift. The cost is that
   every copy edit is a two-file edit. Deleting it is the owner's scope call
   — it trades that check for a lighter edit loop, and the test (plus its
   `importlib` shim) goes with it in the same commit.
4. **RollyPay terminal: `fee_payer` is empty.** The merchant absorbs the
   acquirer fee, but the bot credits coins from the **gross** amount the
   callback reports (`services/payments/rollypay.py`, `coins_for_rub(amount,
   …)`), so a pure in-and-out round trip loses exactly that fee.
   `WITHDRAW_PAYOUT_RATIO` is `1.0`; R6's lifetime cap bounds the loss at the
   fee and no more, and the loss only realises if the user actually withdraws.
   Every way to close it changes what real customers are charged or what the
   published offer promises, so none is applied unilaterally. **If the owner
   ever flips `fee_payer` to the payer, the adapter must switch to crediting
   the base amount rather than `data["amount"]`** — otherwise the fee the
   customer just paid on top gets minted as coins.
5. **RollyPay terminal: settlement is `t_plus_1`** — conversion happens a day
   after the payment, so the owner carries a day of FX exposure on every
   payment taken. Fine if the payout currency matches, a real (small)
   directional bet if it is not. Owner's call; the bot does not read this
   field.

6. **Delete the unused `telegram_invite_bot.commands` package?** As of
   2026-08-19 nothing in `src/` imports it — the only importers are its own
   tests under `tests/unit/commands/`. It is not the catalog the bot runs on:
   `/help` and the public `/commands` page come from `core/ranks.py`
   (`COMMAND_ENTRIES`) plus `handlers/help_catalog.py`, plain-text aliases come
   from `middlewares/text_alias.py`, and dispatch is aiogram's own `Command`
   filter. Editing `commands/data/registry.yaml` changes nothing at runtime,
   yet the module is named "command registry" and reads like the source of
   truth — the next person to "fix" a command by editing that YAML will get no
   effect and no error. (An earlier version of this item said eleven of its 122
   rows "have no handler anywhere" — wrong twice over, #709: *all* 122 are
   handler-less in `src/`, and the eleven named ones all do have legacy handlers
   in `bot.py` / `rock_paper_scissors.py`.) Worse, the YAML has drifted from the
   catalog that is live: 122 registry keys vs 149 catalog rows, 105 shared of
   which 74 carry different alias sets, and two rows are actively wrong —
   `create_check` still claims the `check` alias that `core/ranks.py:472-484`
   documents as the bug that refused every claim below rank 5, and `dice` claims
   `кубик`, which the catalog deliberately assigns to `roll`. Neither can bite
   while the module is inert; both would the moment someone wires it up. The one
   problem it was written to solve — stripping the `cmd@botname` suffix Telegram
   appends in groups — is solved natively by aiogram's `Command` filter, and it
   was never a live problem in legacy either, since `get_canonical_command` had
   no reachable call site (#710). So the module has no remaining job. A warning
   now sits at the top of `commands/registry.py`;
   deleting the package (module + YAML + two test files) is a scope call left
   to the owner, and it is a plain `git revert` away either direction.

7. **`ensure_user_access` was never ported — six gates, one of them a real
   product behaviour.** Legacy funnels almost every command through
   `ensure_user_access` (`bot.py:1609-1717`); `grep -c` counts **120 call
   sites**. Nothing in `src/` implements it, and nothing decides against it
   either: the only trace is ~15 handler docstrings each noting the gap for
   their own command (`handlers/referrals.py:41`, `send.py:15`, `daily.py:13`,
   `cancel.py:22`, `commission.py:23`, `ads.py:10`, `group_pay.py:6`,
   `relations.py:11,193`, …). No `docs/*.md`, `CUTOVER.md` or
   `LOST_FEATURES_BACKLOG.md` entry covers it — the GAP-2 sweep enumerated
   *features*, and this is a cross-cutting gate, so it fell between the
domains. Exactly one of the six was ported: the **chat-type
   requirement** (`require_group` / `require_private`), and it was ported
   in the right shape — per-handler aiogram filters, rather than a
   function every handler has to remember to call. The other five were
   membership checks and runtime kill-switches of the kind legacy kept in
   mutable settings; which of them this package should grow back, and in
   what form, is deliberately not decided here.

   **Why they are not simply reinstated.** Two of the five are product
   decisions rather than parity bugs. One governs whether joining the
   group is a precondition for using the bot at all — and therefore
   whether the welcome balance is handed out before anyone joins. The
   other is an operational lever: a runtime way to take a single surface
   offline, instead of stopping the whole unit, when a money bug shows
   up in one of them. Both change user-visible behaviour, both are cheap
   to build (`CHAT_ID` is already in `Settings` at
   `config/settings.py:100`, and the port already calls `get_chat_member`
   elsewhere), and **neither is started without an explicit go-ahead.**
   The remaining three were configured off for the whole of the legacy
   bot's life, so no behaviour regressed when they were dropped; what
   was lost is the lever, not the rule.

   The transferable lesson is the one worth keeping: a
   feature-by-feature migration sweep finds features. A gate that
   *every* feature passes through belongs to no feature, so it is
   invisible to that sweep and has to be looked for on purpose.

## B. Executable locally now

**This section is empty — every CMD-* port listed here has shipped.** Each one
went out as: faithful port → bilingual i18n (ru+en, enforced by the I18N-2
convergence guard) → tests → ruff+mypy+full-suite green → one commit, pushed to
`origin/main` no-force. Verified against the tree on 2026-08-19; each command
below has a handler, a `registry.yaml` entry (so it appears in `/help` and on
`/commands`), and a `core/ranks.py` gate.

- ~~**CMD-1** — `/quote` + `/joke` (SFW).~~ DONE, and superseded by RR-6
  #71/#72: `/joke` now prefers the online source (`JokeService`) and
  `/quote` is AI-generated again, both with the ru+en pools as fallback and
  both answering in groups. See `docs/RICHNESS_REGRESSIONS.md`.
- ~~**CMD-2** — `/topactive` + `/chatinfo`.~~ DONE — `handlers/chatstats.py`
  owns the whole family (`/chatstats`, `/cstats`, `/chatinfo`, `/top_activity`,
  `/topactive`); `/chatinfo` is wired as a text alias of `/chatstats` in
  `middlewares/text_alias.py`.
- ~~**CMD-3** — `/currency` + `/crypto`.~~ DONE — `handlers/currency.py`,
  reusing `CurrencyService` as planned.
- ~~**CMD-4** — `/forecast` + `/city`.~~ DONE — `/forecast` in
  `handlers/weather.py` (RR-6 #68/#69), `/city` in `handlers/city.py` (RR-6
  #74, with the saved-city store).
- ~~**DEPLOY-PREP** — the deploy runbook.~~ DONE — `docs/DEPLOY.md`, rewritten
  in #146 for the live `tgbot.delabs.space` topology and extended in #143 with
  the owner's what-to-do-on-a-refund-alert runbook.

New work is tracked as numbered audit items rather than being appended here;
open ones live in section A (owner-blocked) or in the task list.

## C. Out of scope / intentionally deferred

- ~39 owner/dev-only commands (`/sql`, `/broadcast`, `/maintenance`, `/deploy`,
  `/reload`, smoke-test commands…) — intentionally stay legacy/dev-only.
- Admin-panel chrome localization (dev-only Russian text) — only developers see
  it; low value, not a public-UI convergence bug.
- ~~FSM SQLite-storage (currently MemoryStorage) — optional, "only if persistence
  is needed" per the original plan.~~ NO LONGER DEFERRED — persistence turned
  out to be needed after all: a restart mid-`/cpc` (or mid-anything with an FSM
  step) dropped the flow and the user's coins with it. `fsm/sqlite_storage.py`
  ships the backend, `FSM_BACKEND` selects it (default still `memory`, and a
  typo like `sqllite` is refused rather than silently falling back), and prod
  runs `FSM_BACKEND=sqlite`.
- ~~**ru/en parity for the non-slash "classic" command surface** (I18N-3, LATER —
  flagged by the user).~~ DONE (#171). Most of the English trigger words had
  already landed alongside their Russian counterparts as commands were ported;
  what was left was the tail. `/city` had no latin trigger at all and
  `/chatinfo` reached English only through the phrase fold — which the site's
  per-command trigger list cannot see, so its row advertised Cyrillic to an
  English reader. Both now have one, and
  `test_every_plain_command_advertises_a_latin_trigger` fails the build if a
  future command ships Russian-only. The AI trigger was the other half:
  `extract_ai_direct_question` accepted `ai` as a *bare word only*, so
  «ии что такое X» answered and "ai what is X" did not. It is now a prefix like
  the other two, with one deliberate asymmetry — in a group the latin form
  needs a comma or colon ("ai, what is X"), because "AI" opens English
  sentences constantly and the space form would have the bot answering
  conversations it was not part of. The slash-command surface and the i18n YAML
  were already converged (guarded by `test_ru_en_convergence`).
- ~~`/city` saved-default-city + `/currency` display-currency SETTER — deferred
  (need a per-user storage column + migration; legacy stored saved cities in a
  file, not the DB).~~ DONE — the migration was written and both setters
  shipped: `handlers/city.py` (RR-6 #74) and `handlers/currency.py` (RR-6
  #66/#67, which also taught `/rate` to read the saved currency).

## D. Unregistered / half-ported features (GAP-2 find-all audit)

A whole-project scan (1889 i18n keys; 1326 unreferenced in code) + per-cluster
triage surfaced features whose strings/models existed but whose **handler was
not registered**. Classified into: genuinely-missing USER-FACING, admin/dev-
deferred (intentional), and legacy-noise (already ported under `h_*`).

**The user-facing list is empty — all five have shipped** (verified against the
tree on 2026-08-20; see D.1). What remains below is the two "not a gap"
classifications, kept because they explain why a reader who greps for
`perm_*` or `cpc_*` keys and finds no handler should *not* file a bug.

The hole this section was created to track is now guarded mechanically rather
than by periodic hand-sweeps: `tests/regression/test_router_wiring.py` asserts
that every module building an aiogram router is imported by `main_router.py`,
and that every factory it imports is actually passed to `include_router` —
importing without including is dead in exactly the same way, and ruff cannot
see it because the alias *is* referenced, by its own import statement.

### D.1 — Formerly-unregistered USER-FACING features: ALL SHIPPED

Listed in the original port order, with where each one actually lives now.

1. ~~**Romance RP-actions** (`FEAT-RP`) — `rel_rp_*` (61 i18n keys).~~ DONE —
   `handlers/rp.py`, shipped as RR-5 #49/#51/#54/#58: reply-to-partner actions
   in a group, pair XP off `bonds_repo.RELATIONSHIP_LEVEL_XP`, per-level
   command unlocks and the `/rp_commands` discovery list.
2. ~~**Couple joint activities** (`FEAT-COUPLE`) — `rel_activity_*` (14).~~
   DONE — `handlers/couple_activities.py` (with `marriage.py` / `relations.py`
   carrying the entry points). Its spend path was itself audited later: #63
   put the coin movements into the transaction registry, and #153 removed the
   `rel_activity_footer_hint` copy that advertised a syntax the handler never
   accepted.
3. ~~**Check subscription-gate, claim side** (`FEAT-CHECKSUB`) — `check_sub_*`
   (4).~~ DONE — `keyboards/builders/checks.py` owns the `check_sub` prefix and
   `handlers/checks.py:handle_check_sub_verify` the callback. The gate is
   enforced claim-side, which is the half that matters: a creator who sets
   `required_subscription` now actually gets it honoured instead of decorative.
4. ~~**Group onboarding / welcome** (`FEAT-WELCOME`) — `bot_added_*`,
   `group_welcome_*`.~~ DONE — `handlers/group_events.py` handles both
   `my_chat_member` and `new_chat_members`; it is also what #110 (supergroup
   migration) and #111 (`bot_groups` add/remove bookkeeping) hang off.
5. ~~**P2P COM exchange / marketplace** (`FEAT-P2P`) — `p2p_*`,
   `dispute_resolved_*`, `crypto_payment_*` (~25).~~ DONE — `handlers/p2p.py`
   plus `handlers/p2p_trade.py` (order book, express-buy, my orders/trades,
   "I paid", disputes), `handlers/topup.py` for the crypto leg. The hardening
   passes that followed are the interesting part of its history: #65 (min/max
   limits displayed but not enforced), #81 and #103 (`1e400` / `nan` crashing
   order creation and the payment step), #97 (order number rendered as a
   placeholder), #104 (a buyer freezing the whole book with free open trades).

### D.2 — Admin/dev-only, intentionally deferred (not a gap)

Roles/permissions (`perm_*`, `rank_*`, `role_title`, `group_role[s]_*`), admin
panels (`admin_*`, `economy_give/stats`, `marry_admin`, `mod_stats`), group ops
(`group_pay`, `group_modes`, `filter_action`, `category_unavailable`,
`constructor_mod`), watermark/preview (`watermark_*`), logs/backups
(`logs_backups`, `backup_restore`, `log_action`), `test_mode`, `admin_password`.
Operator tooling; most already listed in `_INTENTIONALLY_LEGACY`.

### D.3 — Legacy noise / already ported under `h_*` (not a gap)

`cpc_*` → RPS ported (`h_rps_*`); `buy_item_*` → shop (`h_buy_*`/`h_shop_*`);
marriage second-tier (`marry_extend/auto/other/history` — in `_INTENTIONALLY_LEGACY`);
display labels (`cat_name`, `rel_status`, `profile_activity`, `marriage_cat`) →
superseded by `h_*`; `cmd_kom_*` (37) → `kom_*` aliases.

## Done this session (beyond the original stage list)

**#176 — английские страницы сайта печатали русские слова. ЗАКРЫТО.**
Каталог хранит оба написания каждого триггера рядом — «баланс» и
`balance` доходят до одного хендлера, — а страница печатала список
целиком, независимо от языка. На `/commands/en` это давало около 200
кириллических токенов: `мои_обращения`, `выплата_из_казны`, `дейли`,
«кто», «чат», «инфа». Читателю английской страницы выдавали слово,
которое он не прочитает, не наберёт и не найдёт поиском.

Правка односторонняя и намеренно такая. `readable_in(lang, tokens)`
отбрасывает кириллицу только для `en`; русская страница по-прежнему
показывает и `balance`, и «баланс», потому что русскому читателю
латиница доступна, а обратное неверно. Фильтр стоит на алиасах, на
алиасах подкоманд, на «бесслэшевых» триггерах и на всех трёх рядах
плашки внизу страницы — включая пример обращения к ИИ, единственную
строку, которую предлагается скопировать дословно. Заодно чистится
`data-search`: поиск по странице больше не находит то, чего на ней нет.

Вычистить — половина дела: у 29 команд английского написания просто не
существовало, и фильтр оставил бы английскую страницу с пустыми
строками. Поэтому каждая такая команда получила латинский алиас, у
длинных — сокращение (`wd` для «вывод», `fees`, `warns`, `f_add`,
`w_on`, `m_accept`, `gpay`, `giverights`). Алиасы добавлены и в каталог,
и в сам хендлер — `test_every_advertised_alias_is_registered` не даст
разъехаться. Две ветки в `marriage.py` разбирают команду по слову, и
там короткие формы пришлось внести в набор явно, иначе `/m_accept`
делал бы ровно противоположное. Последним закрыт `GROUP_PREFIXES`: «бот »
работал, `bot ` — нет, хотя это единственная дверь к алиасу, которого
нет в списке бесслэшевых фраз.

Два комментария в CSS содержали кириллицу и попадали в инлайн-стиль на
каждой странице — переписаны (хэши CSP считаются от готового тела, так
что правка безопасна). Осталось намеренно: бренд «ком17» — это имя,
выбранное владельцем, оно же стоит на юр. страницах и совпадает с
`@kom17bot`. Регрессия тройная: `/commands/en` не должна содержать ни
одного кириллического слова, `/commands` — обязана содержать оба
написания, и ни одна запись каталога (вместе с подкомандами) не имеет
права остаться без латинского написания. Последнее — то, что делает
фильтр безопасным: команда, приехавшая только по-русски, на английской
странице потеряла бы все свои триггеры разом, а не просто выглядела бы
не так.

Живая проверка прода после выката нашла вторую половину дефекта: под
сгенерированным индексом на той же странице лежит рукописный гайд
`telegraph_guide_en.md`, через который ни один фильтр не проходит. Он
предлагал `/кнб`, `/очистить`, `/репорт` и `/вывод` как альтернативы к
их же каноническим написаниям. Сканер мёртвых имён молчал — это живые,
зарегистрированные триггеры; они просто нечитаемы для того, кому
адресована страница. `/вывод` заменён на уже существующий `/wd`,
`/репорт` убран (рядом стоял `/kom_report`), а для двух оставшихся
английского аналога не было вовсе: добавлены `/rps` (та же игра под
именем, которое читатель знает) и `/purge` — слово, которым сам гайд и
описывал `/clear`. Оба заведены и в каталоге, и в хендлере. Гарантия
живёт в `tests/regression/test_copy_command_references.py`, где как раз
и записано, что латинский паттерн кириллицу не видит: английский гайд
обязан быть без кириллицы, русский — обязан её содержать, иначе первая
проверка сторожила бы не тот файл.

**#177 — деплой не отправлял файлы, из которых собирается сайт.
ЗАКРЫТО.** Найдено ровно там, где кончается #176: коммит с починенным
английским гайдом уехал, метка сборки на проде обновилась, журнал
чистый — а страница по-прежнему предлагала `/кнб` и `/очистить`. Файл на
сервере оказался от 13 августа. `scripts/deploy.sh` синхронизировал три
дерева (`src/telegram_invite_bot`, `migrations`, `tests`), а сайт читает
рукописные гайды из `GuideSiteSettings.sources_dir`, и по умолчанию это
`Path(".")` — рабочий каталог юнита, то есть корень выкатки. В git оба
файла лежат в корне репозитория, вне всех трёх деревьев, поэтому каждый
деплой оставлял их такими, какими их когда-то положил ручной rsync.

Отказ здесь тихий в самом неудобном смысле: деплой рапортует успех,
`/admin_status` показывает новую ревизию, тесты зелёные, файл в git
правильный — и посетитель читает страницу недельной давности. Комментарий
в `config/settings.py` уже утверждал, что «деплой кладёт оба файла туда»;
теперь это правда, а не намерение. Корневые файлы едут отдельным rsync
без `--delete`: цель — сам `$APP_DIR`, где рядом лежат `.env`,
`BUILD_INFO` и `.venv`, и общий флаг стёр бы конфигурацию вместе с
окружением. Гарантия в `tests/regression/test_deploy_ships_runtime_files.py`
берёт имена файлов из `webhook/server.py`, а не из литерала рядом, так что
переименование гайда или третий такой файл ломают проверку так же, как
сломало бы забытое имя.

**#178 — гайды печатали алиасы без слеша, а бот их без слеша не принимает.
ЗАКРЫТО.** Строка вида `- **/relationship** (или **rel**, **отношения**)`
читается однозначно: канон со слешем, альтернативы — как написано. Для
одной из двух это правда — `отношения` есть в `_ALIAS_MAP`, и мидлварь
`text_alias` подхватывает слово в личке. `rel` в карте нет, и набранный
ровно так, как напечатано, он не делает ничего и нигде. Два слова в одной
скобке, оформлены одинаково, ведут себя по-разному — отличить их читатель
не может.

Бесслешевая диспетчеризация заметно уже таблицы алиасов: в личке слово
разрешается только через `_ALIAS_MAP`, в группе — через короткий белый
список `BARE_GROUP_PHRASES` (`меню`, `команды`, `пинг`, `бот` и ещё
несколько). Всё остальное требует слеша. Таких мест нашлось одиннадцать —
семь в русском гайде (`alive` дважды, `настройки`, `язык`, `брак_статус`,
`rel`, `отношения_список`) и четыре в английском (`language`,
`my_marriage`, `rel`, `alive`). Слеш проставлен везде, включая
`отношения`: правило «без слеша тоже можно» уже объяснено отдельно и
целиком — RU:161 и EN:124, вместе с префиксами `бот `, `.`, `?`.

Сгенерированная половина страницы `/commands` эту разницу знала всегда:
чипы алиасов рендерятся как `/alias`, а бесслешевые слова уходят в
отдельную строку `cmd-plain` с подписью «без слеша» — там даже
комментарий стоит о том, что одинаковая форма подсказала бы лишний слеш.
Правка приводит рукописную половину к той же конвенции.

Гарантия — `test_guides_print_aliases_the_reader_can_actually_type` в
`tests/regression/test_copy_command_references.py`. Она не требует слеша
всегда: слово проходит, если мидлварь действительно взяла бы его голым,
поэтому `бот баланс` и `.дейли` остаются легальными. Исключены единицы
длительности `m/h/d/w/s` из строк `/mute` и `/ban` — `h` по совпадению
ещё и алиас `/help`, и это единственная причина, по которой список
исключений вообще существует.

**#174 — тревога о возврате не называла пользователя, хотя бот его знал.
ЗАКРЫТО.** `processed_webhooks` с самого начала хранит
(провайдер, идентификатор платежа) → пользователь + выданные монеты, а
RollyPay и ЮKassa присылают возврат под тем же идентификатором, под
которым зачисляли. То есть один SELECT по первичному ключу отделял
владельца от факта, ради которого он и открывает кабинет провайдера.
Письмо же говорило только «какой-то платёж вернули» — и решение о
списании монет принималось вслепую.

Теперь путь тревоги сначала поднимает строку зачисления и называет
пользователя, сумму и дату начисления. Когда не поднимает — пишет
честно, почему: либо зачисления под этим идентификатором нет, либо
провайдер нумерует возврат иначе, чем оплату. Stripe намеренно во
второй категории (`session_id` при оплате, `payment_intent`/`charge`
при возврате): сказать про него «начисления нет» было бы враньём про
деньги, которые бот вполне мог выдать.

Вторая половина — durability: миграция `0014_webhook_reversal` добавляет
`reversed_at` / `reversed_event`, и факт возврата переживает
перезапуск. Из этого сразу выпадает повторная доставка — та же строка
уже помечена, и письмо это говорит. Поиск обёрнут так, что любая его
поломка (залоченная база, схема до `0014`) гасится в «не определён»:
тревога с одним идентификатором всё ещё несравнимо лучше, чем
отсутствие тревоги, и на это есть отдельный регрессионный тест.
Монеты по-прежнему не списываются автоматически — это решение
человека, и оно им остаётся.

**#172 — карточка `/admin_withdrawals` печатала номер карты целиком. ЗАКРЫТО.**
`_truncate` резал `payment_details` по 20 символам, а PAN — 16, поэтому
каждая карта, введённая в легаси-форму `/withdraw`, уходила владельцу
в Telegram полностью и оставалась в истории чата, в бэкапах Telegram и
в любом пересланном скриншоте. Роутер private-only, так что утечка
начиналась на владельце, а не заканчивалась на нём. Теперь `_mask_pan`
ищет карточную последовательность цифр (12–19, пробелы и дефисы
допустимы) в любом месте строки и заменяет её на BIN + точки +
последние 4 — окружающий текст («Сбербанк …») сохраняется, потому что
легаси-строки именно такие. Телефон СБП (11 цифр) и криптоадрес не
трогаются: это платёжные инструменты, которые оператор читает с
карточки, и они не секрет в том смысле, в каком секретна карта.
Живой путь выплат — только Crypto Pay, `WithdrawalsRepo.create` вообще
не пишет `payment_details`, так что операционно не потеряно ничего.

Вторая половина — данные: затронутая строка оказалась тестовой.
Номер вычищен в NULL, в `admin_note` записано, что это легаси-тест.
**Статус строки НЕ трогали** — отклонять ли заявку и возвращать ли
монету, решает владелец. Перед правкой снят локальный бэкап БД.


**#179 — страница `/commands` не вела ни на юридические документы, ни на
форму обращения. ЗАКРЫТО.** Сайт — это пять видов страниц, которые
собирали четыре пакета, и каждый строил свою навигационную строку. Они
расходились: `/` и `/commands` показывали гайд, документы и форма — нет;
документы перечисляли только друг друга. А `/commands` — страница, на
которую `/help` и `/faq` отправляют вообще всех, то есть самая
посещаемая на сайте, — не показывала строку совсем: наружу с неё вели
только бот, логотип и переключатель RU/EN. Со страницы, куда попадает
большинство, политика конфиденциальности и форма обращения были
недостижимы.

Это важно не из-за аккуратности. Документы существуют потому, что
эквайер требует их постоянной доступности, а «доступность» — свойство
всего сайта, а не трёх страниц, которые случайно ссылаются друг на
друга.

Строку теперь строит один модуль — `cms/nav.py`. Он берёт одни и те же
аргументы от всех вызывающих, и страница решает единственное: какой
пункт её собственный. Попутно вскрылся настоящий цикл импортов:
`cms/legal/__init__.py` ре-экспортировал роутер, поэтому импорт
*таблицы документов* тянул за собой *роутер*, а тот — обратно `cms.nav`.
Пакет сведён к пустому пространству имён, `doc_path` переехал в
`cms/paths.py` — модуль без единого внутреннего импорта, — и порядок
зависимостей стал однонаправленным на деле, а не на бумаге.

Гарантия — `tests/regression/test_site_navigation.py`: рендерит все пять
видов страниц на обоих языках и требует от каждой один и тот же набор
ссылок, ровно одну отметку `aria-current` и отсутствие ссылок на другой
язык. Последняя проверка сразу нашла свою первую ошибку: логотип на
`/en` вёл на русскую главную — единственная страница, где он был
захардкожен на `ru`. Проверка языка намеренно смотрит на всю страницу, а
не только на строку навигации, иначе бы этого не увидела; вырезается
только пара RU/EN, для которой переход между языками и есть работа.

#180: `guide_enabled` жил в двух местах — параметром и в контексте — ЗАКРЫТО.

Продолжение #179, на уровень ниже. Строку навигации свели к одному
источнику, но сам флаг «гид смонтирован» остался раздвоенным: главная
страница получала его отдельным аргументом `build_router(ctx,
guide_enabled=...)`, а юридические страницы и форма обращения читали
одноимённое поле того же самого `LegalContext`. Ничто не заставляло эти
два значения совпадать. Последствие — ровно тот класс расхождения, что
убрал #179: `/` могла спрятать `/commands` из своего текста и своей
строки навигации, пока `/privacy` и `/contact` продолжали её
рекламировать. Собственный докстринг `build_router` при этом уже
говорил, что вторая копия значения — это второе место, где оно может
разойтись.

Флаг убран из сигнатуры: `_body_md`, `render_home` и `build_router`
читают `ctx.guide_enabled`. Вызов в `webhook/server.py` и тесты,
передававшие ключевое слово, переведены на контекст. В хелпере тестов
`_client` параметр удалён целиком, а не оставлен рядом с `ctx`: он бы
молча игнорировался, когда контекст передан явно.

Гарантия из #179 расширена на выключенные переключатели: `_pages` и
`_expected_links` теперь умеют рендерить случай «гид выключен» (страница
`/commands` при этом не монтируется, поэтому её нет и среди страниц), и
добавлены две проверки — ни одна страница не ссылается на выключенный
гид, и при обоих выключенных переключателях строка навигации состоит из
главной и трёх документов, ровно.

Мутационная проверка нашла в самой гарантии дыру: ссылки в строке
навигации абсолютные (их копируют в форму эквайера), а в тексте страниц
намеренно относительные (чтобы читателя не выбрасывало во вторую
вкладку). Проверка знала только первую форму, поэтому мутация «текст
игнорирует флаг» проходила мимо неё. Обе проверки — и про гид, и про
форму обращения — теперь смотрят обе формы ссылки.


#181: главная перечисляла три документа и молчала о форме обращения — ЗАКРЫТО.

Раздел «Документы» на главной вёл на политику конфиденциальности,
пользовательское соглашение и поддержку. Формы обращения в нём не было:
на неё ссылалась только строка навигации.

Собственная копия формы называет свою аудиторию прямым текстом — запрос
от банка-эквайера или регулятора, вопрос о персональных данных,
досудебная претензия, сообщение об уязвимости. Это ровно тот читатель,
ради которого написана главная: он обрезает юридическую ссылку до
голого домена и ищет, куда писать. Единственный список на странице,
озаглавленный «Документы», прятал от него именно тот адрес, за которым
он пришёл. Строка навигации доносит ссылку только до того, кто читает
обрамление страницы, а не её текст.

Сделано пятым токеном SLOT_CONTACT и отдельным полем copy.contact_md —
одним пунктом списка, а не разделом. Пункт приклеивается к docs_md
одним переводом строки (пустая строка опубликовала бы его отдельным
списком) и только при ctx.contact_enabled, тем же шаблоном, что и
commands_md для гида: страница не обещает адрес, который отдаёт 404.

Комментарий над токенами утверждал, что главная «ссылается на все
остальные страницы». Пока пятого токена не было, это было неправдой —
теперь по токену на страницу, и это же свойство держит утверждение
верным дальше.

Мутационная проверка: пункт без условия роняет гарантию #179/#180
(«ни одна страница не рекламирует невключённую форму») в обеих
языковых версиях; пункт, убранный совсем, роняет новую проверку
tests/unit/cms/home — она смотрит относительную форму ссылки, потому
что абсолютная прошла бы на одной строке навигации и ничего не сказала
бы о самом списке.


#182: у сайта не было HTML-страницы 404 — промах отдавал JSON-блоб. ЗАКРЫТО.

Находка на живом проде. Любой неверный адрес — `/en/privacy`, `/commands/ru`,
опечатка в `/priacy` — отвечал `{"detail":"Not Found"}` с типом
`application/json`: ни строки навигации, ни имени сайта, ни пути обратно.
Промахи не выдуманные: английские документы живут на `/privacy/en`, поэтому
`/en/privacy` угадывается первым, а существующий `/commands/en` делает
правдоподобным и `/commands/ru`. Тот же класс, что #130 (голый корень отдавал
404) и #131 (405 на HEAD).

Сделано: новый модуль `cms/notfound.py` — четыре строки копии на язык и
обработчик исключения. Решения, каждое записано в docstring рядом с кодом:

— Обработчик, а не роут. 404 — это то, что говорит таблица маршрутов, когда
  не совпало ничего; роут-заглушка `/{path:path}` мог бы затенить настоящую
  страницу. Зарегистрирован на класс `StarletteHTTPException` и внутри
  проверяет `status_code == 404`, чтобы 405 (#131) и 403 у `/metrics`
  продолжали отвечать как раньше.
— Согласование по `Accept`. То же приложение обслуживает вебхук Telegram и
  три платёжных колбэка. Они настраиваются по URL, так что их 404 — это
  ошибка конфигурации, и помогает тут машиночитаемый ответ, который их
  инструменты уже пишут в лог. HTML отдаётся только тому, кто явно назвал
  `text/html`; `*/*` и `application/json` получают прежний JSON. Обычный
  `curl` шлёт `*/*` — и правильно получает JSON.
— Язык: сначала путь, потом заголовок. Путь — свидетельство об этом запросе,
  заголовок — постоянная настройка. Сегмент должен целиком равняться `en`:
  проверка по префиксу приняла бы `/enterprise` за английский. Считаются оба
  порядка — и `/privacy/en`, и зеркальная догадка `/en/privacy`. Из заголовка
  берётся только первый язык: русский браузер почти всегда перечисляет `en`
  запасным вариантом, и «просто упоминание» отдало бы английскую страницу
  большей части аудитории.
— `Cache-Control: no-store`: промах не должен осесть на edge и пережить
  появление настоящей страницы по этому адресу.
— Готовая страница кэшируется (`lru_cache` на восемь записей). 404 — это
  единственный ответ, который случайный незнакомец может запрашивать без
  ограничений, а сканеры делают это постоянно; рендерить markdown и считать
  sha256 по семнадцати килобайтам на каждый промах значило бы делать эту
  работу в том же event loop, который отвечает вебхуку Telegram. Ключ —
  контекст и язык, а не один язык: иначе второе приложение в том же процессе
  отдало бы строку навигации первого. Мутация «ключ только по языку» роняет
  отдельную проверку.
— Своя политика `csp_for_html(page)`. Запасная политика мидлвари —
  `style-src 'none'`; без собственного хеша страница приехала бы голым
  текстом, то есть выглядела бы ровно как тот сломанный сайт, который
  читатель и решил, что нашёл.
— `current=None` в строке навигации: этой страницы в строке нет, и ни один
  пункт не должен объявлять себя текущим — иначе скринридер сообщит читателю,
  что он на странице, где он не находится, а стиль погасит как раз ту ссылку,
  которая ему нужна.

Мутационная проверка, восемь правок, все пойманы: `wants_html` всегда True
(6 падений, включая «обычный клиент получает JSON»); всегда False (6);
проверка языка по префиксу (4, в том числе `/en-gb/privacy`); «просто
упоминание en» в заголовке (1); `current=nav.HOME` вместо `None` (2 — после
того, как срез был уточнён до `<nav class="docnav">`: первый `<nav>` на
странице языковой и не помечает ничего, поэтому срез по первому тегу
пропускал мутацию); снятый заголовок CSP (1); снятый `Cache-Control` (1);
обработка любого статуса, а не только 404 (1 — падает гарантия #131).

Плюс гарантия подключения (урок #160): интеграционный тест поднимает настоящее
приложение из `create_app` и проверяет обе половины — читатель получает
страницу с собственной политикой CSP, программа получает прежний JSON. Снятый
вызов `notfound.install` роняет именно его.


A-07…A-12 (achievements card + awarding, chatstats, rate/convert, roulette,
games-table write-side, luck-coin inventory), SEC-1/2/3 + BUG-1/2 (three
adversarial audit loops), I18N-1/2 (ru/en convergence + guard test).


#183: у сайта не было favicon — вкладка, закладка и превью ссылки пустые. ЗАКРЫТО.

Находка на живом проде. `/favicon.ico` отвечал 404, а в `<head>` ни одной
страницы не было `<link rel="icon">` — то есть браузер честно спрашивал и
честно ничего не получал. Двенадцать страниц сайта плюс 404 показывались во
вкладке безымянным листом бумаги: рядом с любой другой открытой вкладкой это
читается как «сайт недоделан», и ровно этот сайт эквайер видит рядом с офертой
и политикой. Признак того, что пробел был замечен раньше кода: `cms/csp.py`
уже держал `img-src 'self' data:` и в докстринге объяснял это «для favicon,
который браузер просит сам» — дыра в политике была прорезана под иконку,
которой не существовало.

Сделано: две константы в `cms/guide_site/rendering.py` и по строке в оба
`<head>`. Решения, каждое записано в docstring рядом с кодом:

— Монета перерисована, а не переиспользована. Встроенный `_COIN_SVG` рядом с
  словесным знаком — тонкое кольцо в `currentColor`: на своём месте верно, но
  на вкладке шириной шестнадцать пикселей обводка уходит под один пиксель, и
  наследовать цвет текста не у чего. Поэтому вкладка получает монету заливкой,
  называющую свои цвета сама — золото сайта на фоне сайта.
— `data:`-URI, а не файл. Файл означал бы маршрут, который надо смонтировать,
  закэшировать и покрыть тестами ради трёхсот неизменных байт — на сайте,
  построенном как один запрос на страницу. Дыра в CSP под это и была прорезана.
— Кодируются ровно три символа: `#` без экранирования начинает фрагмент URI и
  отрезал бы оба цвета, `<` и `>` — чтобы ничто, читающее атрибут менее
  внимательно, чем парсер, не приняло его за разметку. Внутри SVG кавычки
  одинарные, поэтому двойным кавычкам атрибута экранирование не нужно.

Тест — новый `tests/unit/cms/test_chrome.py`, и он про место, а не про иконку.
Пять пакетов рендерят страницы — главная, три документа, форма обращения,
справочник команд и 404 — и все проходят через один из двух сборщиков оболочки.
Значит, оболочка — единственное место, где «на каждой странице есть вот это»
можно сделать правдой, а этот файл — единственное, где это можно проверить:
тест, живущий рядом с одним роутером, доказывает страницу этого роутера и
молчит про остальные четыре. Фикстура рендерит по странице каждого вида в обоих
языках (десять штук, включая справочник — единственного пользователя второго
сборщика), и каждая проверка утверждает сразу по всем. Шестой вид страницы,
добавленный позже без оболочки, упадёт здесь. Ссылка вынимается регулярным
выражением из готовой страницы, а не импортируется из рендерера: тесты должны
описывать то, что получает браузер, а не пересказывать константу.

Мутационная проверка, семь правок, все пойманы: иконка убрана из doc-оболочки
(4 падения); из guide-оболочки (4); снято кодирование `#` (1 — цвета
обрезаются); у SVG отобран `xmlns` (1 — перестаёт разбираться как SVG); ссылка
уехала под `<body>` (1); `img-src` в `csp.py` потерял `data:` (1); монета
закрашена цветом фона (1).


#185: RU/EN-двойники не объявляли ни hreflang, ни canonical. ЗАКРЫТО.

Находка на живом проде. Каждая страница сайта существует дважды — по-русски и
по-английски, по двум разным адресам, — и в разметке про это не говорилось
ничего. Языковая строка в шапке — обычный `<a>`: краулер, читая её, узнаёт, что
две страницы ссылаются друг на друга, а не что это один документ на двух
языках. Последствия обычные — версии конкурируют между собой вместо того, чтобы
складывать вес, а читателю, ищущему по-английски, может достаться русская
страница, — и приходятся они на поверхность, которую читает эквайер. Данные при
этом уже были на руках: все пять роутеров передают в оболочку абсолютные и
экранированные `url_ru`/`url_en`, так что правка свелась к тем же двум `<head>`,
что и #183.

Сделано: `_head_links` в `cms/guide_site/rendering.py` и обязательный
именованный аргумент `canonical` у обоих сборщиков оболочки. Решения:

— `canonical` обязателен, без значения по умолчанию. Умолчание означало бы, что
  шестой вид страницы, добавленный позже, молча получит чужой ответ на вопрос,
  которого ему не задали.
— Страница объявляет каноническим свой собственный адрес — тот из пары, что
  соответствует её языку. Канонический адрес, называющий ЧУЖУЮ страницу, хуже
  отсутствующего: он просит выкинуть эту страницу в пользу той. Поэтому тест
  проверяет адрес, а не наличие тега.
— Объявление взаимно: обе половины документа перечисляют одну и ту же пару.
  Односторонняя ссылка игнорируется целиком, поэтому проверка сформулирована
  как «русская страница называет английскую своим двойником, и английская
  говорит ровно то же самое».
— `x-default` — русская страница. Он называет версию для читателя, чей язык не
  совпал ни с одним, а это бот, у которого весь интерфейс, каталог и поддержка
  русские: отправить такого читателя в английский перевод было бы ложью,
  которая выглядит дружелюбнее правды. Оба конкретных варианта при этом
  объявлены, так что читатель, который английский всё-таки просит, будет
  сопоставлен с `hreflang="en"` раньше, чем дело дойдёт до умолчания.
— У страницы 404 — `canonical=False`, и она единственная причина, по которой
  выключатель вообще существует. Языковая строка у неё есть, значит есть и
  `url_ru`/`url_en`, но они ведут на главную, а не на этот адрес на другом
  языке: адреса не существует ни в одном. Пропустить эту пару в `hreflang`
  значило бы объявить главную английским переводом извинения, а
  само-канонический адрес был бы хуже — он приглашает проиндексировать URL,
  который никогда ничего не отдаст.
— Абсолютность наследуется от `cms.paths.absolute`, которая при ненастроенном
  origin отдаёт относительную форму. Относительный само-канонический адрес
  разрешается в адрес самой страницы, то есть говорит ровно то же, что сказал
  бы абсолютный: запасной вариант вырождается в пустую операцию, а не в ложное
  утверждение.

Мутационная проверка, восемь правок, все пойманы: ссылки убраны из doc-оболочки
(4 падения); из guide-оболочки (4); канонический адрес перестал смотреть на
язык (1 — английские страницы объявляют себя русскими); `x-default` переведён
на английскую страницу (2); английский двойник указывает на русскую страницу
(1); оставлен только canonical без пары (1); у 404 включён `canonical=True`
(1); ссылки действительно уехали под `<body>` (1 — падает ровно проверка
положения).

#184: сайт не отдавал ни своего robots.txt, ни sitemap.xml. ЗАКРЫТО.

Находка на живом проде. `GET /robots.txt` возвращал 200, и это сбивало с
толку: отвечал не бот, а Cloudflare — managed-блок про content signals,
1248 байт сплошных комментариев, ни одной строки `User-agent`, `Allow`,
`Disallow` или `Sitemap`. `GET /sitemap.xml` отдавал 404. То есть два первых
запроса, которые делает любой краулер, приходили в пустоту, и сайт из
двенадцати страниц (шесть документов на двух языках) оставлял поиск
разбираться самостоятельно — сразу после того, как #185 научил эти страницы
объявлять свои канонические адреса и языковые пары.

Сделано: `cms/discovery.py` — два построителя и роутер, смонтированный в
`webhook/server.py` последним из сайтовых, прямо перед установкой страницы
404. Решения:

— Роутер монтируется последним не для красоты, а потому что он единственный,
  кому нужно знать решения остальных. Карта сайта перечисляет ровно те
  страницы, которые эта поставка отдаёт, и читает те же два флага
  (`guide_enabled`, `contact_enabled`), под которыми смонтированы их роутеры.
  Запись про страницу, которой нет, — это машиночитаемое заявление о
  собственной поломке: краулер уходит на страницу извинения.
— Никакого перечисления `Disallow` для вебхуков, служебных и
  редактирующих адресов. Правка выглядит как ужесточение и является его
  противоположностью: краулеры ходят только GET-ом и до POST-эндпоинтов не
  добрались бы в любом случае, так что запрет не защищает ничего, — а файл
  публичный, и список выдал бы постороннему готовую карту записывающих
  адресов. На это есть отдельный тест, потому что правка правдоподобная и
  сделана будет из лучших побуждений.
— Без origin (`WEBHOOK_URL` пуст — режим polling и весь набор тестов) маршрут
  карты не монтируется вовсе, а из robots уходит строка `Sitemap:`.
  `<loc>` обязан быть абсолютным, и относительная карта отбрасывается
  целиком; лучше молчать, чем отдать файл, который любой читатель выкинет.
— Ни `<lastmod>`, ни `<priority>`, ни `<changefreq>`. Дата изменения, которую
  никто не обновляет, через месяц становится ложью, и поисковики её
  игнорируют именно поэтому. Приоритеты между шестью страницами одного сайта
  не значат ничего.
— Каждая запись повторяет ту же пару языков, что страница объявляет в
  `<head>` (#185) — для краулера, который начал отсюда, а не пришёл по ссылке.
— Оба файла собираются один раз при построении роутера: их содержимое
  зависит только от конфигурации, которая за время жизни процесса не меняется.

Главный тест не содержит ожидаемого списка адресов — список дрейфовал бы
вместе с кодом, правился бы в том же коммите и тем же человеком по той же
неверной причине. Вместо этого он рендерит навигационную строку — ту самую,
которую несёт каждая страница и видит каждый читатель, — и требует, чтобы
множество её ссылок совпадало с множеством `<loc>`. Совпасть они могут только
будучи оба правильными.

Мутационная проверка, шесть правок, все пойманы: карта забыла страницу
обращения (2 падения); карта перечисляет гид при выключенном флаге (1);
robots перестал указывать на карту (2); robots перечислил платёжный вебхук
(1); карта смонтирована без origin (2); роутер не подключён в `create_app`
(2). Итог — 34 теста зелёные.
