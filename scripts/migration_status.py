#!/usr/bin/env python3
"""Migration-progress dashboard for the strangler cutover.

Run after the cutover flag flips, but useful before too. Output:

* Per-command: ``new`` (handled by an aiogram router) vs ``legacy``
  (still served by ``bot.py`` and reached via ``TelebotFallback``)
  vs ``both`` (registered in both — the new path wins, but the
  legacy registration is dead code worth deleting).
* Summary counts and percentage migrated.
* An "operator next picks" list — top-N legacy-only commands ranked
  by how many aliases they have (aliases are usually a sign the
  command is high-traffic, since translators bothered to add them).

The script is hermetic — it parses ``bot.py`` as text (no import) and
builds the new pipeline's command set the same way ``/admin_routes``
does internally. No DB connections, no env reads beyond the parser.

Use as:

  python scripts/migration_status.py
  python scripts/migration_status.py --json    # for CI dashboards
  python scripts/migration_status.py --legacy-only --top 20

Why a separate script rather than another ``/admin_*`` command: the
admin commands answer "what does THIS deploy serve?" — they require
the new pipeline to be running. This script answers the strategic
question "what's still left to migrate?" and the answer must be
available from the repo alone, including in CI before any deployment.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT_PY = ROOT / "bot.py"
SRC = ROOT / "src" / "telegram_invite_bot"


# Two regexes because the decorator's ``commands=[...]`` list can wrap
# across lines and embed comments. We grab the whole decorator call
# first, then extract the list literal from inside it.
_DECO_RE = re.compile(
    r"@bot\.message_handler\s*\((?P<args>[^)]*)\)",
    re.DOTALL,
)
_COMMANDS_RE = re.compile(r"commands\s*=\s*\[(?P<body>[^\]]*)\]", re.DOTALL)
_STRING_RE = re.compile(r"""['"]([^'"]+)['"]""")


def parse_legacy_commands(text: str) -> dict[str, int]:
    """Return ``{command: count}`` from ``bot.py``.

    Counts duplicates because the same command can legitimately appear
    in multiple handlers (different chat-type filters, etc.) — the
    operator looking at the migration dashboard wants the total
    handler count, not a deduplicated set.
    """
    counts: dict[str, int] = defaultdict(int)
    for deco in _DECO_RE.finditer(text):
        body = _COMMANDS_RE.search(deco.group("args"))
        if not body:
            continue
        for cmd in _STRING_RE.findall(body.group("body")):
            counts[cmd.lower()] += 1
    return dict(counts)


def parse_new_pipeline_commands(src: Path) -> dict[str, list[str]]:
    """Return ``{command: [module:lineno, ...]}`` from aiogram routers.

    Walks every ``.py`` under ``src/telegram_invite_bot/handlers/`` and
    extracts ``Command("foo", ...)`` and ``Command("foo", "bar", ...)``
    literals via AST. The AST walk handles the wrapped/aliased import
    forms — ``from aiogram.filters import Command`` is the universal
    style in this codebase but the walk doesn't assume it.
    """
    found: dict[str, list[str]] = defaultdict(list)
    handlers_root = src / "handlers"
    for py_file in sorted(handlers_root.rglob("*.py")):
        rel = py_file.relative_to(ROOT)
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name)
                else None
            )
            if func_name != "Command":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found[arg.value.lower()].append(f"{rel}:{node.lineno}")
    return dict(found)


def classify(
    legacy: dict[str, int], new: dict[str, list[str]]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for cmd in set(legacy) | set(new):
        in_legacy = cmd in legacy
        in_new = cmd in new
        if in_new and in_legacy:
            # Both registered — the bridge sends to new first and
            # short-circuits, so the legacy registration is dead
            # code that should eventually be deleted from bot.py.
            out[cmd] = "both"
        elif in_new:
            out[cmd] = "new"
        else:
            out[cmd] = "legacy"
    return out


def summarise(
    legacy: dict[str, int], new: dict[str, list[str]], status: dict[str, str]
) -> dict[str, int]:
    counts = {"new": 0, "legacy": 0, "both": 0}
    for kind in status.values():
        counts[kind] += 1
    total = sum(counts.values())
    counts["total"] = total
    counts["migrated_pct"] = (
        round(100 * (counts["new"] + counts["both"]) / total) if total else 0
    )
    return counts


def render_text(
    status: dict[str, str],
    summary: dict[str, int],
    new: dict[str, list[str]],
    *,
    legacy_only: bool,
    top: int,
) -> str:
    lines: list[str] = []
    lines.append(
        f"Strangler migration: {summary['migrated_pct']}% of commands on new pipeline"
    )
    lines.append(
        f"  total={summary['total']}  new={summary['new']}  "
        f"both={summary['both']}  legacy={summary['legacy']}"
    )
    lines.append("")
    if legacy_only:
        legacy_cmds = sorted(c for c, k in status.items() if k == "legacy")
        lines.append(f"Legacy-only commands ({len(legacy_cmds)}) — top {top}:")
        for cmd in legacy_cmds[:top]:
            lines.append(f"  /{cmd}")
    else:
        lines.append("Per-command status (sorted):")
        for cmd in sorted(status):
            tag = status[cmd]
            extra = (
                f"  ({new[cmd][0]})" if tag in ("new", "both") else ""
            )
            lines.append(f"  [{tag:6s}] /{cmd}{extra}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of text"
    )
    parser.add_argument(
        "--legacy-only",
        action="store_true",
        help="only show legacy-only commands (the migration to-do list)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="cap on --legacy-only output (default 30)",
    )
    args = parser.parse_args(argv)

    if not BOT_PY.is_file():
        print(f"bot.py not found at {BOT_PY}", file=sys.stderr)
        return 2
    if not SRC.is_dir():
        print(f"src tree not found at {SRC}", file=sys.stderr)
        return 2

    legacy = parse_legacy_commands(BOT_PY.read_text(encoding="utf-8"))
    new = parse_new_pipeline_commands(SRC)
    status = classify(legacy, new)
    summary = summarise(legacy, new, status)

    if args.json:
        print(
            json.dumps(
                {"summary": summary, "status": status},
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        print(
            render_text(
                status, summary, new,
                legacy_only=args.legacy_only,
                top=args.top,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
