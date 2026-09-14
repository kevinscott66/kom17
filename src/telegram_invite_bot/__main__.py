"""Entry point: ``python -m telegram_invite_bot --mode={polling,webhook}``.

* ``polling`` — local dev; uses aiogram long-polling. Do not run against
  the production token. The legacy webhook this line used to name is
  gone (T-011), but the new one is live and is the only thing prod has:
  Telegram refuses ``getUpdates`` while a webhook is set, so polling the
  prod token means taking prod's webhook down to do it.
* ``webhook`` — production; FastAPI + uvicorn + Telegram setWebhook on
  startup. Defaults to bind ``HOST:PORT`` from settings.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def _async_main(mode: str) -> int:
    if mode == "polling":
        from telegram_invite_bot.runner.polling import run as run_polling

        await run_polling()
    elif mode == "webhook":
        from telegram_invite_bot.runner.webhook import run as run_webhook

        await run_webhook()
    else:  # pragma: no cover — argparse already validated
        raise ValueError(f"unknown mode: {mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram_invite_bot")
    parser.add_argument(
        "--mode",
        choices=("polling", "webhook"),
        default="polling",
        help="Run mode (default: polling).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args.mode))


if __name__ == "__main__":
    sys.exit(main())
