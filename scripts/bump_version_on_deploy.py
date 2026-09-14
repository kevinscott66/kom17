#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Перед rsync на VPS: увеличивает patch-версию в bot.py и строку «Версия» в README.md.
Вызывается из deploy_to_vps.sh (отключить: SKIP_VERSION_BUMP=1 ./deploy_to_vps.sh).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_PY = ROOT / "bot.py"
README = ROOT / "README.md"


def main() -> int:
    if not BOT_PY.is_file():
        print("bot.py not found", file=sys.stderr)
        return 1
    text = BOT_PY.read_text(encoding="utf-8")
    m = re.search(r"^BOT_VERSION\s*=\s*[\"']([0-9]+)\.([0-9]+)\.([0-9]+)[\"']", text, re.MULTILINE)
    if not m:
        print("BOT_VERSION not found in bot.py", file=sys.stderr)
        return 1
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    new_ver = f"{major}.{minor}.{patch + 1}"
    text2, n = re.subn(
        r"^BOT_VERSION\s*=\s*[\"'][0-9]+\.[0-9]+\.[0-9]+[\"']",
        f'BOT_VERSION = "{new_ver}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        print("Failed to replace BOT_VERSION", file=sys.stderr)
        return 1
    BOT_PY.write_text(text2, encoding="utf-8")

    if README.is_file():
        rtxt = README.read_text(encoding="utf-8")
        r2, n2 = re.subn(
            r"(\*\*Версия:\*\*\s*)[0-9]+\.[0-9]+\.[0-9]+",
            rf"\g<1>{new_ver}",
            rtxt,
            count=1,
        )
        if n2:
            README.write_text(r2, encoding="utf-8")

    print(f"Bump: версия приложения → {new_ver} (bot.py, README)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
