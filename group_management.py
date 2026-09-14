# -*- coding: utf-8 -*-
"""
Мультигрупповое управление: меню выбора группы и переключение активной.
Этап 1: класс GroupManager использует инъекцию зависимостей (функции из bot.py),
чтобы не импортировать bot и не создавать циклических зависимостей.

Использование из bot.py:
  from group_management import GroupManager
  manager = GroupManager(
      user_id,
      get_current_group=get_user_current_group,
      get_manageable_groups=get_user_manageable_groups,
      set_current_group=set_user_current_group,
      get_group_info=<функция получения title по group_id>,
  )
  text, markup = manager.get_main_menu()  # текст и клавиатура для админ-панели групп
"""

from typing import Optional, Callable, List, Dict, Any, Tuple

# InlineKeyboardMarkup/InlineKeyboardButton создаются в bot.py при использовании
def _noop(*args: Any, **kwargs: Any) -> Any:
    return None


class GroupManager:
    """
    Менеджер групп пользователя: активная группа, список групп, переключение.
    Все обращения к БД и боту — через переданные функции.
    """

    def __init__(
        self,
        user_id: int,
        get_current_group: Optional[Callable[[int], Optional[int]]] = None,
        get_manageable_groups: Optional[Callable[[int], List[Dict[str, Any]]]] = None,
        set_current_group: Optional[Callable[[int, Optional[int]], None]] = None,
        get_group_info: Optional[Callable[[int], Dict[str, Any]]] = None,
    ):
        self.user_id = user_id
        self._get_current = get_current_group or _noop
        self._get_manageable = get_manageable_groups or _noop
        self._set_current = set_current_group or _noop
        self._get_group_info = get_group_info or (lambda gid: {'title': str(gid), 'id': gid})

        self.current_group_id: Optional[int] = None
        self.user_groups: List[Dict[str, Any]] = []
        self._refresh()

    def _refresh(self) -> None:
        self.current_group_id = self._get_current(self.user_id)
        self.user_groups = self._get_manageable(self.user_id) or []

    def get_main_menu(self) -> Tuple[str, Any]:
        """
        Возвращает (text, markup) для экрана «Админ-панель / мои группы».
        markup должен быть InlineKeyboardMarkup — создаётся в bot.py, здесь возвращаем
        структуру для кнопок: list of rows, each row list of (text, callback_data).
        """
        self._refresh()
        lines = ["👑 **Админ-панель**\n"]
        if self.current_group_id:
            title = next(
                (g.get("title", str(g["id"])) for g in self.user_groups if g.get("id") == self.current_group_id),
                str(self.current_group_id),
            )
            lines.append(f"🎯 Активная группа: **{str(title)[:40]}**\n")
        else:
            lines.append("🎯 Активная группа не выбрана\n")
        lines.append("Выберите группу для управления:\n")
        text = "".join(lines)

        buttons: List[List[Tuple[str, str]]] = []
        if self.current_group_id:
            buttons.append([("⚙️ Управление активной", "admin_open_current_group")])
        buttons.append([("🔄 Сменить группу", "admin_panel_switch_group")])
        for g in self.user_groups[:20]:
            title = (g.get("title") or str(g.get("id", "")))[:40]
            role_emoji = "👑" if g.get("role") == "owner" else "🛡"
            buttons.append([(f"{role_emoji} {title}", f"admin_group_{g.get('id')}")])

        return text, buttons

    def switch_group(self, new_group_id: int) -> None:
        """Устанавливает активную группу и обновляет состояние."""
        self._set_current(self.user_id, new_group_id)
        self.current_group_id = new_group_id
