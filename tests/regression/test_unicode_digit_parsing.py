"""Regression guard: no ``.isdigit()`` gate in front of an ``int()``.

``str.isdigit()`` is the obvious way to ask "is this token a number?"
before calling ``int()`` on it, and it is wrong. 128 code points answer
``True`` to it and then raise ``ValueError`` inside ``int()``:
superscripts (``"²"``), subscripts (``"₃"``), circled digits (``"①"``),
Ethiopic numerals… Every gate that used it therefore had a crash behind
it, reachable by anyone who can type — and the two worst were reachable
in one message:

* ``handlers/games._is_vanity_roll`` runs as a FILTER, so ``/roll ²``
  raised while aiogram was still choosing a handler and killed the whole
  update, with no reply of any kind.
* ``handlers/withdraw.handle_withdraw_amount`` is an FSM step, so
  ``²`` at the amount prompt raised instead of answering "это не
  число" — leaving the user parked in ``awaiting_amount`` with a flow
  that could not be advanced.

The fix is one shared predicate, :func:`is_int_token`, and this file
keeps it that way from three angles:

1. ``test_every_isdigit_only_codepoint_is_rejected`` — the property
   itself, swept over the whole Unicode space rather than over a
   hand-picked sample.
2. ``test_no_bare_isdigit_gates_in_src`` — an AST scan, so a new
   ``.isdigit()`` gate cannot be added back without either routing
   through the helper or landing on the audited allowlist below.
3. The behavioural block — the actual parsers, fed ``"²"``, must answer
   politely instead of raising.

The same family has a second member, guarded here too: a gate that
checks one string and an ``int()`` that parses another. Four sites gated
on ``token.lstrip("-")`` and then called ``int(token)`` — ``lstrip``
removes EVERY leading sign, so ``"--5"`` passed the gate and raised
inside the ``int()``. The fix is
:func:`~telegram_invite_bot.utils.numbers.parse_int_token`, which
returns the VALUE so there is no second string to disagree with, and
``test_no_sign_stripping_gates_in_src`` keeps the shape from coming
back.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from telegram_invite_bot.handlers import marriage as marriage_mod
from telegram_invite_bot.handlers.admin.withdrawals import parse_reject_args
from telegram_invite_bot.handlers.checks import _pos_ints
from telegram_invite_bot.handlers.clear import _parse_count
from telegram_invite_bot.handlers.games import (
    _is_stake_flip,
    _is_stake_roll,
    _is_vanity_roll,
)
from telegram_invite_bot.handlers.group_pay import parse_amount
from telegram_invite_bot.handlers.groupadmin import _resolve_staff_target
from telegram_invite_bot.handlers.marriage import _parse_extend_days
from telegram_invite_bot.handlers.moderation import (
    _parse_duration_seconds,
    parse_ban_duration,
)
from telegram_invite_bot.handlers.profile import _resolve_target
from telegram_invite_bot.handlers.pvp_stake import _parse_bet
from telegram_invite_bot.handlers.send import _parse_args
from telegram_invite_bot.handlers.withdraw import handle_withdraw_amount
from telegram_invite_bot.utils.numbers import (
    MAX_DB_INT,
    is_int_token,
    parse_int_token,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"

_METHODS = frozenset({"isdigit", "isnumeric"})

# Audited sites that may keep the bare stdlib predicate. Each one is
# allowed for a reason, not for convenience — none of them feeds an
# ``int()``, and none of them reads a Telegram message.
_ALLOWED: dict[str, str] = {
    "utils/numbers.py": "the helper itself",
    # /proc/partitions device names (sda1, nvme0n1p1) — kernel-supplied
    # ASCII, and the result is a bool about the NAME, never parsed.
    "handlers/admin/partitions.py": "/proc/partitions device-name shape",
    # /proc/interrupts irq ids: numeric = hardware, alphabetic = pseudo
    # (NMI, LOC). Kernel-supplied ASCII, no int().
    "handlers/admin/interrupts.py": "/proc/interrupts irq-id shape",
}

# Floors that keep the scans below from passing by scanning nothing.
# ``is_int_token`` is counted including its own module; the sites that
# need the parsed VALUE call ``parse_int_token`` instead.
_MIN_FILES_SCANNED = 150
_MIN_HELPER_CALL_SITES = 11
_MIN_PARSER_CALL_SITES = 5


def _isdigit_sites() -> list[tuple[str, int]]:
    """Every ``x.isdigit()`` / ``x.isnumeric()`` CALL under ``src/``.

    AST rather than grep on purpose: several docstrings quote legacy's
    ``args[0].isdigit()`` verbatim while the code beneath them uses the
    helper, and a textual scan cannot tell the two apart.
    """
    sites: list[tuple[str, int]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _METHODS
            ):
                sites.append((path.relative_to(SRC_ROOT).as_posix(), node.lineno))
    return sites


def _helper_call_sites(name: str = "is_int_token") -> list[str]:
    """Modules that actually call ``name`` (the shared token helpers)."""
    hits: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ):
                hits.append(path.relative_to(SRC_ROOT).as_posix())
                break
    return hits


# ── 1. the property, over the whole Unicode space ───────────────────


def _isdigit_but_unparseable() -> list[str]:
    """Code points where ``str.isdigit()`` is True but ``int()`` raises."""
    out: list[str] = []
    for code in range(sys.maxunicode + 1):
        char = chr(code)
        if not char.isdigit():
            continue
        try:
            int(char)
        except ValueError:
            out.append(char)
    return out


_TRAPS = _isdigit_but_unparseable()


def test_the_trap_set_is_real() -> None:
    """Guard the guard: the sweep must actually find the trap code points.

    If a future CPython made ``int()`` accept every ``isdigit()`` code
    point, this list would empty out and the test below would assert
    nothing at all — that deserves a failure and a re-read, not a
    silent pass.
    """
    assert len(_TRAPS) >= 120, f"only {len(_TRAPS)} trap code points found"
    for sample in ("²", "³", "₃", "①", "⑴"):
        assert sample in _TRAPS


def test_every_isdigit_only_codepoint_is_rejected() -> None:
    for char in _TRAPS:
        assert not is_int_token(char), f"{char!r} (U+{ord(char):04X}) slipped through"
        # Also in the far more likely mixed shape: a real number the user
        # fat-fingered a superscript into.
        assert not is_int_token(f"1{char}")


def test_plain_ascii_numbers_still_pass() -> None:
    for token in ("0", "7", "42", "1000000", "0" * 100):
        assert is_int_token(token)
        int(token)  # the promise the predicate is making


def test_non_numbers_are_rejected() -> None:
    # Empty, signed, spaced and fractional tokens are the caller's job —
    # see the helper's docstring for why the sign stays at the call site.
    for token in ("", " ", "-1", "+1", "1.5", "1 000", "abc", "٣"):
        assert not is_int_token(token)


def test_parse_int_token_takes_at_most_one_sign() -> None:
    """The other half of this bug family: a gate and an ``int()`` that disagree.

    ``token.lstrip("-")`` reads as "drop the sign" and is not — it drops
    EVERY leading ``-``. A gate written that way accepted ``"--5"`` and
    the ``int(token)`` behind it raised. :func:`parse_int_token` parses
    the one string it was given, so there is nothing left to disagree
    with.
    """
    assert parse_int_token("42") == 42
    assert parse_int_token("-5", signed=True) == -5
    assert parse_int_token("+5", signed=True) == 5
    assert parse_int_token("-0", signed=True) == 0
    # A run of signs is not an integer, in either mode.
    for token in ("--5", "++5", "+-5", "-+5", "---1"):
        assert parse_int_token(token, signed=True) is None, token
        assert parse_int_token(token) is None, token
    # Unsigned is the default: a sign is simply not part of the grammar.
    assert parse_int_token("-5") is None
    assert parse_int_token("+5") is None
    # And the Unicode trap does not sneak back in behind a sign.
    assert parse_int_token("-²", signed=True) is None
    assert parse_int_token("", signed=True) is None
    assert parse_int_token("-", signed=True) is None


class _UsersRepoStub:
    """``UsersRepo`` stand-in: records the lookups, resolves nobody."""

    def __init__(self) -> None:
        self.by_id: list[int] = []
        self.by_username: list[str] = []

    async def get(self, user_id: int) -> None:
        self.by_id.append(user_id)

    async def get_by_username(self, username: str) -> None:
        self.by_username.append(username)


@pytest.mark.asyncio
async def test_double_sign_tokens_are_rejected() -> None:
    """``--5`` / ``++5`` must answer, not raise, at every parse site.

    All three used to gate on the ``lstrip``ed string and then ``int()``
    the original — the check-create ones inside FSM steps, where a raise
    leaves the user parked at a prompt that never advances.
    """
    assert _parse_bet("--5") is None
    assert _parse_bet("-5") == -5  # parsed; the service refuses it as INVALID_BET
    assert _pos_ints(["++5"]) is None
    assert _pos_ints(["5", "++7"]) is None
    assert _pos_ints(["+5", "7"]) == [5, 7]
    repo = _UsersRepoStub()
    assert await _resolve_target("--5", repo) is None  # type: ignore[arg-type]
    assert repo.by_id == [], "a double sign must never reach an id lookup"


def test_absurdly_long_digit_runs_are_rejected() -> None:
    """``int()`` refuses a literal past 4300 digits — so does the gate.

    Not reachable through a Telegram message (4096 chars max), but the
    predicate is the promise "``int()`` will not raise", and it has to
    hold for every caller.
    """
    assert not is_int_token("1" * 5000)


def test_values_sqlite_cannot_store_are_rejected() -> None:
    """#1042: past 2**63-1 the gate says no, because aiosqlite raises.

    A bind parameter outside the signed 64-bit range raises
    ``OverflowError`` inside aiosqlite, and nothing catches it — the
    handler dies mid-reply instead of answering. The boundary is
    asserted from both sides so a future "just widen it" edit has to
    argue with a test.
    """
    assert is_int_token(str(2**63 - 1))
    assert not is_int_token(str(2**63))
    assert not is_int_token("9" * 25)
    assert parse_int_token(str(2**63 - 1)) == 2**63 - 1
    assert parse_int_token(str(2**63)) is None
    assert parse_int_token(f"-{2**63}", signed=True) is None


@pytest.mark.asyncio
async def test_an_unstorable_id_never_reaches_the_repo() -> None:
    """The #1042 crash path: ``/profile <20 digits>`` used to raise.

    ``_resolve_target`` is the shared "@name or numeric id" resolver
    behind /profile and the numeric form of the moderation commands, so
    proving the token dies before ``UsersRepo.get`` covers the family.
    """
    repo = _UsersRepoStub()
    assert await _resolve_target("9" * 20, repo) is None  # type: ignore[arg-type]
    assert repo.by_id == [], "an id SQLite cannot store must never be bound"
    # It degrades into the bare-word branch, which is the same thing
    # ``/profile someuser`` already does: a TEXT lookup that finds
    # nobody. Harmless, and preferable to a magic third answer.
    assert repo.by_username == ["9" * 20]


# ── 2. no bare gate may come back ───────────────────────────────────


def test_no_bare_isdigit_gates_in_src() -> None:
    offenders = [f"{path}:{line}" for path, line in _isdigit_sites() if path not in _ALLOWED]
    assert not offenders, (
        "bare .isdigit()/.isnumeric() found — 128 code points pass it and then "
        "raise ValueError inside int(). Use "
        "telegram_invite_bot.utils.numbers.is_int_token, or add the site to "
        f"_ALLOWED with a reason: {offenders}"
    )


_STRIP_METHODS = frozenset({"lstrip", "rstrip", "strip"})
_SIGNS = frozenset("+-")

# Sign-stripping that is NOT a number gate. Same rule as ``_ALLOWED``:
# an entry earns its place by having no ``int()`` behind it.
_STRIP_ALLOWED: dict[str, str] = {
    # Trims stray dashes off a generated URL slug — a string result, and
    # nothing parses it.
    "cms/guide_site/markdown.py": "slug trim, no int() behind it",
}


def _sign_strip_sites(tree: ast.AST, label: str) -> list[str]:
    """Every ``x.lstrip("-")``-shaped call in ``tree`` (sign chars only)."""
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _STRIP_METHODS
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value
            and set(node.args[0].value) <= _SIGNS
        ):
            found.append(f"{label}:{node.lineno}")
    return found


def _all_sign_strip_sites() -> list[str]:
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in _STRIP_ALLOWED:
            continue
        offenders.extend(_sign_strip_sites(ast.parse(path.read_text(encoding="utf-8")), rel))
    return offenders


def test_no_sign_stripping_gates_in_src() -> None:
    """``lstrip("-")`` before an ``int()`` is the same bug wearing a sign.

    ``"--5".lstrip("-")`` is ``"5"``, so the gate says yes and the
    ``int()`` on the ORIGINAL string raises. Use
    :func:`telegram_invite_bot.utils.numbers.parse_int_token`, which
    returns the value and therefore leaves no second string to disagree.
    """
    offenders = _all_sign_strip_sites()
    assert not offenders, (
        "sign-stripping found — if an int() follows, '--5' passes the gate and "
        "then raises. Use parse_int_token(token, signed=True), or add the site "
        f"to _STRIP_ALLOWED with a reason: {offenders}"
    )


def test_the_sign_strip_scan_can_see_a_sample() -> None:
    """Guard the guard: the detector must fire on the exact shape it bans."""
    sample = ast.parse('if is_int_token(token.lstrip("-")):\n    x = int(token)\n')
    assert _sign_strip_sites(sample, "sample.py") == ["sample.py:1"]
    # …and stay blind to the plain no-argument form, which is harmless.
    assert _sign_strip_sites(ast.parse("token.strip()"), "sample.py") == []
    for rel, reason in _STRIP_ALLOWED.items():
        tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"))
        assert _sign_strip_sites(tree, rel), (
            f"_STRIP_ALLOWED entry {rel!r} ({reason}) no longer has a stripping "
            f"call — the entry is stale and is now hiding future offenders"
        )


def test_the_scan_is_not_vacuous() -> None:
    """Guard the guard: the scanner must see files, and see the allowlist.

    A broken ``rglob`` or a parse that silently yields nothing would let
    the test above pass on an empty list forever.
    """
    scanned = {path for path, _ in _isdigit_sites()}
    files = list(SRC_ROOT.rglob("*.py"))
    assert len(files) >= _MIN_FILES_SCANNED, f"only {len(files)} files scanned"
    missing = set(_ALLOWED) - scanned
    assert not missing, (
        f"_ALLOWED lists {sorted(missing)}, but the scan found no bare call "
        f"there — the entry is stale and is now hiding future offenders"
    )
    users = _helper_call_sites()
    assert len(users) >= _MIN_HELPER_CALL_SITES, (
        f"is_int_token is called from only {len(users)} modules — the gates "
        f"were removed rather than routed through the helper"
    )
    parsers = _helper_call_sites("parse_int_token")
    assert len(parsers) >= _MIN_PARSER_CALL_SITES, (
        f"parse_int_token is called from only {len(parsers)} modules — the "
        f"sites that need the VALUE were reverted to a gate + a separate int()"
    )


# ── 3. the parsers themselves ───────────────────────────────────────


def _message(text: str) -> Any:
    """Minimal stand-in: the predicates below read ``.text`` and nothing else."""
    return SimpleNamespace(text=text)


@pytest.mark.parametrize(
    ("label", "call"),
    [
        # /clear ² — count parser, falls back to the default.
        ("clear", lambda: _parse_count("²", default=3) == 3),
        # /roll ² — the FILTER that used to raise mid-routing.
        ("roll-vanity", lambda: _is_vanity_roll(_message("/roll ²")) is False),
        ("roll-stake", lambda: _is_stake_roll(_message("/roll ² 3")) is False),
        ("flip-stake", lambda: _is_stake_flip(_message("/flip ² орёл")) is False),
        ("group_pay", lambda: parse_amount("/group_pay ²") is None),
        ("pvp-bet", lambda: _parse_bet("²") is None),
        ("check-count", lambda: _pos_ints(["²"]) is None),
        (
            "marry-extend",
            lambda: _parse_extend_days(SimpleNamespace(args="²")) is None,  # type: ignore[arg-type]
        ),
        # /ban ² in reply form: not a duration, not a user id.
        (
            "ban-duration",
            lambda: parse_ban_duration("²", allow_bare_number=True) is None,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_parsers_reject_unicode_digits(label: str, call: Callable[[], bool]) -> None:
    assert call(), f"{label}: parser did not reject '²'"


class _ReplyRecorder:
    """Message stub that records the reply text instead of calling Telegram."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=1, is_bot=False)
        self.replies: list[str] = []

    async def reply(self, text: str, **_: object) -> None:
        self.replies.append(text)


class _StateStub:
    async def get_data(self) -> dict[str, object]:
        return {"lang": "ru"}


@pytest.mark.asyncio
async def test_withdraw_amount_step_answers_instead_of_raising() -> None:
    """The FSM step must reply, not raise — a raise parks the user forever.

    Everything after the amount gate (wallet read, escrow, confirm card)
    is unreachable for a rejected token, so the repos below are never
    touched and are passed as ``None``.
    """
    message = _ReplyRecorder("²")
    await handle_withdraw_amount(
        message,  # type: ignore[arg-type]
        _StateStub(),  # type: ignore[arg-type]
        economy_repo=None,  # type: ignore[arg-type]
        withdraw_service=None,  # type: ignore[arg-type]
        withdraw_config=None,  # type: ignore[arg-type]
    )
    assert len(message.replies) == 1, "the user got no answer at all"
    assert message.replies[0].strip(), "the answer was empty"


# --- the regex member of the same family (#1646) ---------------------
#
# The AST scan above cannot see this one. A compiled pattern is not a
# method call, so ``_DURATION_RE`` sat outside every guard in this file
# while being the only user-facing numeric parser in ``handlers/`` that
# did not route through ``is_int_token``.


@pytest.mark.parametrize(
    "token",
    [
        "30ſ",  # LATIN SMALL LETTER LONG S — case-folds to "s"
        "30ſec",
        "٣٠d",  # Arabic-Indic thirty
        "۳۰m",  # Extended Arabic-Indic thirty
    ],
)
def test_the_mute_duration_regex_refuses_unicode(token: str) -> None:
    """Neither half of this may parse, and neither may raise.

    ``_parse_duration_seconds`` looks the matched unit up in a dict
    with no fallback, so a match on a unit the dict does not carry is
    a ``KeyError`` — which is what the long-s case used to be. The
    ``is None`` assertion therefore also asserts "did not raise": a
    parser that answers ``None`` sends the admin a usage hint, and one
    that raises sends them the generic error card.
    """
    assert _parse_duration_seconds(token) is None
    # The same token through the /ban entry point, which lowercases
    # first — that ``.lower()`` is not a defence, because
    # ``"ſ".lower()`` is ``"ſ"``.
    assert parse_ban_duration(token, allow_bare_number=False) is None


@pytest.mark.parametrize(
    ("token", "seconds"),
    [("30s", 30), ("30 M", 1800), ("2hrs", 7200), ("1week", 604800), ("7D", 604800)],
)
def test_the_mute_duration_regex_still_reads_ascii(token: str, seconds: int) -> None:
    """The control group, including the case-insensitive and plural
    forms — the narrowed flag must not have cost any of them.
    """
    assert _parse_duration_seconds(token) == seconds


def test_the_long_s_really_does_fold_to_s() -> None:
    """Without this the parametrisation above could rot into a set of
    tokens the *old* pattern already refused, and the tests would pass
    on a reverted fix.
    """
    import re

    unfixed = re.compile(r"^(\d+)\s*(s|sec|m|min|h|hr|d|day|w|week)s?$", re.IGNORECASE)
    assert unfixed.match("30ſ") is not None
    assert unfixed.match("٣٠d") is not None
    assert "ſ".lower() == "ſ"


# --- the magnitude member of the same family (#1691) -----------------
#
# ``try: int(x) except ValueError`` catches the parse failure and
# nothing else. A long pure-ASCII digit run parses fine, escapes the
# ``except``, and raises ``OverflowError`` where SQLite binds it — one
# layer past the parser that already declared the input valid, so the
# user gets the generic error card instead of «это не число».
#
# ``/send`` is the sharp end: it is open to every user in every chat,
# and the RECIPIENT field is shielded by nothing (the amount field is
# incidentally covered by the Python-side ``balance < amount`` check,
# which returns before any bind).

_TOO_BIG = "9" * 20  # every character an ASCII digit, and past 2**63-1


def test_the_too_big_token_really_is_past_the_sqlite_ceiling() -> None:
    """Without this the tokens below could rot into ones any parser
    refuses, and the guards would pass on a reverted fix.
    """
    assert _TOO_BIG.isdigit()
    assert int(_TOO_BIG) > MAX_DB_INT


def test_send_refuses_a_recipient_sqlite_cannot_bind() -> None:
    """``/send <20-digit id> 1`` must die in the parser.

    Unfixed it parsed cleanly, survived the ``get_chat_member`` probe
    (whose failure is caught and does not stop the flow) and raised at
    ``TransferService.send`` step 3, ``economy.get(to_id)``. No coins
    moved — the crash lands on a READ, before any debit — but the user
    saw the generic error card for an input the bot should have named.
    """
    assert _parse_args(f"{_TOO_BIG} 1") is None
    assert _parse_args(f"-{_TOO_BIG} 1") is None


def test_send_refuses_an_amount_sqlite_cannot_bind() -> None:
    """Both arms: the explicit form's second token and the reply
    form's first one.
    """
    assert _parse_args(f"777 {_TOO_BIG}") is None
    assert _parse_args(_TOO_BIG, has_reply=True) is None


def test_send_still_reads_the_ordinary_forms() -> None:
    """The control group — the bound must not cost any live form,
    including the negative ids the numeric form has always accepted.
    """
    assert _parse_args("777 100") == ("id", 777, 100)
    assert _parse_args("-100123 100") == ("id", -100123, 100)
    assert _parse_args("@alice 100") == ("username", "alice", 100)
    assert _parse_args("100", has_reply=True) == ("reply", None, 100)
    assert _parse_args("777 100", has_reply=True) == ("id", 777, 100)


# --- the same ceiling on ids that arrive as callback data (#1692) ----
#
# Callback data looks trusted and is not. Telegram does not check a
# pressed button's payload against the keyboard it came from, so an
# MTProto client can send any string the router's filter accepts — and
# ``F.data.regexp(r"^marry_accept_\\d+$")`` bounds the CHARACTER CLASS,
# not the length. The four proposal callbacks parsed the tail with a
# bare ``int()`` in a ``try``, so a twenty-digit id passed and raised
# OverflowError one layer down, on the SELECT that looks the proposal
# up.
#
# Nothing moves — the crash lands on a read, before any write — but the
# repo should never be handed a value the database cannot store, and
# the tapper should get the handler's own «❌» rather than the generic
# error card.

_PROPOSAL_CALLBACKS = [
    ("callback_marry_accept", "marry_accept_"),
    ("callback_marry_decline", "marry_decline_"),
    ("callback_rel_accept", "rel_accept_"),
    ("callback_rel_decline", "rel_decline_"),
]


class _RecordingBondsRepo:
    """A bonds repo that records lookups and finds nothing.

    Finding nothing lets the ordinary-id control case run to a clean
    early return; recording is what proves the too-big case never got
    that far.
    """

    def __init__(self) -> None:
        self.lookups: list[int] = []

    async def get_proposal_by_id(self, prop_id: int, chat_id: int) -> None:
        self.lookups.append(prop_id)

    async def get_relationship_proposal_by_id(self, prop_id: int, chat_id: int) -> None:
        self.lookups.append(prop_id)


class _FakeProposalCall:
    """Callback stub: the handlers touch only ``.data``, ``.from_user``,
    ``.message.chat.id`` and ``.answer`` before the lookup watched here.
    """

    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=777)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=-100123))
        self.answers: list[str] = []

    async def answer(self, text: str = "", **_kw: Any) -> None:
        self.answers.append(text)


@pytest.mark.parametrize(("handler_name", "prefix"), _PROPOSAL_CALLBACKS)
async def test_proposal_callbacks_refuse_an_id_sqlite_cannot_bind(
    handler_name: str, prefix: str
) -> None:
    repo = _RecordingBondsRepo()
    call = _FakeProposalCall(f"{prefix}{_TOO_BIG}")
    handler: Any = getattr(marriage_mod, handler_name)

    await handler(call, repo, "ru")

    assert repo.lookups == [], "an id SQLite cannot store must never be bound"
    assert call.answers == ["\u274c"]


@pytest.mark.parametrize(("handler_name", "prefix"), _PROPOSAL_CALLBACKS)
async def test_proposal_callbacks_still_read_an_ordinary_id(handler_name: str, prefix: str) -> None:
    """The control group — the bound must not cost a real button."""
    repo = _RecordingBondsRepo()
    call = _FakeProposalCall(f"{prefix}42")
    handler: Any = getattr(marriage_mod, handler_name)

    await handler(call, repo, "ru")

    assert repo.lookups == [42]


async def test_groupadmin_staff_target_refuses_an_id_sqlite_cannot_bind() -> None:
    """The staff-grant target is typed by an admin: the sign was
    checked, the magnitude was not.

    The numeric branch touches no database, so ``None`` stands in for
    the registry — reaching a repo at all would be the failure this
    asserts against. The value went on to ``RankService.get_rank``,
    which binds it.
    """
    assert await _resolve_staff_target(None, _TOO_BIG) is None  # type: ignore[arg-type]
    assert await _resolve_staff_target(None, f"-{_TOO_BIG}") is None  # type: ignore[arg-type]
    assert await _resolve_staff_target(None, "777") == 777  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# #1692 (part 3): ``/admin_withdrawals reject <id> [reason]``.
#
# Developer-gated, so this is a crash rather than an escalation — but it
# is a crash on a command an operator types by hand, next to a queue of
# real payouts, and the id was read with a bare ``int()`` guarded by
# ``except ValueError``. Twenty digits parse cleanly there and raise
# ``OverflowError`` inside ``WithdrawalsRepo.get``, which binds the id:
# verified against a live schema, "Python int too large to convert to
# SQLite INTEGER".
#
# The parse now lives in a module-level helper for the same reason
# ``groupadmin.parse_staff_grant`` does — inside the handler it is
# reachable only through a wired Dispatcher, and the edge cases are the
# whole point.


def test_reject_args_refuse_an_id_sqlite_cannot_bind() -> None:
    assert parse_reject_args(f"reject {_TOO_BIG}") is None
    assert parse_reject_args(f"reject {_TOO_BIG} опечатка") is None


def test_reject_args_read_the_ordinary_forms() -> None:
    """The control group — the bound must not cost a real command."""
    assert parse_reject_args("reject 7") == (7, None)
    assert parse_reject_args("reject 7 нет реквизитов") == (7, "нет реквизитов")
    assert parse_reject_args("REJECT 7") == (7, None)


def test_reject_args_refuse_the_shapes_that_were_never_ids() -> None:
    """A negative id cannot exist (AUTOINCREMENT), and a signed one is
    refused rather than read: taking the sign here would re-admit it.
    """
    assert parse_reject_args("reject -5") is None
    assert parse_reject_args("reject +5") is None
    assert parse_reject_args("reject") is None
    assert parse_reject_args("approve 7") is None
    assert parse_reject_args("reject 7x") is None
