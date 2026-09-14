"""``/admin_deploy`` — static reminder that deploy lives in the shell.

Legacy ``/deploy`` (bot.py:25438) returns a hardcoded message telling
the operator: *"deploy is a shell command, run it from the project
directory, not Telegram."* The point of the command is the absence of
behaviour — typing ``/deploy`` into the bot must NOT initiate a deploy.
A non-zero number of operators try it at least once; without this
handler they see "unknown command" and then guess (wrong) that the
deploy is wedged.

Renamed ``/deploy`` → ``/admin_deploy`` in the new pipeline. The
``/admin_*`` prefix is the strangler-pattern convention for everything
behind the developer-only gate. This paragraph used to end "the legacy
alias keeps working through the bridge until the legacy handler is
removed" — T-011 (2026-05-26) removed that bridge, and for four months
afterwards ``/deploy`` matched nothing at all. Not "unknown command":
nothing. The update was dropped and the operator got silence, which is
the exact outcome the paragraph above calls worse than a wrong card.

So the short name is registered here, on the same handler behind the
same developer gate. It widens no surface — a non-developer typing
either spelling is dropped in silence either way — and it restores the
one case this module exists for: the name an operator's fingers reach
for first has to answer.

The body is the whole handler, so the body being wrong is the whole
bug — and it was, in four places at once (#170). It named ``make
deploy`` (there is no ``Makefile``), ``./deploy_to_vps.sh`` (which
targets the legacy monolith's layout — a ``venv`` and a
``requirements.txt`` in a fixed directory — on a server that
no longer exists), a blue/green window (removed in #146), and
``CLI_DEPLOY.txt`` (same legacy vintage). Every route this card
offered led somewhere dead.

That is worse than a card that says nothing. An operator opens
``/admin_deploy`` precisely when they are unsure how to deploy, which
is exactly when they have the least ability to notice that the
instructions are four months stale — and one of those instructions was
a shell command that would happily run.

It now names exactly one path, ``scripts/deploy.sh``, which is the
real one: it refuses a dirty or unpushed tree, snapshots the databases
first, syncs the three trees the service is made of, stamps
``BUILD_INFO`` so ``/admin_status`` can report what actually shipped,
and waits for ``/healthz`` instead of assuming a restart worked — a
liveness probe, so it proves the process answers again, not that the
databases opened; the journal grep in the script's last stage is what
covers that. The card points at ``docs/DEPLOY.md`` for anything beyond
the happy path.

Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only at the router level. The body contains shell-command
hints (no credentials), but the existence-check still matters — we
don't want ``/admin_deploy`` to be a confirmation channel for
enumerating dev IDs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.deploy_hint")


_BODY = (
    "📤 <b>Deploy is a shell command — not a Telegram command.</b>\n"
    "\n"
    "Typing <code>/admin_deploy</code> here does NOT push to the server. "
    "It can't — by design. A bot that could redeploy itself from its "
    "own chat would be one stolen session away from a self-update RCE.\n"
    "\n"
    "From the project directory on the Mac:\n"
    "\n"
    "  • <code>./scripts/deploy.sh</code> — the whole path in one "
    "command: refuses a dirty or unpushed tree, snapshots the "
    "databases to the Mac first, syncs the code, stamps the revision, "
    "restarts, and waits for <code>/healthz</code>.\n"
    "  • <code>./scripts/deploy.sh --dry-run</code> — show what would "
    "move and change nothing. Also the fastest answer to <i>is prod "
    "behind?</i>\n"
    "\n"
    "Afterwards <code>/admin_status</code> shows the revision the box "
    "is actually running — compare it with <code>git log</code>. A "
    "⚠️ there means the deploy did not stamp it.\n"
    "\n"
    "Anything unusual (migrations, dependency changes, rollback, "
    "restoring a database) is in <code>docs/DEPLOY.md</code>.\n"
    "\n"
    "⚠️ The scripts in the repo root — <code>deploy_to_vps.sh</code>, "
    "<code>deploy_to_server.py</code>, <code>CLI_DEPLOY.txt</code> — "
    "belong to the legacy monolith and point at a server that is gone. "
    "Do not run them."
)


async def handle_admin_deploy(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_deploy; silently dropped"
        )
        return
    await message.answer(_BODY)
    log.bind(user_id=user.id).info("/admin_deploy hint rendered")


def build_router(settings: Settings) -> Router:
    """Private-only at the router level — the body is harmless but
    matches the rest of the admin tree. An operator who fat-fingers
    ``/admin_deploy`` into a group shouldn't see "deploy" appear in
    front of regular members; even an inert hint is noise.
    """
    router = Router(name="admin.deploy_hint")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_deploy(message, settings)

    router.message.register(
        # ``deploy`` is the legacy short name, kept because this handler's
        # whole job is to answer it (module docstring). ``ignore_case``
        # covers the ``/Deploy`` an operator types on a phone keyboard.
        _entry,
        Command("admin_deploy", "deploy", ignore_case=True),
    )
    return router
