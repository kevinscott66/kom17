# -*- coding: utf-8 -*-
"""
Мост обратной совместимости со старыми командами и callback_data.
Этап 1: только заглушка и комментарии. Не регистрирует хэндлеры.

После перехода на новую систему сюда можно вынести:
- old_commands_bridge: перенаправление старых команд в universal_command_handler
- old_callbacks_bridge: попытка обработать callback через новую систему, иначе — старая

Сейчас все хэндлеры остаются в bot.py, этот файл не импортируется.
"""

# OLD_COMMANDS_LIST — полный список всех команд из COMMAND_REGISTRY (для моста)
# def old_commands_bridge(message): ...
# def old_callbacks_bridge(call): ...
