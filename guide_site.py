# -*- coding: utf-8 -*-
"""
Полные гайды по командам (RU/EN) на домене бота: /commands, /commands/en.
Исходники по умолчанию: telegraph_guide_ru.md, telegraph_guide_en.md (в корне проекта).
Опционально: переопределение текста в settings.json — ключи guides_markdown_override_ru / guides_markdown_override_en.

Редактор для разработчика: GET/POST /commands/edit (секрет GUIDES_EDIT_SECRET в .env).
"""
from __future__ import annotations

import html as html_lib
import re
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from flask import Flask

# URL-пути (без базового WEBHOOK_URL)
COMMANDS_GUIDE_PATH_RU = "/commands"
COMMANDS_GUIDE_PATH_EN = "/commands/en"
COMMANDS_EDIT_PATH = "/commands/edit"

_BASE = Path(__file__).resolve().parent
GUIDE_FILE_RU = _BASE / "telegraph_guide_ru.md"
GUIDE_FILE_EN = _BASE / "telegraph_guide_en.md"
_BOT_PY = _BASE / "bot.py"


def _read_bot_version_from_bot_py() -> Optional[str]:
    """Версия из bot.py на диске — совпадает с выкладкой на сервер даже до рестарта процесса."""
    try:
        with _BOT_PY.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if s.startswith("BOT_VERSION"):
                    m = re.match(r'^BOT_VERSION\s*=\s*["\']([^"\']+)["\']', s)
                    if m:
                        return m.group(1).strip()
    except OSError:
        pass
    return None


def _resolve_bot_version_for_page(bot: Any) -> str:
    v_file = _read_bot_version_from_bot_py()
    if v_file:
        return v_file
    try:
        import sys

        mod = sys.modules.get("bot")
        if mod is not None:
            v = getattr(mod, "BOT_VERSION", None)
            if v is not None:
                return str(v).strip()
    except Exception:
        pass
    v = getattr(bot, "BOT_VERSION", None)
    if v is not None:
        return str(v).strip()
    return "?"


def _md_line_to_html(line: str) -> str:
    line = html_lib.escape(line)
    line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
    line = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
    line = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", line)
    line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
    return line


def md_guide_to_html_fragment(md_text: str) -> str:
    """Markdown гайда → HTML-фрагмент (тот же упрощённый синтаксис, что в .md файлах)."""
    lines = (md_text or "").strip().split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("### "):
            out.append(f"<h4>{_md_line_to_html(stripped[4:])}</h4>")
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(f"<h4>{_md_line_to_html(stripped[3:])}</h4>")
            i += 1
            continue
        if stripped.startswith("# "):
            out.append(f"<h3>{_md_line_to_html(stripped[2:])}</h3>")
            i += 1
            continue
        if stripped == "---":
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith("- ") and not stripped.startswith("- **"):
            out.append(f"<p>• {_md_line_to_html(stripped[2:])}</p>")
            i += 1
            continue
        if re.match(r"^- \*\*", stripped):
            out.append(f"<p>• {_md_line_to_html(stripped[2:])}</p>")
            i += 1
            continue
        if not stripped:
            i += 1
            continue
        out.append(f"<p>{_md_line_to_html(stripped)}</p>")
        i += 1
    return "\n".join(out)


def load_guide_markdown(lang: str, settings_dict: Optional[Dict[str, Any]]) -> str:
    lang = (lang or "ru").lower()
    s = settings_dict or {}
    if lang == "en":
        ov = (s.get("guides_markdown_override_en") or "").strip()
        path = GUIDE_FILE_EN
    else:
        ov = (s.get("guides_markdown_override_ru") or "").strip()
        path = GUIDE_FILE_RU
    if ov:
        return ov
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return f"# {'Guide' if lang == 'en' else 'Гайд'}\n\nФайл {path.name} не найден на сервере."


def build_guide_shell(
    *,
    inner_html: str,
    lang: str,
    page_title: str,
    site_title: str,
    version: str,
    url_ru: str,
    url_en: str,
    tme_url: str,
) -> str:
    """Оболочка HTML: минималистичная витрина, навигация RU/EN."""
    lang = (lang or "ru").lower()
    is_ru = lang == "ru"
    nav_ru_cls = "nav-pill active" if is_ru else "nav-pill"
    nav_en_cls = "nav-pill active" if not is_ru else "nav-pill"
    html_lang = "ru" if is_ru else "en"
    open_tg = "Открыть бота в Telegram" if is_ru else "Open bot in Telegram"
    sub = "Полное руководство по командам" if is_ru else "Complete command reference"
    return f"""<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="color-scheme" content="dark light"/>
<title>{page_title}</title>
<style>
:root {{
  --bg: #0c0e12;
  --surface: #141820;
  --elevated: #1a202c;
  --text: #e8eaef;
  --muted: #8b95a8;
  --accent: #5b8def;
  --accent-dim: rgba(91, 141, 239, 0.15);
  --border: rgba(255,255,255,0.07);
  --radius: 14px;
  --font: "SF Pro Text", system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.65;
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
}}
.topbar {{
  position: sticky; top: 0; z-index: 10;
  background: linear-gradient(180deg, rgba(12,14,18,0.97) 0%, rgba(12,14,18,0.88) 100%);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
  padding: 0.75rem 1rem;
}}
.top-inner {{
  max-width: 720px; margin: 0 auto;
  display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1rem;
  justify-content: space-between;
}}
.brand {{
  font-weight: 700; font-size: 0.95rem; letter-spacing: -0.02em;
  color: var(--text); text-decoration: none;
}}
.brand span {{ color: var(--muted); font-weight: 500; font-size: 0.8rem; }}
.nav {{
  display: flex; gap: 0.4rem; align-items: center;
}}
.nav-pill {{
  padding: 0.35rem 0.85rem; border-radius: 999px; font-size: 0.8rem; font-weight: 600;
  text-decoration: none; color: var(--muted); border: 1px solid transparent;
  transition: color .15s, background .15s, border-color .15s;
}}
.nav-pill:hover {{ color: var(--text); background: var(--accent-dim); }}
.nav-pill.active {{
  color: var(--accent); background: var(--accent-dim); border-color: rgba(91,141,239,0.35);
}}
.hero {{
  max-width: 720px; margin: 0 auto; padding: 1.75rem 1rem 0.5rem;
}}
.hero h1 {{
  font-size: 1.15rem; font-weight: 600; margin: 0 0 0.35rem; letter-spacing: -0.02em;
  color: var(--text);
}}
.hero p {{
  margin: 0; font-size: 0.88rem; color: var(--muted);
}}
.tg-link {{
  display: inline-flex; margin-top: 1rem; align-items: center; gap: 0.4rem;
  font-size: 0.85rem; font-weight: 600; color: var(--accent);
  text-decoration: none;
}}
.tg-link:hover {{ text-decoration: underline; }}
main {{
  max-width: 720px; margin: 0 auto; padding: 0 1rem 3rem;
}}
.guide-body {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.35rem 1.25rem 1.75rem;
  margin-top: 1rem;
  box-shadow: 0 4px 24px rgba(0,0,0,0.25);
}}
.guide-body h3 {{
  font-size: 1.05rem; margin: 1.35rem 0 0.6rem; font-weight: 650;
  color: var(--text); letter-spacing: -0.02em;
}}
.guide-body h3:first-child {{ margin-top: 0; }}
.guide-body h4 {{
  font-size: 0.92rem; margin: 1rem 0 0.45rem; font-weight: 600; color: #c5cad6;
}}
.guide-body p {{ margin: 0.5rem 0; color: #c8cdd8; font-size: 0.92rem; }}
.guide-body code {{
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.84em;
  background: var(--elevated); padding: 0.12rem 0.4rem; border-radius: 6px;
  color: #a8c7ff; border: 1px solid var(--border);
}}
.guide-body hr {{
  border: none; border-top: 1px solid var(--border); margin: 1.25rem 0;
}}
footer {{
  max-width: 720px; margin: 2rem auto 0; padding: 0 1rem 2rem;
  font-size: 0.75rem; color: var(--muted); text-align: center;
}}
@media (min-width: 640px) {{
  .guide-body {{ padding: 1.75rem 2rem 2rem; }}
  body {{ font-size: 17px; }}
}}
</style>
</head>
<body>
<header class="topbar">
  <div class="top-inner">
    <a class="brand" href="/">{site_title} <span>· {sub}</span></a>
    <nav class="nav" aria-label="Language">
      <a class="{nav_ru_cls}" href="{url_ru}">RU</a>
      <a class="{nav_en_cls}" href="{url_en}">EN</a>
    </nav>
  </div>
</header>
<div class="hero">
  <h1>{page_title}</h1>
  <p>{sub} · v{version}</p>
  <a class="tg-link" href="{tme_url}" target="_blank" rel="noopener">{open_tg} →</a>
</div>
<main>
  <article class="guide-body">
{inner_html}
  </article>
</main>
<footer>{site_title} · v{version}</footer>
</body>
</html>"""


def _tme_url(bot: Any) -> str:
    try:
        me = bot.bot.get_me()
        u = (me.username or "").strip()
        if u and all((c.isalnum() or c == "_") for c in u):
            return "https://t.me/" + u
    except Exception:
        pass
    return "https://t.me"


def render_commands_page(bot: Any, lang: str) -> str:
    """Полная HTML-страница гайда."""
    lang = (lang or "ru").lower()
    settings_dict = getattr(bot, "settings", {}) or {}
    md = load_guide_markdown(lang, settings_dict)
    inner = md_guide_to_html_fragment(md)
    base = (os.getenv("WEBHOOK_URL") or "").strip().rstrip("/")
    url_ru = (base + COMMANDS_GUIDE_PATH_RU) if base else COMMANDS_GUIDE_PATH_RU
    url_en = (base + COMMANDS_GUIDE_PATH_EN) if base else COMMANDS_GUIDE_PATH_EN
    ver = _resolve_bot_version_for_page(bot)
    _prof = getattr(bot, "telegram_display_name_for_profile", None)
    site_title = html_lib.escape(_prof() if callable(_prof) else "Bot")
    page_title = (
        "Команды бота — полный гайд"
        if lang == "ru"
        else "Bot commands — full guide"
    )
    page_title_esc = html_lib.escape(page_title)
    return build_guide_shell(
        inner_html=inner,
        lang=lang,
        page_title=page_title_esc,
        site_title=site_title,
        version=html_lib.escape(str(ver)),
        url_ru=html_lib.escape(url_ru, quote=True),
        url_en=html_lib.escape(url_en, quote=True),
        tme_url=html_lib.escape(_tme_url(bot), quote=True),
    )


def _editor_html(ru: str, en: str, msg: str = "") -> str:
    m = html_lib.escape(msg) if msg else ""
    notice = f'<p class="ok">{m}</p>' if msg else ""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Редактор гайда</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#0c0e12; color:#e8eaef; padding:1.5rem; max-width:900px; margin:0 auto; }}
h1 {{ font-size:1.1rem; }}
label {{ display:block; margin-top:1rem; font-size:0.85rem; color:#8b95a8; }}
textarea {{ width:100%; min-height:220px; background:#141820; color:#e8eaef; border:1px solid rgba(255,255,255,.1); border-radius:8px; padding:0.75rem; font-size:13px; }}
input[type=password] {{ width:100%; max-width:360px; padding:0.5rem; border-radius:8px; border:1px solid rgba(255,255,255,.12); background:#141820; color:#e8eaef; }}
button {{ margin-top:1rem; padding:0.6rem 1.2rem; border-radius:10px; border:none; background:#5b8def; color:#fff; font-weight:600; cursor:pointer; }}
.ok {{ color:#3dd68c; }}
.err {{ color:#f66; }}
.hint {{ font-size:0.8rem; color:#8b95a8; margin-top:0.5rem; }}
</style>
</head>
<body>
<h1>Редактор гайдов (RU / EN)</h1>
<p class="hint">Пустое поле = брать текст из файла <code>telegraph_guide_*.md</code> на сервере. После сохранения откройте /commands и /commands/en.</p>
{notice}
<form method="post" action="{COMMANDS_EDIT_PATH}">
<label>Russian (Markdown)</label>
<textarea name="ru_text">{html_lib.escape(ru)}</textarea>
<label>English (Markdown)</label>
<textarea name="en_text">{html_lib.escape(en)}</textarea>
<label>Секрет (GUIDES_EDIT_SECRET из .env)</label>
<input type="password" name="secret" autocomplete="off" placeholder="Секрет"/>
<button type="submit">Сохранить</button>
</form>
<p class="hint"><a href="/" style="color:#5b8def">← На главную панель</a> · <a href="{COMMANDS_GUIDE_PATH_RU}" style="color:#5b8def">Просмотр RU</a> · <a href="{COMMANDS_GUIDE_PATH_EN}" style="color:#5b8def">EN</a></p>
</body>
</html>"""


def register_guide_routes(app: "Flask", bot: Any) -> None:
    """Регистрирует маршруты гайда и редактора."""
    from flask import Response, request

    def guide_ru():
        html = render_commands_page(bot, "ru")
        return Response(html, mimetype="text/html; charset=utf-8")

    def guide_en():
        html = render_commands_page(bot, "en")
        return Response(html, mimetype="text/html; charset=utf-8")

    def guide_edit():
        if request.method == "GET":
            s = getattr(bot, "settings", {}) or {}
            ru = (s.get("guides_markdown_override_ru") or "")
            en = (s.get("guides_markdown_override_en") or "")
            if not ru.strip() and GUIDE_FILE_RU.is_file():
                ru = GUIDE_FILE_RU.read_text(encoding="utf-8")
            if not en.strip() and GUIDE_FILE_EN.is_file():
                en = GUIDE_FILE_EN.read_text(encoding="utf-8")
            msg = ""
            if not (os.getenv("GUIDES_EDIT_SECRET") or "").strip():
                msg = "Внимание: GUIDES_EDIT_SECRET не задан — сохранение отключено."
            return Response(_editor_html(ru, en, msg), mimetype="text/html; charset=utf-8")

        secret_env = (os.getenv("GUIDES_EDIT_SECRET") or "").strip()
        if not secret_env:
            return Response(
                _editor_html("", "", "Задайте GUIDES_EDIT_SECRET в .env на сервере."),
                mimetype="text/html; charset=utf-8",
                status=503,
            )
        sec = (request.form.get("secret") or "").strip()
        ru_new = request.form.get("ru_text")
        en_new = request.form.get("en_text")
        if ru_new is None:
            ru_new = ""
        if en_new is None:
            en_new = ""
        if sec != secret_env:
            return Response(_editor_html(ru_new, en_new, "Неверный секрет."), mimetype="text/html; charset=utf-8", status=403)
        s = dict(getattr(bot, "settings", {}) or {})
        s["guides_markdown_override_ru"] = ru_new.strip()
        s["guides_markdown_override_en"] = en_new.strip()
        bot.settings = s
        try:
            bot.save_settings(s)
        except Exception as e:
            return Response(_editor_html(ru_new, en_new, f"Ошибка сохранения: {e}"), mimetype="text/html; charset=utf-8", status=500)
        return Response(_editor_html(ru_new, en_new, "Сохранено. Страницы /commands обновлены."), mimetype="text/html; charset=utf-8")

    app.add_url_rule(COMMANDS_GUIDE_PATH_RU, "commands_guide_ru", guide_ru, methods=["GET"])
    app.add_url_rule(COMMANDS_GUIDE_PATH_EN, "commands_guide_en", guide_en, methods=["GET"])
    app.add_url_rule(COMMANDS_EDIT_PATH, "commands_guide_edit", guide_edit, methods=["GET", "POST"])
