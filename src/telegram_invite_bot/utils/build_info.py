"""Which commit is this process actually running?

Nothing in the running bot knew. ``/admin_status`` reported
``telegram_invite_bot.__version__``, a string literal that has read
``0.1.0`` since the first commit, so the one card whose job is "is the
pipeline healthy" answered the question "am I running the code you
pushed?" with the same six characters whether prod was current or four
days behind.

That is not hypothetical. On 2026-08-19 prod was still raising on
``/admin_help`` and ``/admin_routes`` (the 4096-char fix landed
2026-08-18) and on rating-page taps (the caption-edit fallback landed
2026-08-16). The unit even restarted at 20:10 that evening without
picking any of it up, and no surface anywhere — chat, log line, metric
— said so. The bugs looked unfixed; the fixes looked undeployed; only
an ``rsync -n`` from the dev checkout could tell the two apart.

So the deploy stamps what it shipped and the bot reads it back.

Deliberately a **file written by the deploy**, not something derived at
import time:

* Prod has no git checkout — the service directory receives an
  ``rsync`` of three trees, so there is no revision to interrogate.
* Shelling out to ``git`` from a request path would be wrong even where
  it would work: a subprocess per card render, and a value that
  describes the *dev* checkout rather than the box's code.

The file is optional and every field in it is optional. A missing or
unreadable stamp reports as unknown rather than raising or inventing a
value — a status card that lies about the revision is worse than one
that admits it does not know, because only the first kind gets trusted.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from loguru import logger

log = logger.bind(component="utils.build_info")

#: Name of the stamp, written by ``scripts/deploy.sh`` into the service's
#: ``WorkingDirectory``. It sits next
#: to ``.env`` rather than inside ``src/`` on purpose: the code trees are
#: synced with ``rsync --delete``, which would erase a stamp placed under
#: them on the very next deploy.
BUILD_INFO_FILENAME = "BUILD_INFO"

#: Read ceiling. The stamp is four short lines; anything larger is a
#: wrong file at the right path, and a card is not the place to find
#: that out by rendering 2 MB of it.
_MAX_BYTES = 4096

#: Per-value ceiling, applied after parsing. Same reasoning one level
#: down: a plausible-looking file with one absurd value must not be able
#: to push the status card past Telegram's own limit.
_MAX_VALUE_LEN = 120

_KNOWN_KEYS = frozenset({"revision", "committed_at", "deployed_at", "deployed_by"})


class BuildInfo(NamedTuple):
    """What the deploy claims it shipped.

    Every field is what the file said, not what the code is — this is a
    record of an assertion made at deploy time, and it is only as honest
    as the deploy that wrote it. That is precisely why
    ``scripts/deploy.sh`` writes it in the same command that runs the
    ``rsync``: the two cannot drift if neither can happen without the
    other.
    """

    revision: str
    committed_at: str | None
    deployed_at: str | None
    deployed_by: str | None


def default_root() -> Path:
    """The service's working directory, derived from this file's path.

    ``src/telegram_invite_bot/utils/build_info.py`` → up four parents is
    the tree root: the service directory in production, and the
    repo checkout in development. Derived rather than configured because a
    setting for it would be one more thing a deploy could get wrong, and
    the layout has been stable since the cut-over.
    """
    return Path(__file__).resolve().parents[3]


def _truncate(value: str) -> str:
    return value if len(value) <= _MAX_VALUE_LEN else value[: _MAX_VALUE_LEN - 1] + "…"


def read_build_info(root: Path | None = None) -> BuildInfo | None:
    """Parse the deploy stamp, or ``None`` when there isn't a usable one.

    ``None`` covers every failure the same way — absent, unreadable,
    malformed, no ``revision`` line — because the caller's decision is
    identical in all of them: say "unknown" out loud. Distinguishing
    "no file" from "bad file" would only matter to someone already
    ssh'd into the box, who can just read it.

    Never raises. This is called from a card render, and an operator
    reaching for ``/admin_status`` during an incident must not be met
    with an error because a stamp file has the wrong permissions.
    """
    path = (root or default_root()) / BUILD_INFO_FILENAME
    try:
        # Bounded read, not read-then-slice: ``read_bytes()`` would pull
        # the whole file into memory first and only then discard the
        # tail, which is exactly what ``_MAX_BYTES`` promises not to do.
        with path.open("rb") as fh:
            raw = fh.read(_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None

    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        if key in _KNOWN_KEYS:
            values[key] = _truncate(value.strip())

    revision = values.get("revision", "")
    if not revision:
        # A stamp without a revision answers nothing, so it is not a
        # stamp. Reported as unknown rather than as an empty revision,
        # which would render as a confident-looking blank.
        return None
    return BuildInfo(
        revision=revision,
        committed_at=values.get("committed_at"),
        deployed_at=values.get("deployed_at"),
        deployed_by=values.get("deployed_by"),
    )
