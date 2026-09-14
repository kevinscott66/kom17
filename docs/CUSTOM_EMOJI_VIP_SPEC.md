# ТЗ #25 — Кастомные эмодзи VIP (косметический бейдж)

Status: **SHIPPED** (product decisions taken 2026-06-05). Kept as an ADR.
Replaces the static "в разработке" stub in `handlers/vip.py`
(`/emojis /emoji_set /emoji_buy /emoji_preview /эмодзи`).

## Product decisions (от пользователя, 2026-06-05)

| Вопрос | Решение |
|---|---|
| Что это | **Косметический бейдж в боте.** Эмодзи хранится в нашей БД и подставляется ботом рядом с именем. НЕ зависит от Telegram Premium / Fragment / custom_emoji entities. |
| Деньги | **Бесплатно для VIP.** Нет money-path. `/emoji_buy` НЕ списывает монеты — доступ к набору открывается фактом наличия VIP. |
| Каталог | **Фиксированный VIP-набор.** Один закрытый список, одинаковый для всех VIP. Источник — константа в коде (не shop_items, не пользовательский ввод). |
| Где показывать | **Рекомендация (моя):** карточка `/profile` + топы/лидерборды. Обоснование ниже. |

### Почему именно profile + топы (и почему НЕ остальное)

Поиск по реализациям подтвердил: настоящие Telegram custom-emoji entities
(`custom_emoji_id`) требуют Premium у отправителя и Fragment-username у бота —
для нашего кейса это тупик. Значит бейдж рисуем сами, и показывать его имеет
смысл только там, **где сообщение полностью рендерит бот**:

- ✅ **Карточка `/profile`** — бот рендерит её целиком, нулевой оверхед.
- ✅ **Топы/лидерборды (`/top` и т.п.)** — там уже есть резолвер
  отображаемого имени (`get_user_display_name_in_chat`, см. `handlers/nick.py`),
  бейдж добавляется в ОДНОМ месте.
- ❌ **Префикс в обычных сообщениях группы** — отклонено: бот не может
  дописать эмодзи к чужому сообщению, не удалив и не переслав его (ломает
  reply/медиа/порядок). Дорого и плохой UX.
- ➖ **Никнейм/титул** — это не отдельная поверхность, а поле хранения,
  которое и так питает profile + топы.

Итог: бейдж — это префикс к **display name**, добавляемый в одном резолвере,
который уже используют и `/profile`, и `/top`.

## Существующие швы, на которые опираемся

- `repositories/vip_repo.py` → `VipRepo` + `is_vip` (читает `users.vip_till`,
  глобальный VIP). VIP — бинарный статус, истечение проверяется в WHERE
  (без cron). Бейдж скрывается автоматически, когда VIP истёк, потому что
  резолвер спрашивает `is_vip` в момент рендера.
- `handlers/nick.py` + `NicknamesRepo` (`users.user_group_nicknames`) и
  легаси `get_user_display_name_in_chat` — точка, куда вставляется бейдж.
- `handlers/profile.py:_format` — карточка, рендерит `full_name`.
- `middlewares/economy.py` (`EconomyMiddleware`) / session-middleware — инъекция
  репозиториев под общей сессией.

## Модель данных

Новая маленькая таблица в `economy.db` (НЕ трогаем общий с легаси `EconomyUser`):

```
user_emoji_badge(
    user_id   INTEGER PRIMARY KEY,   -- одна выбранная иконка на юзера
    emoji     TEXT NOT NULL,         -- значение ИЗ фикс-набора (валидируется)
    set_at    DATETIME
)
```

- Миграция: `migrations/versions/economy/0005_user_emoji_badge.py`
  (down_revision = `0004_check_claims_unique`). `create_all` строит её для тестов.
- Одна строка = текущий выбор. `/emoji_set X` → upsert. Снять → delete (bare `/emoji_set`).

## Фиксированный VIP-набор

Константа в коде, напр. `services/emoji_badge_service.py`:

```python
VIP_BADGE_SET: tuple[str, ...] = ("👑", "💎", "🔥", "⭐", "🦄", "🌟", "🍀", "🎭", ...)
```

Валидация: `/emoji_set` принимает только член `VIP_BADGE_SET` (и ровно один
emoji-графему, не текст/картинку). Любой ввод вне набора → подсказка со списком.

## Команды (легаси-паритет, 4 шт.)

| Команда | Поведение |
|---|---|
| `/emojis` (`/эмодзи`) | Показать весь VIP-набор + что сейчас надето. Не-VIP видит апселл «оформи VIP». |
| `/emoji_set <emoji>` | VIP-only. Надеть бейдж из набора (валидация членства + VIP). Bare `/emoji_set` — снять. |
| `/emoji_preview` | VIP-only. Показать, как имя выглядит с бейджем (`{badge} {имя}`). |
| `/emoji_buy` | **Нет money-path.** Так как бесплатно для VIP — это информационный алиас: VIP → «набор уже открыт, выбери через /emoji_set»; не-VIP → апселл. Решение задокументировано здесь, чтобы поведение не выглядело багом. |

Все — private-chat-only (как остальные ported economy-стабы); группы падают в легаси.

## Гейтинг

- Надеть/превью — только при активном глобальном VIP (`VipRepo.is_vip`).
- Не-VIP: дружелюбный апселл, НЕ молчание.
- VIP истёк → резолвер не подставляет бейдж (проверка в момент рендера),
  строка в `user_emoji_badge` остаётся (вернётся при продлении VIP).

## Резолвер отображения (ключевая интеграция)

Одна функция-шов, например в `services/emoji_badge_service.py`:

```python
async def decorate_display_name(user_id: int, base_name: str) -> str:
    # if is_vip(user_id) and badge := selected(user_id): return f"{badge} {base_name}"
    # else: return base_name
```

Вызывается из:
1. `handlers/profile.py:_format` — перед рендером имени.
2. Рендер топов (`/top`-поверхность) — там же, где сейчас зовётся резолвер ника.

Везде HTML-escape базового имени сохраняется (бейдж — из доверенного фикс-набора,
эскейпить его не нужно, но имя — по-прежнему через `html.escape`).

## i18n

Новые ключи в `i18n/data/{ru,en}.yaml` (парность обязательна):
`h_emoji_list`, `h_emoji_set_ok`, `h_emoji_cleared`, `h_emoji_not_in_set`,
`h_emoji_vip_only` (апселл), `h_emoji_preview`, `h_emoji_buy_info`.

## Тесты

- unit `services/test_emoji_badge_service.py`: валидация набора, VIP-гейт,
  `decorate_display_name` (VIP+бейдж / VIP-без-бейджа / не-VIP-с-строкой → без бейджа).
- integration `repositories/test_emoji_badge_repo.py`: upsert/delete/get.
- e2e `handlers/test_emoji.py`: каждая из 4 команд + апселл не-VIP + группа→fall-through.
- regression `test_command_surface.py`: токены эмодзи остаются в ported-наборе.
- Профиль/топ: тест, что бейдж появляется в карточке у VIP и исчезает после истечения.

## Не входит в scope

- Telegram premium custom_emoji entities (требуют Premium/Fragment — отклонено).
- Анимированные эмодзи-паки.
- Покупка за монеты / паки / подписка (выбрано «бесплатно для VIP»).
- Пользовательские произвольные эмодзи вне набора.
- Префикс в обычных сообщениях групп (технически невозможно без delete+repost).

## Порядок реализации

1. Модель `UserEmojiBadge` + миграция 0005.
2. `EmojiBadgeRepo` (get/upsert/delete) под `economy.db`.
3. `EmojiBadgeService` (VIP_BADGE_SET, валидация, VIP-гейт через VipRepo,
   `decorate_display_name`).
4. Инъекция в `EconomyMiddleware`.
5. Заменить 4 стаба в `handlers/vip.py` реальными хендлерами (или вынести в
   `handlers/emoji.py` по образцу `handlers/checks.py`).
6. Интеграция бейджа в `/profile` и топы через резолвер.
7. i18n ru/en.
8. Тесты всех уровней.
9. Гейты: `ruff check src/`, `mypy src/ --strict`, полный `pytest`.
