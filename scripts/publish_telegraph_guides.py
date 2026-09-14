#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Печать ссылок на гайды команд на сайте бота (те же, что показывает админка).

Гайды хранятся в telegraph_guide_ru.md / telegraph_guide_en.md и отдаются по /commands и /commands/en.

Запуск из корня репозитория (рядом с main.py и bot.py). В .env желательно указать WEBHOOK_URL
(скрипт подставляет заглушку, если пусто — нужна для импорта bot).

  python3 scripts/publish_telegraph_guides.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _ensure_writable_paths_for_publish(root: Path) -> None:
    """
    bot.py при импорте вызывает init_all_databases() и создаёт DATABASE_DIR.
    В .env с сервера часто указан DATABASE_DIR=/var/lib/telegram-bot — на локальной машине
    нет прав на запись → PermissionError. Для этого скрипта достаточно локальной database/.
    """
    local_db = (root / "database").resolve()
    candidates: list[Path] = []
    env_db = (os.environ.get("DATABASE_DIR") or "").strip()
    if env_db:
        candidates.append(Path(env_db).expanduser().resolve())
    candidates.append(local_db)

    chosen: Path | None = None
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            chosen = p
            break
        except OSError:
            continue
    if chosen is None:
        raise RuntimeError(f"Не удалось создать каталог для БД (проверьте права): {local_db}")
    os.environ["DATABASE_DIR"] = str(chosen)

    sf = (os.environ.get("SETTINGS_FILE") or "").strip()
    if sf:
        sf_path = Path(sf).expanduser().resolve()
        try:
            sf_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            os.environ["SETTINGS_FILE"] = str(chosen / "settings.json")
    else:
        os.environ.setdefault("SETTINGS_FILE", str(chosen / "settings.json"))


def _reset_db_file_env_overrides() -> None:
    """
    В .env с сервера часто заданы ECONOMY_DB=/var/lib/.../economy.db и т.д.
    После подмены DATABASE_DIR на локальный каталог эти переменные всё ещё указывают на сервер → SQLite ошибки.
    """
    for key in ("ECONOMY_DB", "USERS_DB", "MESSAGE_STATS_DIR"):
        os.environ.pop(key, None)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    _load_dotenv(root / ".env")
    _ensure_writable_paths_for_publish(root)
    _reset_db_file_env_overrides()
    os.environ.setdefault("WEBHOOK_URL", os.environ.get("WEBHOOK_URL") or "https://127.0.0.1")
    sys.path.insert(0, str(root))
    import bot  # noqa: E402

    out = bot._update_telegraph_commands()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
