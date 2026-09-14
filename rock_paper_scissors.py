# -*- coding: utf-8 -*-
"""
Камень, ножницы, бумага (CPC / КНБ) на ставки.
Использует систему внутренней валюты бота (get_balance, add_coins, remove_coins).
"""

import logging
import threading
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable, Tuple, List

try:
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
except ImportError:
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None

try:
    from telebot.util import extract_command as _extract_slash_command
except ImportError:
    _extract_slash_command = None

logger = logging.getLogger(__name__)

# Имена команд после нормализации (cpc, кириллица кнб, латиница knb — часто путают раскладку)
def _normalize_bot_command_name(raw: str) -> str:
    if not raw:
        return ""
    u = unicodedata.normalize("NFKC", raw)
    u = "".join(ch for ch in u if ch not in "\u200b\u200c\u200d\ufeff")
    return u.casefold()


_CPC_CMD_ALIASES = frozenset(
    _normalize_bot_command_name(x) for x in ("cpc", "кнб", "knb")
)

# Константы
ACCEPT_TIMEOUT_SEC = 60
CHOOSE_TIMEOUT_SEC = 30
CHOICES = ("rock", "scissors", "paper")
CHOICE_EMOJI = {"rock": "✊", "scissors": "✌️", "paper": "✋"}
CHOICE_KEYS = {"rock": "cpc_choice_rock", "scissors": "cpc_choice_scissors", "paper": "cpc_choice_paper"}
# rock > scissors, scissors > paper, paper > rock
WIN_MATRIX = {("rock", "scissors"): 1, ("scissors", "paper"): 1, ("paper", "rock"): 1}


@dataclass
class CPCGameSession:
    """Сессия игры КНБ."""
    id: int
    challenger_id: int
    opponent_id: int
    chat_id: int
    bet: int
    status: str  # waiting_accept, choosing, done, cancelled, timeout
    challenge_message_id: Optional[int] = None
    accept_until: Optional[datetime] = None
    choose_until: Optional[datetime] = None
    challenger_choice: Optional[str] = None
    opponent_choice: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)

    def is_accept_expired(self) -> bool:
        if self.status != "waiting_accept" or not self.accept_until:
            return False
        return datetime.now() > self.accept_until

    def is_choose_expired(self) -> bool:
        if self.status != "choosing" or not self.choose_until:
            return False
        return datetime.now() > self.choose_until


class CPCManager:
    """Менеджер активных игр КНБ."""

    def __init__(self):
        self.sessions: Dict[int, "CPCGameSession"] = {}
        self.by_chat_message: Dict[Tuple[int, int], int] = {}  # (chat_id, message_id) -> session_id
        self.counter = 0
        self.lock = threading.Lock()

    def create(
        self,
        challenger_id: int,
        opponent_id: int,
        chat_id: int,
        bet: int,
        get_balance: Callable[[int], int],
        min_bet: int,
        max_bet: int,
    ) -> Tuple[Optional["CPCGameSession"], Optional[str], Optional[dict]]:
        """Создаёт вызов на игру. Возвращает (session, error_key, error_kwargs). Если error_key — вызывающий делает t(lang, error_key, **error_kwargs)."""
        with self.lock:
            if self.get_active_for_user(challenger_id) or self.get_active_for_user(opponent_id):
                return None, "cpc_err_active_game", {}
            if bet < min_bet:
                return None, "cpc_err_min_bet", {"min_bet": min_bet}
            if bet > max_bet:
                return None, "cpc_err_max_bet", {"max_bet": max_bet}
            if get_balance(challenger_id) < bet:
                return None, "cpc_err_insufficient_you", {}
            if get_balance(opponent_id) < bet:
                return None, "cpc_err_insufficient_opponent", {}

            self.counter += 1
            now = datetime.now()
            session = CPCGameSession(
                id=self.counter,
                challenger_id=challenger_id,
                opponent_id=opponent_id,
                chat_id=chat_id,
                bet=bet,
                status="waiting_accept",
                accept_until=now + timedelta(seconds=ACCEPT_TIMEOUT_SEC),
                created_at=now,
            )
            self.sessions[session.id] = session
            logger.info("🎮 CPC: создана сессия #%s %s vs %s, ставка %s", session.id, challenger_id, opponent_id, bet)
            return session, None, None

    def set_challenge_message(self, session_id: int, message_id: int) -> None:
        with self.lock:
            s = self.sessions.get(session_id)
            if s:
                s.challenge_message_id = message_id
                self.by_chat_message[(s.chat_id, message_id)] = session_id

    def get(self, session_id: int) -> Optional["CPCGameSession"]:
        return self.sessions.get(session_id)

    def get_by_chat_message(self, chat_id: int, message_id: int) -> Optional["CPCGameSession"]:
        sid = self.by_chat_message.get((chat_id, message_id))
        if sid is None:
            return None
        return self.sessions.get(sid)

    def get_active_for_user(self, user_id: int) -> Optional["CPCGameSession"]:
        for s in self.sessions.values():
            if s.status in ("waiting_accept", "choosing") and (s.challenger_id == user_id or s.opponent_id == user_id):
                return s
        return None

    def accept(self, session_id: int, user_id: int) -> Tuple[bool, Optional[str], Optional[dict]]:
        """Возвращает (ok, error_key, error_kwargs). При ok=True вызывающий показывает t(lang, 'cpc_accept_ok')."""
        with self.lock:
            s = self.sessions.get(session_id)
            if not s:
                return False, "cpc_err_not_found", {}
            if s.status != "waiting_accept":
                return False, "cpc_err_already_started", {}
            if user_id != s.opponent_id:
                return False, "cpc_err_not_your_challenge", {}
            if s.is_accept_expired():
                s.status = "timeout"
                self._remove(session_id)
                return False, "cpc_err_accept_expired", {}

            s.status = "choosing"
            s.choose_until = datetime.now() + timedelta(seconds=CHOOSE_TIMEOUT_SEC)
            logger.info("🎮 CPC #%s: принята игроком %s", session_id, user_id)
            return True, None, None

    def decline(self, session_id: int, user_id: int) -> Tuple[bool, Optional[str], Optional[dict]]:
        """Возвращает (ok, error_key, error_kwargs). При ok вызывающий показывает t(lang, 'cpc_decline_ok')."""
        with self.lock:
            s = self.sessions.get(session_id)
            if not s:
                return False, "cpc_err_not_found", {}
            if s.status != "waiting_accept":
                return False, "cpc_err_already_started", {}
            if user_id != s.opponent_id:
                return False, "cpc_err_not_your_challenge", {}
            s.status = "cancelled"
            self._remove(session_id)
            logger.info("🎮 CPC #%s: отклонена игроком %s", session_id, user_id)
            return True, "cpc_decline_ok", {}

    def cancel_by_user(self, user_id: int) -> Tuple[bool, Optional[str], Optional[dict]]:
        """Возвращает (ok, error_key, error_kwargs). При ok — t(lang, 'cpc_cancel_ok')."""
        with self.lock:
            s = self.get_active_for_user(user_id)
            if not s:
                return False, "cpc_err_no_game", {}
            sid = s.id
            self._remove(sid)
            s.status = "cancelled"
            logger.info("🎮 CPC #%s: отменена пользователем %s", sid, user_id)
            return True, "cpc_cancel_ok", {}

    def set_choice(self, session_id: int, user_id: int, choice: str) -> Tuple[bool, Optional[str], Optional[dict], bool]:
        """Возвращает (success, error_key, error_kwargs, round_complete). При success вызывающий показывает t(lang, 'cpc_choice_ok', choice=t(lang, CHOICE_KEYS[choice]))."""
        if choice not in CHOICES:
            return False, "cpc_err_invalid_choice", {}, False
        with self.lock:
            s = self.sessions.get(session_id)
            if not s:
                return False, "cpc_err_not_found", {}, False
            if s.status != "choosing":
                return False, "cpc_err_not_choosing", {}, False
            if user_id == s.challenger_id:
                if s.challenger_choice is not None:
                    return False, "cpc_err_already_chose", {}, False
                s.challenger_choice = choice
            elif user_id == s.opponent_id:
                if s.opponent_choice is not None:
                    return False, "cpc_err_already_chose", {}, False
                s.opponent_choice = choice
            else:
                return False, "cpc_err_not_participant", {}, False

            round_complete = s.challenger_choice is not None and s.opponent_choice is not None
            logger.info("🎮 CPC #%s: игрок %s выбрал %s; полный выбор: %s", session_id, user_id, choice, round_complete)
            return True, None, None, round_complete

    def _remove(self, session_id: int) -> None:
        s = self.sessions.get(session_id)
        if s:
            if s.challenge_message_id is not None:
                self.by_chat_message.pop((s.chat_id, s.challenge_message_id), None)
            del self.sessions[session_id]

    def remove_session(self, session_id: int) -> None:
        with self.lock:
            self._remove(session_id)

    def cleanup_accept_timeouts(self) -> int:
        """Сбрасывает сессии с истёкшим временем принятия. Возвращает количество."""
        to_edit: List[Tuple[int, int, int]] = []  # (chat_id, message_id, challenger_id)
        with self.lock:
            for sid in list(self.sessions.keys()):
                s = self.sessions.get(sid)
                if s and s.status == "waiting_accept" and s.is_accept_expired():
                    s.status = "timeout"
                    if s.chat_id and s.challenge_message_id:
                        to_edit.append((s.chat_id, s.challenge_message_id, s.challenger_id))
                    self._remove(sid)
        get_lang = _deps.get("get_user_language", lambda u: "ru")
        t = _deps.get("t", lambda lang, key, **kw: key)
        for chat_id, msg_id, challenger_id in to_edit:
            try:
                bot = _deps.get("bot")
                if bot:
                    lang = get_lang(challenger_id)
                    bot.edit_message_text(t(lang, "cpc_edit_accept_expired"), chat_id, msg_id)
            except Exception as e:
                logger.debug("CPC: не удалось обновить сообщение о таймауте: %s", e)
        return len(to_edit)

    def cleanup_choose_timeouts(self) -> List[int]:
        """Обрабатывает таймаут выбора: кто не выбрал — проигрывает. Возвращает список session_id для вызова _resolve_and_finish."""
        to_resolve: List[int] = []
        with self.lock:
            for sid in list(self.sessions.keys()):
                s = self.sessions.get(sid)
                if not s or s.status != "choosing" or not s.is_choose_expired():
                    continue
                # Кто не выбрал — проигрывает
                if s.challenger_choice is None and s.opponent_choice is None:
                    s.status = "cancelled"
                    self._remove(sid)
                    continue
                if s.challenger_choice is None:
                    s.challenger_choice = "timeout_lose"
                    s.opponent_choice = s.opponent_choice or "rock"
                elif s.opponent_choice is None:
                    s.opponent_choice = "timeout_lose"
                    s.challenger_choice = s.challenger_choice or "rock"
                s.status = "done"
                to_resolve.append(sid)
        return to_resolve

    @staticmethod
    def resolve_winner(challenger_choice: str, opponent_choice: str) -> Optional[int]:
        """Возвращает winner_id (challenger_id или opponent_id) или None при ничьей. Учитывает timeout_lose."""
        if challenger_choice == "timeout_lose":
            return 2  # opponent wins
        if opponent_choice == "timeout_lose":
            return 1  # challenger wins
        if challenger_choice == opponent_choice:
            return None
        if (challenger_choice, opponent_choice) in WIN_MATRIX:
            return 1
        return 2


# Глобальный менеджер (инициализируется при регистрации)
_cpc_manager: Optional[CPCManager] = None
_deps: Dict[str, Any] = {}


def _get_bot():
    return _deps.get("bot")


def _get_manager() -> CPCManager:
    global _cpc_manager
    if _cpc_manager is None:
        _cpc_manager = CPCManager()
    return _cpc_manager


def _resolve_opponent_from_message(message) -> Optional[Any]:
    """Противник из ответа на сообщение или из первого text_mention."""
    if getattr(message, "reply_to_message", None) and getattr(message.reply_to_message, "from_user", None):
        return message.reply_to_message.from_user
    for ent in (getattr(message, "entities", None) or []):
        if getattr(ent, "type", None) == "text_mention" and getattr(ent, "user", None):
            return ent.user
    return None


def _cmd_cpc(message) -> None:
    """Обработчик /cpc [сумма] @противник или /cpc [сумма] (ответом на сообщение)."""
    bot = _get_bot()
    if not bot:
        return
    ensure_user_access = _deps.get("ensure_user_access")
    require_group_feature = _deps.get("require_group_feature")
    get_balance = _deps["get_balance"]
    get_user_mention = _deps["get_user_mention"]
    COM_EMOJI = _deps["COM_EMOJI"]
    min_bet = _deps.get("cpc_min_bet", 10)
    max_bet = _deps.get("cpc_max_bet", 100000)

    if ensure_user_access and not ensure_user_access(message, require_group=True):
        return
    if require_group_feature and not require_group_feature(message, "games", message.from_user.id):
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
    t = _deps.get("t", lambda lang, key, **kw: key)
    chat_type = getattr(message.chat, "type", None)
    if chat_type not in ("group", "supergroup"):
        bot.send_message(chat_id, t(lang, "cpc_only_groups"))
        return

    opponent_user = _resolve_opponent_from_message(message)
    if not opponent_user:
        msg_help = t(lang, "cpc_help_title") + "\n\n" + t(lang, "cpc_help_body") + "\n\n" + t(lang, "cpc_stake_range", min_bet=min_bet, max_bet=max_bet, sign=COM_EMOJI)
        bot.send_message(chat_id, msg_help, parse_mode="Markdown")
        return

    opponent_id = opponent_user.id
    if user_id == opponent_id:
        bot.reply_to(message, t(lang, "cpc_self"))
        return
    try:
        bot.get_chat_member(chat_id, opponent_id)
    except Exception:
        bot.reply_to(message, t(lang, "cpc_opponent_not_in_chat"))
        return

    text = (message.text or "").strip()
    parts = re.split(r"\s+", text, 2)
    try:
        bet = int(parts[1]) if len(parts) > 1 else 100
    except (ValueError, IndexError):
        bet = 100
    if bet <= 0:
        bot.reply_to(message, t(lang, "cpc_bet_positive"))
        return

    manager = _get_manager()
    session, err_key, err_kw = manager.create(user_id, opponent_id, chat_id, bet, get_balance, min_bet, max_bet)
    if err_key:
        bot.reply_to(message, t(lang, err_key, **(err_kw or {})))
        return

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(t(lang, "cpc_btn_accept"), callback_data=f"cpc_accept_{session.id}"),
        InlineKeyboardButton(t(lang, "cpc_btn_decline"), callback_data=f"cpc_decline_{session.id}"),
    )
    cpc_player = t(lang, "cpc_player")
    challenger_name = getattr(message.from_user, "first_name", None) or cpc_player
    opponent_name = getattr(opponent_user, "first_name", None) or cpc_player
    msg_text = (
        t(lang, "cpc_challenge_title") + "\n\n"
        f"{t(lang, 'cpc_challenge_by')} {get_user_mention(user_id, challenger_name)}\n"
        f"{t(lang, 'cpc_challenge_opponent')} {get_user_mention(opponent_id, opponent_name)}\n"
        f"{t(lang, 'cpc_challenge_stake')} `{bet}` {COM_EMOJI}\n\n"
        + t(lang, "cpc_accept_time", sec=ACCEPT_TIMEOUT_SEC)
    )
    sent = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")
    if sent:
        manager.set_challenge_message(session.id, sent.message_id)
    logger.info("🎮 CPC #%s: вызов отправлен в чат %s", session.id, chat_id)


def _cmd_accept(message) -> None:
    """Принять вызов (в ответ на сообщение бота с вызовом)."""
    bot = _get_bot()
    if not bot or not message.reply_to_message:
        return
    if message.reply_to_message.from_user.id != bot.get_me().id:
        return
    chat_id = message.chat.id
    msg_id = message.reply_to_message.message_id
    user_id = message.from_user.id
    manager = _get_manager()
    session = manager.get_by_chat_message(chat_id, msg_id)
    if not session:
        lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
        t = _deps.get("t", lambda lang, key, **kw: key)
        bot.reply_to(message, t(lang, "cpc_call_not_found"))
        return
    lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
    t = _deps.get("t", lambda lang, key, **kw: key)
    ok, err_key, err_kw = manager.accept(session.id, user_id)
    if not ok:
        bot.reply_to(message, t(lang, err_key, **(err_kw or {})))
        return
    _on_cpc_accepted(session.id)
    bot.reply_to(message, t(lang, "cpc_accept_ok"))


def _cmd_decline(message) -> None:
    """Отклонить вызов."""
    bot = _get_bot()
    if not bot or not message.reply_to_message:
        return
    if message.reply_to_message.from_user.id != bot.get_me().id:
        return
    chat_id = message.chat.id
    msg_id = message.reply_to_message.message_id
    user_id = message.from_user.id
    manager = _get_manager()
    session = manager.get_by_chat_message(chat_id, msg_id)
    if not session:
        lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
        t = _deps.get("t", lambda lang, key, **kw: key)
        bot.reply_to(message, t(lang, "cpc_call_not_found_short"))
        return
    lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
    t = _deps.get("t", lambda lang, key, **kw: key)
    ok, err_key, err_kw = manager.decline(session.id, user_id)
    if not ok:
        bot.reply_to(message, t(lang, err_key, **(err_kw or {})))
    else:
        bot.reply_to(message, t(lang, "cpc_decline_ok"))
        try:
            bot.edit_message_text(t(lang, "cpc_decline_ok"), chat_id, msg_id)
        except Exception:
            pass


def _cmd_cancel_game(message) -> None:
    """Отменить свою текущую игру."""
    bot = _get_bot()
    if not bot:
        return
    user_id = message.from_user.id
    lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
    t = _deps.get("t", lambda lang, key, **kw: key)
    manager = _get_manager()
    ok, err_key, err_kw = manager.cancel_by_user(user_id)
    msg = t(lang, "cpc_cancel_ok") if ok else t(lang, err_key, **(err_kw or {}))
    bot.reply_to(message, msg)


def _on_cpc_accepted(session_id: int) -> None:
    """После принятия: отправить обоим в ЛС клавиатуру выбора."""
    bot = _get_bot()
    manager = _get_manager()
    session = manager.get(session_id)
    if not session or session.status != "choosing":
        return
    get_user_language = _deps.get("get_user_language", lambda u: "ru")
    t = _deps.get("t", lambda lang, key, **kw: key)
    COM_EMOJI = _deps["COM_EMOJI"]
    lang_chat = get_user_language(session.challenger_id)
    edit_text = t(lang_chat, "cpc_accepted_both_choose", bet=session.bet, sign=COM_EMOJI)
    for uid in (session.challenger_id, session.opponent_id):
        lang = get_user_language(uid)
        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton(t(lang, "cpc_choice_rock"), callback_data=f"cpc_choice_{session_id}_rock"),
            InlineKeyboardButton(t(lang, "cpc_choice_scissors"), callback_data=f"cpc_choice_{session_id}_scissors"),
            InlineKeyboardButton(t(lang, "cpc_choice_paper"), callback_data=f"cpc_choice_{session_id}_paper"),
        )
        text = t(lang, "cpc_choose_pm", bet=session.bet, sign=COM_EMOJI, sec=CHOOSE_TIMEOUT_SEC)
        try:
            bot.send_message(uid, text, reply_markup=markup)
        except Exception as e:
            logger.warning("CPC: не удалось отправить ЛС игроку %s: %s", uid, e)
    try:
        bot.edit_message_text(edit_text, session.chat_id, session.challenge_message_id or 0)
    except Exception:
        pass


def _resolve_and_finish(session_id: int) -> None:
    """Определить победителя, обновить балансы, отправить результат в чат, удалить сессию."""
    bot = _get_bot()
    manager = _get_manager()
    session = manager.get(session_id)
    if not session:
        return
    add_coins = _deps["add_coins"]
    remove_coins = _deps["remove_coins"]
    get_user_mention = _deps["get_user_mention"]
    get_user_display_name = _deps.get("get_user_display_name")
    COM_EMOJI = _deps["COM_EMOJI"]
    save_game_result = _deps.get("save_game_result")
    get_user_language = _deps.get("get_user_language", lambda u: "ru")
    t = _deps.get("t", lambda lang, key, **kw: key)
    lang = get_user_language(session.challenger_id)

    c_choice = session.challenger_choice or "timeout_lose"
    o_choice = session.opponent_choice or "timeout_lose"
    winner_side = CPCManager.resolve_winner(c_choice, o_choice)
    challenger_id = session.challenger_id
    opponent_id = session.opponent_id
    chat_id = session.chat_id
    bet = session.bet

    def name(uid):
        if get_user_display_name:
            return get_user_display_name(chat_id, uid)
        return t(lang, "cpc_player")

    if winner_side == 1:
        winner_id = challenger_id
        loser_id = opponent_id
    elif winner_side == 2:
        winner_id = opponent_id
        loser_id = challenger_id
    else:
        winner_id = loser_id = None

    if winner_id is not None:
        if not remove_coins(loser_id, bet, "Проигрыш в КНБ"):
            logger.error("CPC: не удалось снять монеты с проигравшего %s", loser_id)
        add_coins(winner_id, bet * 2, "Выигрыш в КНБ (камень, ножницы, бумага)")
        if save_game_result:
            save_game_result(challenger_id, "cpc", bet, winner_id == challenger_id, bet if winner_id == challenger_id else -bet, {"session_id": session_id})
            save_game_result(opponent_id, "cpc", bet, winner_id == opponent_id, bet if winner_id == opponent_id else -bet, {"session_id": session_id})
    else:
        add_coins(challenger_id, bet, "Возврат ставки КНБ (ничья)")
        add_coins(opponent_id, bet, "Возврат ставки КНБ (ничья)")
        if save_game_result:
            save_game_result(challenger_id, "cpc", bet, False, 0, {"session_id": session_id, "draw": True})
            save_game_result(opponent_id, "cpc", bet, False, 0, {"session_id": session_id, "draw": True})

    def choice_display(choice: str) -> str:
        if choice == "timeout_lose":
            return t(lang, "cpc_timeout_no_choice")
        return CHOICE_EMOJI.get(choice, "❓") + " " + t(lang, CHOICE_KEYS.get(choice, "cpc_choice_rock"))
    line1 = f"👤 {get_user_mention(challenger_id, name(challenger_id))} — {choice_display(c_choice)}"
    line2 = f"👤 {get_user_mention(opponent_id, name(opponent_id))} — {choice_display(o_choice)}"

    if winner_id is not None:
        result_text = t(lang, "cpc_winner", mention=get_user_mention(winner_id, name(winner_id)), amount=bet * 2, sign=COM_EMOJI)
    else:
        result_text = t(lang, "cpc_draw")

    full_text = t(lang, "cpc_result_header") + "\n" + line1 + "\n" + line2 + "\n\n" + result_text
    try:
        bot.send_message(chat_id, full_text, parse_mode="Markdown")
    except Exception as e:
        logger.warning("CPC: не удалось отправить результат в чат %s: %s", chat_id, e)

    manager.remove_session(session_id)
    logger.info("🎮 CPC #%s завершена, победитель: %s", session_id, winner_id)


def _callback_cpc(call) -> None:
    """Обработчик callback: cpc_accept_N, cpc_decline_N, cpc_choice_N_rock|scissors|paper."""
    bot = _get_bot()
    manager = _get_manager()
    data = (call.data or "").strip()
    if not data.startswith("cpc_"):
        return
    parts = data.split("_")
    if len(parts) < 3:
        lang = _deps.get("get_user_language", lambda u: "ru")(call.from_user.id)
        t = _deps.get("t", lambda lang, key, **kw: key)
        bot.answer_callback_query(call.id, t(lang, "cpc_err_data"))
        return
    action = parts[1]
    try:
        session_id = int(parts[2])
    except (ValueError, IndexError):
        lang = _deps.get("get_user_language", lambda u: "ru")(call.from_user.id)
        t = _deps.get("t", lambda lang, key, **kw: key)
        bot.answer_callback_query(call.id, t(lang, "cpc_err_data"))
        return

    session = manager.get(session_id)
    user_id = call.from_user.id
    lang = _deps.get("get_user_language", lambda u: "ru")(user_id)
    t = _deps.get("t", lambda lang, key, **kw: key)

    if action == "accept":
        if not session:
            bot.answer_callback_query(call.id, t(lang, "cpc_err_not_found"))
            return
        ok, err_key, err_kw = manager.accept(session_id, user_id)
        msg = t(lang, "cpc_accept_ok") if ok else t(lang, err_key, **(err_kw or {}))
        bot.answer_callback_query(call.id, msg)
        if ok:
            _on_cpc_accepted(session_id)
        return

    if action == "decline":
        if not session:
            bot.answer_callback_query(call.id, t(lang, "cpc_err_not_found"))
            return
        ok, err_key, err_kw = manager.decline(session_id, user_id)
        msg = t(lang, "cpc_decline_ok") if ok else t(lang, err_key, **(err_kw or {}))
        bot.answer_callback_query(call.id, msg)
        if ok:
            try:
                bot.edit_message_text(t(lang, "cpc_decline_ok"), call.message.chat.id, call.message.message_id)
            except Exception:
                pass
        return

    if action == "choice":
        choice = parts[3] if len(parts) > 3 else None
        if choice not in CHOICES:
            bot.answer_callback_query(call.id, t(lang, "cpc_err_invalid_choice"))
            return
        if not session:
            bot.answer_callback_query(call.id, t(lang, "cpc_err_not_found"))
            return
        ok, err_key, err_kw, round_complete = manager.set_choice(session_id, user_id, choice)
        msg = t(lang, "cpc_choice_ok", choice=t(lang, CHOICE_KEYS.get(choice, "cpc_choice_rock"))) if ok else t(lang, err_key, **(err_kw or {}))
        bot.answer_callback_query(call.id, msg)
        if round_complete:
            _resolve_and_finish(session_id)
    else:
        bot.answer_callback_query(call.id, t(lang, "cpc_err_unknown_action"))


def run_cpc_cleanup() -> None:
    """Вызывать периодически: очистка истёкших принятий и таймаутов выбора с выплатами."""
    manager = _get_manager()
    manager.cleanup_accept_timeouts()
    for sid in manager.cleanup_choose_timeouts():
        _resolve_and_finish(sid)


def register_cpc_handlers(
    bot_instance,
    get_balance: Callable[[int], int],
    add_coins: Callable[..., bool],
    remove_coins: Callable[..., bool],
    get_user_mention: Callable[..., str],
    COM_EMOJI: str,
    ensure_user_access: Optional[Callable] = None,
    require_group_feature: Optional[Callable] = None,
    get_user_display_name: Optional[Callable[[int, int], str]] = None,
    get_user_language: Optional[Callable[[int], str]] = None,
    t: Optional[Callable] = None,
    save_game_result: Optional[Callable] = None,
    cpc_min_bet: int = 10,
    cpc_max_bet: int = 100000,
) -> None:
    """
    Регистрирует обработчики команд и callback для игры КНБ.
    save_game_result(user_id, game, bet, won, profit, details) — опционально для записи в историю игр.
    """
    global _deps
    _deps = {
        "bot": bot_instance,
        "get_balance": get_balance,
        "add_coins": add_coins,
        "remove_coins": remove_coins,
        "get_user_mention": get_user_mention,
        "get_user_display_name": get_user_display_name,
        "COM_EMOJI": COM_EMOJI,
        "ensure_user_access": ensure_user_access,
        "require_group_feature": require_group_feature,
        "get_user_language": get_user_language or (lambda u: "ru"),
        "t": t or (lambda lang, key, **kw: key),
        "save_game_result": save_game_result,
        "cpc_min_bet": cpc_min_bet,
        "cpc_max_bet": cpc_max_bet,
    }

    def _is_cpc_command_message(message) -> bool:
        """Fallback: /cpc, /кнб, /knb и варианты с @bot, невидимыми символами, другой раскладкой."""
        if getattr(message, "content_type", None) != "text" or not getattr(message, "text", None):
            return False
        text = (message.text or "").strip()
        if not text.startswith("/"):
            return False
        name = None
        if _extract_slash_command:
            name = _extract_slash_command(text)
        if not name:
            token = text.split()[0].split("@", 1)[0]
            if len(token) > 1 and token.startswith("/"):
                name = token[1:]
        if not name:
            return False
        return _normalize_bot_command_name(name) in _CPC_CMD_ALIASES

    def _cmd_cpc_safe(message) -> None:
        try:
            _cmd_cpc(message)
        except Exception as exc:
            logger.exception("CPC /cpc /кнб handler failed: %s", exc)

    # Сначала стандартный filters.commands — совпадает с Telegram и поддерживает Unicode.
    bot_instance.message_handler(commands=["cpc", "кнб", "knb"])(_cmd_cpc_safe)
    # Запасной путь, если extract_command/clients ведут себя иначе.
    bot_instance.message_handler(func=_is_cpc_command_message)(_cmd_cpc_safe)
    bot_instance.message_handler(commands=["cpc_accept"])(_cmd_accept)
    bot_instance.message_handler(commands=["cpc_decline"])(_cmd_decline)
    bot_instance.message_handler(commands=["cpc_cancel"])(_cmd_cancel_game)
    bot_instance.callback_query_handler(func=lambda c: (c.data or "").startswith("cpc_"))(_callback_cpc)
    logger.info("🎮 CPC (камень, ножницы, бумага) handlers registered")
