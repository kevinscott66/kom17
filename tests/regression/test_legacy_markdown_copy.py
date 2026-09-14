"""Ported legacy copy still carries telebot-era Markdown (#708).

``di/providers.py`` sets ``ParseMode.HTML`` bot-wide, but ~1500 of the
i18n values were lifted verbatim out of the legacy ``translations.py``,
where the same strings went out with ``parse_mode="Markdown"``.
``tests/unit/i18n/test_legacy_parity.py`` byte-locks those values against
``translations.py``, so the markers cannot simply be edited out of the
YAML — they have to be translated at render time by
:func:`telegram_invite_bot.utils.html.legacy_md_to_html`.

The audit that filed #708 found six such keys; the AST sweep below found
nineteen. This module pins the sweep so the next one cannot appear
unnoticed: a handler that starts rendering a nineteenth Markdown-bearing
legacy key fails here and has to decide, explicitly, whether to wrap it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

_SRC = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"

# Keys the new pipeline authored itself; they are HTML by construction
# and never went through the Markdown era.
_NEW_PIPELINE_PREFIXES = ("h_", "site_")

# The three constructs ``legacy_md_to_html`` translates. Kept in step
# with that function deliberately: this test is about the copy, so it
# must recognise exactly what the converter recognises.
_MD = re.compile(r"\*\*|`|(?<![\w*])_[^_\n]{1,60}_(?![\w])")

# Every ported key that still ships Markdown AND is actually rendered by
# ``src/``. Frozen on purpose — see the module docstring.
_MARKDOWN_KEYS: frozenset[str] = frozenset(
    {
        "forecast_city_prompt",
        "lang_select",
        "marry_activity_title",
        "p2p_express_enter_fiat",
        "p2p_express_no_orders",
        "p2p_express_result",
        "p2p_express_seller_notify",
        "p2p_express_trade_line",
        "p2p_limits_optional_hint",
        "p2p_trade_important",
        "pvp_coin_usage",
        "pvp_dice_usage",
        "rel_activity_title",
        "rel_rp_commands_no_rel",
        "rel_rp_commands_private",
        "rel_rp_commands_your_level",
        "rp18_prompt_text",
        "rp_vip_outside",
        "weather_city_prompt",
    }
)


def _translations(lang: str) -> dict[str, str]:
    return dict(yaml.safe_load((_SRC / "i18n" / "data" / f"{lang}.yaml").read_text()))


def _t_keys_by_module() -> dict[Path, set[str]]:
    """Literal first arguments of every ``t(...)`` call, per source file.

    Only literals are visible to a static sweep; ``t(usage_key, lang)``
    in ``handlers/pvp_stake.py`` is one such indirection, which is why
    ``pvp_coin_usage`` and ``pvp_dice_usage`` reach the frozen set above
    by way of the ``usage_key="…"`` keyword rather than a direct ``t``
    argument. A sweep of every string constant in ``src/`` that happens to
    name a Markdown-bearing key finds nothing beyond these two forms:
    ``core/achievements.py`` holds a ``"daily_streak"`` that is a stat name
    colliding with an unrendered legacy key, not a ``t`` lookup.
    """
    found: dict[Path, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        keys: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            args: list[ast.expr] = []
            if isinstance(node.func, ast.Name) and node.func.id == "t" and node.args:
                args.append(node.args[0])
            args.extend(kw.value for kw in node.keywords if kw.arg == "usage_key")
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
        if keys:
            found[path] = keys
    return found


def test_the_set_of_markdown_bearing_legacy_keys_is_pinned() -> None:
    ru = _translations("ru")
    en = _translations("en")
    used: set[str] = set()
    for keys in _t_keys_by_module().values():
        used |= keys

    carrying = {
        key
        for key in used
        if not key.startswith(_NEW_PIPELINE_PREFIXES)
        and (_MD.search(ru.get(key, "")) or _MD.search(en.get(key, "")))
    }
    assert carrying == set(_MARKDOWN_KEYS), (
        "new Markdown-bearing legacy copy reached a handler: "
        f"added={sorted(carrying - _MARKDOWN_KEYS)} "
        f"gone={sorted(_MARKDOWN_KEYS - carrying)}"
    )


def test_every_module_rendering_such_a_key_imports_the_converter() -> None:
    """Proxy for "the key is wrapped".

    A per-call-site check would need dataflow; module-level import is the
    honest approximation — it cannot prove the wrapper is applied to THIS
    key, but it does catch a module that renders Markdown copy while
    having no way to convert it at all.
    """
    offenders: list[str] = []
    for path, keys in _t_keys_by_module().items():
        if not (keys & _MARKDOWN_KEYS):
            continue
        if "legacy_md_to_html" not in path.read_text():
            offenders.append(str(path.relative_to(_SRC)))
    assert not offenders, f"renders legacy Markdown without the converter: {offenders}"
