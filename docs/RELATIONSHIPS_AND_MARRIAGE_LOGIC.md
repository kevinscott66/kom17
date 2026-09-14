# Отношения и браки: логика A–Z

Код в основном в `bot.py` (таблицы БД — инициализация + миграции; хендлеры команд и callback; РП; алиасы).

---

## 1. Данные в БД (`users.db`)

### Ключ пары в чате

- Везде пара хранится как **`(user1_id, user2_id) = (min(a,b), max(a,b))`** — функция `_pair(u1, u2)`.
- Уникальность: **`UNIQUE(chat_id, user1_id, user2_id)`** для `relationships` и `marriages`.

### Таблицы

| Таблица | Назначение |
|--------|------------|
| `relationships` | Пара в отношениях; колонки: `experience`, `last_activity_at`, опционально `status`, `ended_at` (миграции). |
| `relationship_proposals` | Исходящие предложения: `from_id`, `to_id`, `chat_id`, время. |
| `relationship_activity_log` | Платные совместные действия + начисление XP (ключ активности, `xp_gained`, `paid_by_user_id`). |
| `marriages` | Брак в чате; `experience`, `last_activity_at`, `status` (active/divorced), `divorced_at`, `restore_until`, `in_top`, `auto_divorce`, `duration_days` и др. (миграции). |
| `marriage_proposals` | Предложение брака `from_id` → `to_id`. |
| `marriage_activity_log` | Аналогично логу отношений, но для брака. |

### Миграции

- К `relationships` / `marriages` добавлялись `experience`, `last_activity_at`, `status`, `ended_at` и поля брака — см. блок миграций в `bot.py` (около `PRAGMA table_info`).

---

## 2. Отношения — жизненный цикл

### 2.1 Предложение

- Команды: **`/relationship`**, **`/rel`**, **`отношения`**, **`в_отношениях`**.
- **Только в группе** (`ensure_user_access(..., require_group=True)`).
- Режим A — **ответ на сообщение** кандидата:
  - нельзя себе, боту, если уже есть пара с этим человеком;
  - `_insert_relationship_proposal` → кнопки **`rel_accept_{prop_id}`** / **`rel_decline_{prop_id}`** в ответ на **сообщение адресата** (как у брака).
- Режим B — **без реплая**: показ **списка текущих отношений** пользователя в этом чате (может быть несколько партнёров) + кнопки «совместные действия» / история.

### 2.2 Принятие / отказ

- **`rel_accept_{id}`**: проверка, что нажал именно `to_id` из `relationship_proposals`; `_create_relationship` → удаление proposal; сообщение «готово».
- **`rel_decline_{id}`**: только `to_id`; proposal удаляется.

### 2.3 Создание записи `_create_relationship`

- `INSERT` в `relationships` с `experience=0`.
- При конфликте `UNIQUE` — **реактивация** (`status='active'`, `ended_at=NULL`) или удаление старой строки и повторный `INSERT`.

### 2.4 Расставание

- **`/breakup`** (ответом на сообщение партнёра): `_delete_relationship_with_partner` — удаление строки пары.

### 2.5 Список пар в чате

- **`/relations`**, **`отношения_список`**, **`отны`**: все активные пары в чате, сортировка по опыту; уровень считается из XP.

### 2.6 Точечные алиасы (как у Ирис)

- «Мои отношения»: `.отнстата`, `.моиотн`, … → подмена на **`/relationship`**.
- «Список пар»: `.пары`, `.отны`, … → **`/relations`**.

---

## 3. Опыт и уровень отношений

### 3.1 Пороги

- Константа **`RELATIONSHIP_LEVEL_XP`**: `(0, 150, 1500, 5000, 10000,
  30000, 60000, 150000, 300000, 1_000_000, 3_000_000, 10_000_000)` —
  **12 уровней** (0…11), названия в **`RELATIONSHIP_LEVEL_NAMES`**.
  Список раньше обрывался на седьмом пороге и назывался «11 игровых
  уровней»; верхние ярусы были добавлены в код и не дописаны сюда.
  Канон живёт в `BondsWriteRepo.RELATIONSHIP_LEVEL_XP` — правки
  порогов делать там (в `handlers/` пока лежат копии, см. тикет).

### 3.2 Уровень из XP

- **`relationship_xp_to_level(experience)`** — монотонно по порогам.

### 3.3 Спад (decay)

- **`RELATIONSHIP_DECAY_PER_DAY`**, **`_apply_relationship_decay`**: при длительной неактивности снижает `experience` и обновляет `last_activity_at`. Вызывается при чтении отношений (списки, партнёр).

### 3.4 Начисление XP

- **`relationship_add_xp`**: `UPDATE relationships SET experience = experience + ?`, опционально запись в **`relationship_activity_log`** + `last_activity_at`.
- Источники: **РП** (если пара подходит по уровню), **платные совместные действия** (`callback_rel_do_activity`).

---

## 4. Совместные действия в отношениях (монеты)

- Список **`RELATIONSHIP_ACTIVITIES`**: стоимость, XP, `min_level`, `effect_hours`, тексты `done_line_*`, иконки.
- Вход: кнопка из статуса **`rel_activity_menu_{partner_id}`** → меню с действиями.
- **`rel_do_{key}_{partner_id}`**:
  - проверка пары, уровня, баланса монет;
  - `remove_coins` → `relationship_add_xp`;
  - сообщение «готово» через **`_build_rel_activity_done_message`** (без Markdown).

### История

- **`rel_history_{partner_id}`** — чтение `relationship_activity_log`.

---

## 5. РП (`handle_rp_relationship_action`)

Только **группы**. Порядок проверок:

1. Команда из **`RP_ACTION_SPEC`** → `min_level`, XP, ключ перевода, `activity_key`.
2. **РП 18+** (`min_level >= RP_18_MIN_LEVEL`): нужно `rp_18_enabled` в настройках группы; иначе подсказка/одноразовый промпт админу.
3. **Антиспам** по чату/пользователю (`rp_rate_limit_tracker`).
4. Цель: **реплай**; у части 18+ команд цель дополнительно разбирается
   из текста сообщения как **@username**.
5. Если цель — **супруг** по браку → **`marriage_add_xp`** (без проверки уровня отношений).
6. Иначе если есть **отношения с целью** → проверка **`level >= min_level`**, затем **`relationship_add_xp`**.
7. **Универсальные** РП (`RP_UNIVERSAL_ACTIONS`) без пары — без XP, текст «general».
8. **VIP вне отношений** (настройка группы + VIP): «романтические» команды без пары — без XP.
9. Иначе — **`rel_rp_no_pair`**.

Триггер текстовых сообщений — в **`handle_shortcut_messages`** (или аналог): реплай + разбор команды.

---

## 6. Брак — условия и предложение

### 6.1 Минимальный уровень отношений

- **`MARRIAGE_MIN_RELATIONSHIP_LEVEL = 6`** (порог XP **`RELATIONSHIP_LEVEL_XP[6]` = 60000**).
- Проверяется в **`cmd_marry`** и при **принятии** предложения в **`_process_marriage_proposal_response`**.

### 6.2 Команда `/marry` (ответом)

- Проверки: группа, реплай, не себя/бот, ни один не в браке с другим, **есть отношения с целью** и **`level >= 6`**.
- `_insert_marriage_proposal` → сообщение с **`marry_accept_{prop_id}`** / **`marry_decline_{prop_id}`**; кэш `marry_prop_{chat_id}_{message_id}` для разрешения по реплаю.

### 6.3 Принятие / отказ

- Кнопки или **`/marry_accept`**, **`/marry_decline`** и текстовые алиасы.
- **`_process_marriage_proposal_response`**:
  - при accept снова **уровень отношений ≥ 6**;
  - у каждого не должно быть **другого активного** брака (иначе отмена);
  - **восстановление** в течение срока после развода (`divorced` + `restore_until`) или **`INSERT`** новой строки `marriages`;
  - удаление proposal.

---

## 7. Брак после заключения

### 7.1 Статус

- **`/marriage`**, **`my_marriage`**, **`брак_статус`**: `_get_marriage` → дата, длительность, категория, уровень брака, XP, кнопки «действия в браке» и история.

### 7.2 Уровень брака

- **`marriage_xp_to_level`**, **`MARRIAGE_ACTIVITIES`**, **`marriage_level_name`** — отдельная шкала от **опыта брака**.

### 7.3 Платные действия в браке

- **`marriage_activity_menu`** → **`marriage_do_{key}`**: списание монет, **`marriage_add_xp`**, лог в **`marriage_activity_log`**.

### 7.4 История

- **`marriage_history`** callback.

### 7.5 Список браков в чате

- **`/marriages`**, **`браки`**, **`пары`**.

### 7.6 Развод

- **`/divorce`**: **`_delete_marriage`** — мягко `status=divorced`, `restore_until` (окно восстановления).

### 7.7 Прочие пользовательские команды (брак)

- **`marry_top_on` / `marry_top_off`** — участие в рейтинге браков.
- **`marry_extend`** — продление срока (монеты, `duration_days`).
- **`marry_auto_divorce`** — режим авто-развода.
- **`marry_other`** — брак другого пользователя (ответом).

### 7.8 Админ

- **`cmd_admin_marry_pair`**, **`cmd_admin_divorce_pair`**, **`cmd_admin_reset_marriages`**, **`cmd_admin_divorce_left`** — для персонала (проверки прав внутри хендлеров).

---

## 8. Очистка при выходе из чата

Правило: если участник числится вышедшим из чата **дольше 7 дней** и не вернулся — его **отношения** в этом чате переводятся в `status='ended'`, а **браки** — в `divorced` с трёхдневным `restore_until` (тот же льготный срок, что и у `/divorce`).

- **Legacy** (`bot.py:21586` `_cleanup_left_users_bonds`): читал таблицу `user_chat_left` и вызывался инлайн из пяти ГРУППОВЫХ ПУТЕЙ ЧТЕНИЯ (`:21758`, `:22410`, `:22668`, `:22989`, `:23487`). Значит, правило срабатывало только там, где кто-то открыл карточку пары, и любое чтение могло побочным эффектом расторгнуть брак.
- **Сейчас** (#482): то же правило живёт фоновой задачей `scheduler/left_bonds_cleanup.py` (`LeftBondsCleanupSweeper`, раз в 6 часов, запуск из `app.py`). Источник факта ухода — `user_group_joins.left_at`/`is_active` (пишет `handlers/group_events.py:_record_leave`), а не legacy-таблица `user_chat_left`. Чтение больше ничего не пишет: `/marriages` снова чистый SELECT, зато правило теперь действует во ВСЕХ чатах, а не только в тех, где в карточку заглянули.
- Ничто не расторгается без живой проверки `get_chat_member`: ошибка проверки трактуется как «не трогать» (как и в legacy), а если человек на самом деле вернулся — снимается ошибочная метка ухода.
- **Строки legacy-таблицы `user_chat_left` (в проде их около одиннадцати) этот сборщик НЕ видит** — они предшествуют `user_group_joins` и останутся замороженными, пока эти участники не будут замечены заново.

---

## 9. Связь «отношения ↔ брак»

- В одном чате пользователь может иметь **отношения** с несколькими партнёрами (если логика списка это допускает), но **брак** — одна активная запись на пользователя в чате (проверки при accept).
- РП с супругом идёт в **XP брака**; РП с партнёром в отношениях (не обязательно супруг) — в **XP отношений**, если не сработала ветка брака первой.

---

## 10. Переводы и каталог команд

- Строки в **`translations.py`** (`rel_*`, `marry_*`, `rel_rp_*`, …).
- Регистрация в **`COMMAND_CATALOG`** / Telegra.ph — для справки пользователей.

---

## 11. Тесты

Покрытие в этом репозитории:

- `tests/e2e/handlers/test_relationship.py`, `test_relations.py` — путь
  `/relationship` и список отношений.
- `tests/e2e/handlers/test_marriage.py` — `/marry` → принятие → `/marriage`.
- `tests/e2e/handlers/test_admin_relations.py`, `test_admin_marriages.py` —
  админские срезы.
- `tests/unit/handlers/test_marriage_rel_list_bounds.py`,
  `test_relations_board_bounds.py`, `test_marriage_name_fallback.py` —
  границы выборок и запасное имя.

Смоук-скрипты, которые прогоняют те же сценарии двумя настоящими
Telegram-аккаунтами через userbot, в публичный снимок не входят: они
завязаны на живые сессии и на конкретные идентификаторы аккаунтов.

---

*При изменении логики обновляйте этот файл и пороги `RELATIONSHIP_LEVEL_XP` / `MARRIAGE_MIN_RELATIONSHIP_LEVEL` в одном месте в коде.*
