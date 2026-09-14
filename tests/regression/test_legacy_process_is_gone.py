"""#2004: prose may not claim the legacy telebot process is running.

T-011 (2026-05-26) removed the strangler bridge. ``CUTOVER.md`` records
the rest: ``main.py`` is out of the repository, ``ENABLE_NEW_PIPELINE``
is out of ``Settings``, and no legacy unit is left on any host. Since
then every Telegram update is resolved by this package
(``webhook/server.py``) — there is no second bot.

Roughly thirty docstrings had not been told. They said the legacy
process "still writes" a table, "still owns" a callback literal, that a
column is "co-owned with the live telebot process", that an operator's
old command name "falls through to legacy". A reader who believed them
reached for the wrong invariant: a shared-writer hazard that no longer
exists, and — worse — a second owner for work that in fact has no owner
at all. Three of those turned out to be exactly that (donation writes,
``/send``'s per-chat opt-in, the crypto USD→coins rate); the prose was
what hid them.

**Why the phrase list is not the discriminator.** It would rot, and a
list of banned wordings is the kind of guard #2001 declined to add. The
discriminator is the first test below: the repository itself can say
whether a legacy process could exist. Only when it says no does the
second test hold the prose to it — so if a legacy layer ever comes back,
this file fails on the premise and says so, rather than quietly policing
sentences that have become true again.

The patterns are therefore deliberately narrow: each one can only ever
be a present-tense claim that a second process is running. Softer
phrasings ("used to fall through to legacy") are left alone on purpose —
telling those apart from the honest historical note that
``handlers/group_only.py`` and ``handlers/relations.py`` are built on
would need a list of phrasings, which is the thing being avoided.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "telegram_invite_bot"

#: Wordings that can only mean "a legacy process is running right now".
_LIVE_CLAIMS = (
    re.compile(r"(?:live|LIVE|running)\s+telebot"),
    re.compile(r"telebot\s+process,?\s+which"),
    re.compile(r"legacy\s+webhook\s+is\s+live"),
    re.compile(r"legacy\s+(?:\S+\s+){0,2}(?:is|are)\s+still\s+(?:alive|live|running)"),
    re.compile(r"legacy\s+still\s+(?:writes|owns|reads|renders|maintains|runs)"),
    re.compile(r"still\s+owned\s+by\s+the\s+(?:live|LIVE)"),
    re.compile(r"co-owned\s+with\s+the\s+(?:live|LIVE)"),
    re.compile(r"(?:during|for the duration of)\s+the\s+strangler\s+window"),
    re.compile(r"(?:during|under)\s+the\s+parallel[- ]run"),
    re.compile(r"legacy\s+(?:owns|maintains|gates)\s+those"),
    re.compile(r"strangler\s+bridge\s+still"),
    # The shape that survived every pattern above until #2007: an
    # adjectival "still-live legacy monolith" rather than a verb.
    # It read as scene-setting, which is exactly why nobody caught
    # that the sentence it justified was also wrong.
    re.compile(r"still[- ]live\s+legacy"),
    # #2009, the same adjective one word further along. "the
    # still-legacy writers", "the still-legacy code path", "the
    # still-legacy redeem callbacks" — six sites, each naming a second
    # owner for work that has none. Unlike the phrasings this file
    # leaves alone, "still-legacy" cannot be read as history: the
    # "still" is doing the same job the deleted process was.
    re.compile(r"still[- ]legacy\b"),
    # The co-writer hazard in its two remaining spellings. Both were
    # written to justify a design decision (a server-side timestamp, a
    # schema kept ALTER-free) that is still correct for other reasons —
    # which is why the sentences outlived the process they describe.
    re.compile(r"legacy\s+can\s+keep\s+writing"),
    re.compile(r"legacy\s+and\s+new\s+write"),
)


def _prose_of(path: Path) -> list[tuple[int, str]]:
    """Every comment and string constant in ``path``, with line numbers."""
    source = path.read_text()
    prose = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    prose += [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return prose


def test_nothing_in_the_tree_could_run_a_legacy_process() -> None:
    """Guard the guard — and the premise of the one below it.

    Four independent facts, any one of which coming back would mean the
    prose claims are true again. They are checked rather than assumed
    because the whole point of this file is that a claim about the world
    should be answerable from the repository.
    """
    assert not (ROOT / "main.py").exists(), (
        "``main.py`` is back. It was the legacy systemd entrypoint; if it"
        " runs again, the docstrings this file polices are true again and"
        " this guard is the thing that is wrong. Re-read CUTOVER.md."
    )
    assert not (SRC / "handlers" / "legacy_bridge.py").exists(), (
        "the strangler bridge module is back — T-011 removed it, and it is"
        " the only thing that ever handed an update to telebot"
    )
    pipeline_flag = [
        path.relative_to(SRC)
        for path in sorted(SRC.rglob("*.py"))
        if "ENABLE_NEW_PIPELINE" in path.read_text()
    ]
    assert not pipeline_flag, (
        "``ENABLE_NEW_PIPELINE`` is back in the package. It was the switch"
        f" between the two pipelines: {pipeline_flag}"
    )

    def _imports_telebot(node: ast.AST) -> bool:
        if isinstance(node, ast.Import):
            return any(alias.name.split(".")[0] == "telebot" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").split(".")[0] == "telebot"
        return False

    importers = [
        path.relative_to(SRC)
        for path in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if _imports_telebot(node)
    ]
    assert not importers, f"something imports telebot again: {importers}"


def test_no_prose_claims_the_legacy_process_is_running() -> None:
    """The finding itself, as a standing check."""
    stale: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, text in _prose_of(path):
            for pattern in _LIVE_CLAIMS:
                for match in pattern.finditer(text):
                    offset = text[: match.start()].count("\n")
                    line = text.splitlines()[offset].strip()
                    site = f"{path.relative_to(SRC)}:{lineno + offset}"
                    stale.append(f"  {site}: {line}")
    assert not stale, (
        "prose says a legacy telebot process is running. It is not — see"
        " the test above and CUTOVER.md. Rewrite the claim in the past"
        " tense AND check what the sentence was justifying: half of these"
        " turned out to be describing work that has no owner at all rather"
        " than work owned by the other layer:\n" + "\n".join(dict.fromkeys(stale))
    )


#: The same claim, in the corpus the *user* reads. ``h_*`` and ``site_*``
#: are the values this pipeline authored (the rest of the YAML is a
#: verbatim carry-over pinned by ``tests/unit/i18n/test_legacy_parity``,
#: and none of it ours to re-word — see
#: :mod:`tests.regression.test_copy_command_references` for the same
#: scoping decision).
_USER_FACING_CLAIMS = (
    re.compile(r"\blegacy\b", re.IGNORECASE),
    re.compile(r"стар\w+\s+схем\w+", re.IGNORECASE),
    re.compile(r"\bold\s+(?:scheme|flow|pipeline|bot)\b", re.IGNORECASE),
)


def test_no_copy_tells_a_user_that_a_legacy_flow_handles_their_request() -> None:
    """#2005: the same finding, one layer out.

    A stale comment costs a maintainer a wasted trip. The same claim in
    a card costs a *user* money: ``h_inventory_detail_legacy_hint`` told
    the buyer of an UNKNOWN inventory row that the item "is still
    handled by the legacy activation flow — see /help". There is no such
    flow, ``/help`` cannot activate anything, and
    ``services/inventory_use_planner`` says UNKNOWN is terminal — the
    service refuses before consuming, so the item the user already paid
    for can never be used. The copy sent them to a dead end and made it
    sound temporary.

    Copy may of course describe history; what it may not do is tell the
    reader that some other system is going to handle this for them.
    """
    data_dir = SRC / "i18n" / "data"
    guilty: list[str] = []
    for locale in sorted(data_dir.glob("*.yaml")):
        catalog = yaml.safe_load(locale.read_text())
        for key, value in catalog.items():
            if not isinstance(value, str):
                continue
            if not key.startswith(("h_", "site_")):
                continue
            if any(pattern.search(value) for pattern in _USER_FACING_CLAIMS):
                guilty.append(f"  {locale.name}:{key}: {value.strip()}")
    assert not guilty, (
        "user-facing copy tells the reader a legacy flow will handle"
        " this. Nothing will — see the premise test above. Say what the"
        " user can actually do instead, and check that the destination"
        " can act: pointing at an index of commands is not an answer"
        " when the answer is that nothing in the bot can help:\n" + "\n".join(guilty)
    )
