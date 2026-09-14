"""Romance "RP-action" data + parser (FEAT-RP) — pure, no I/O.

A roleplay action is a prefixed verb a user writes while replying to a
partner's message in a group (e.g. ``.обнять``, ``бот поцеловать``,
``ком hug``). Each action carries a minimum relationship level, an XP
reward granted to the pair, and a short name that maps to the
``rel_rp_done_<name>`` / ``rel_rp_done_<name>_general`` i18n keys.

This module is the single source of truth for:

* :data:`RP_ACTION_SPEC` — every trigger word → ``(min_level, xp, name)``.
* :data:`RP_UNIVERSAL` — the ``name`` set that may target anyone, even
  with no relationship (renders the ``_general`` 0-XP variant).
* :data:`RP_18_MIN_LEVEL` — actions at this level or above are 18+.
* :func:`rp_activity_key` — ``name`` → the joint-activity-log key the
  pair's history stores the action under.
* :func:`parse_rp_trigger` — the load-bearing prefix-gated parser that
  decides whether a raw group message is an RP action at all. It returns
  ``None`` for ordinary chatter so the group-filter never steals normal
  conversation (the no-chatter-theft rule).

Ported from the legacy ``RP_ACTION_SPEC`` table (bot.py:22120-22173).
The EN trigger twins are legacy's own — every RU verb there already
carried an English sibling mapped to the same spec tuple. This module
adds exactly two one-word conveniences on top, and only alongside the
legacy spelling, never instead of it: ``highfive`` next to legacy's
``high five`` (bot.py:22124) and ``dinner`` next to legacy's
``romantic dinner`` (bot.py:22154). Dropping a legacy trigger would
make the action unreachable for anyone who learnt the old wording:
the resolver matches whole triggers, so ``.romantic dinner @user``
misses a bare ``dinner`` entry on both the two-word and the one-word
pass and falls through to ``None``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Action spec — trigger word → (min_level, xp, name)
# ---------------------------------------------------------------------------
#
# ``name`` is the suffix for ``rel_rp_done_<name>`` (+ ``_general``). The
# Russian triggers are the canonical legacy set; EN twins (added below)
# map to the SAME tuple so both locales reach the same flavoured key.

RP_ACTION_SPEC: dict[str, tuple[int, int, str]] = {
    # Russian triggers (legacy verbatim) -----------------------------------
    "пожать руку": (1, 3, "handshake"),
    "дать пять": (1, 5, "highfive"),
    "ударить": (1, 1, "hit"),
    "пнуть": (1, 1, "kick"),
    "обнять": (2, 10, "hug"),
    "погладить": (2, 5, "stroke"),
    "извиниться": (2, 2, "sorry"),
    "укусить": (3, 5, "bite"),
    "щекотать": (3, 5, "tickle"),
    "успокоить": (3, 2, "calm"),
    "покормить": (4, 10, "feed"),
    "напоить": (4, 10, "drink"),
    "обидеться": (4, 2, "offend"),
    "поцеловать": (5, 20, "kiss"),
    "лизнуть": (5, 5, "lick"),
    "подарить": (5, 15, "gift"),
    "романтический ужин": (6, 30, "dinner"),
    "комплимент": (6, 10, "compliment"),
    "цветы": (7, 25, "flowers"),
    "признание": (7, 40, "confess"),
    "кольцо": (8, 50, "ring"),
    "фото": (8, 15, "photo"),
    "предложение": (9, 100, "propose"),
    "помолвка": (9, 50, "engagement"),
    "свадьба": (10, 200, "wedding"),
    "выебать": (6, 0, "sex"),
    "трахнуть": (6, 0, "sex"),
    # English trigger twins (same spec tuple) ------------------------------
    "handshake": (1, 3, "handshake"),
    "highfive": (1, 5, "highfive"),
    "high five": (1, 5, "highfive"),
    "hit": (1, 1, "hit"),
    "kick": (1, 1, "kick"),
    "hug": (2, 10, "hug"),
    "stroke": (2, 5, "stroke"),
    "sorry": (2, 2, "sorry"),
    "bite": (3, 5, "bite"),
    "tickle": (3, 5, "tickle"),
    "calm": (3, 2, "calm"),
    "feed": (4, 10, "feed"),
    "drink": (4, 10, "drink"),
    "offend": (4, 2, "offend"),
    "kiss": (5, 20, "kiss"),
    "lick": (5, 5, "lick"),
    "gift": (5, 15, "gift"),
    "romantic dinner": (6, 30, "dinner"),
    "dinner": (6, 30, "dinner"),
    "compliment": (6, 10, "compliment"),
    "flowers": (7, 25, "flowers"),
    "confess": (7, 40, "confess"),
    "ring": (8, 50, "ring"),
    "photo": (8, 15, "photo"),
    "propose": (9, 100, "propose"),
    "engagement": (9, 50, "engagement"),
    "wedding": (10, 200, "wedding"),
    "sex": (6, 0, "sex"),
}

# Inverse index: action ``name`` → every trigger word that fires it, in
# spec order (Russian first, then the EN twins). ``/rp_commands`` prints
# these so a user learns what to actually TYPE — the display labels it
# used to print were only ever one locale's spelling, so a Russian-UI
# user never discovered that ``hug`` works too (RR-5 #54/#58).
#
# Derived rather than hand-written: a trigger added above and forgotten
# here would be undiscoverable, which is the exact failure this restores.
TRIGGERS_BY_NAME: dict[str, tuple[str, ...]] = {}
for _trigger, (_min_level, _xp, _name) in RP_ACTION_SPEC.items():
    TRIGGERS_BY_NAME[_name] = (*TRIGGERS_BY_NAME.get(_name, ()), _trigger)
del _trigger, _min_level, _xp, _name

# Actions at this relationship level or above are 18+ (gated on the
# group's rp_18 flag — see handlers/rp.py for the gate decision).
RP_18_MIN_LEVEL = 5

# Actions at this relationship level or above may be used by a VIP with
# NO pair at all, when the group's ``rp_vip_outside_enabled`` flag is on
# (#270; legacy ``bot.py:22116``). Every relationship-only action is
# level 6 or above, so in practice the threshold admits all eight of
# them and the constant is really a floor for future spec edits — see
# the VIP branch in handlers/rp.py for what it gates.
RP_VIP_OUTSIDE_MIN_LEVEL = 3

# Action ``name`` set that may target anyone even with no relationship.
# These render the ``_general`` (0-XP) variant when the target is not a
# pair. Relationship-only actions (dinner, flowers, confess, ring,
# propose, engagement, wedding, photo) are deliberately excluded.
RP_UNIVERSAL: frozenset[str] = frozenset(
    {
        "handshake",
        "highfive",
        "hit",
        "kick",
        "hug",
        "stroke",
        "sorry",
        "bite",
        "tickle",
        "calm",
        "feed",
        "drink",
        "offend",
        "kiss",
        "lick",
        "gift",
        "compliment",
        "sex",
    }
)

# Bare verbs (no prefix required) — legacy let ``выебать``/``трахнуть``
# fire un-prefixed. Stored lowercased.
_BARE_TRIGGERS: frozenset[str] = frozenset({"выебать", "трахнуть"})

# Leading prefixes that mark a message as an RP-action invocation. Order
# matters: longer/more-specific prefixes are tried before bare ``!`` so
# ``"! "`` strips the trailing space too. Matched case-insensitively.
_PREFIXES: tuple[str, ...] = (".", "?", "! ", "!", "бот ", "ком ", "ии ")


def _match_trigger(text: str) -> tuple[int, int, str] | None:
    """Match ``text`` against the spec by full string, then 2 words, then 1.

    Multi-word triggers (``романтический ужин``, ``пожать руку``,
    ``high five``) only match when the whole phrase is present, so we
    probe the longest candidate first.
    """
    lowered = text.strip().lower()
    if not lowered:
        return None
    spec = RP_ACTION_SPEC.get(lowered)
    if spec is not None:
        return spec
    words = lowered.split()
    if len(words) >= 2:
        two = f"{words[0]} {words[1]}"
        spec = RP_ACTION_SPEC.get(two)
        if spec is not None:
            return spec
    if words:
        spec = RP_ACTION_SPEC.get(words[0])
        if spec is not None:
            return spec
    return None


def parse_rp_trigger(text: str | None) -> tuple[str, int, int, str] | None:
    """Parse a raw message into an RP action, or ``None``.

    Returns ``(trigger_name, min_level, xp, name)`` where ``trigger_name``
    is the matched action's canonical ``name`` (same as ``name``; kept as
    a 4-tuple so callers can destructure positionally) — actually
    ``(name, min_level, xp, name)``: the first element echoes the action
    name for readability, the rest mirror the spec tuple.

    Gating (the no-chatter-theft rule): the message MUST start with one
    of :data:`_PREFIXES` (``.``/``?``/``!``/``! ``/``бот ``/``ком ``/``ии ``,
    case-insensitive). The only exception is the bare sex-verbs
    ``выебать``/``трахнуть``, which legacy allowed un-prefixed. Anything
    that is neither prefixed nor a bare sex-verb returns ``None``, so
    ordinary group chatter is never intercepted.

    That exception is narrower than legacy's on one axis and wider on
    another, and both halves are deliberate:

    * Legacy accepted the ARGUMENT form too — ``bot.py:43455`` tests
      ``lower.startswith("выебать ")`` as well as the bare word, and the
      comment at ``bot.py:43596`` names «Выебать @user» explicitly. Here
      only the exact bare word matches (``lowered in _BARE_TRIGGERS``),
      so ``выебать @user`` is not an RP action. Accepting it would make
      every group message beginning with those two words an interception
      candidate, which is exactly the chatter theft the rule exists to
      prevent.
    * Legacy also required a target — ``bot.py:43597`` gates on
      ``message.reply_to_message or parts[1].strip()`` — so a lone
      ``выебать`` with no reply did nothing. This parser accepts it and
      leaves the target check to the handler.

    The divergence is bounded to cosmetics: both verbs are
    ``(6, 0, "sex")`` (``:72-73``) — relationship level 6, ZERO xp, no
    coins — so no economy path can move on it either way.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    lowered = raw.lower()

    # Prefixed invocation — strip exactly ONE leading prefix, then match.
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            remainder = raw[len(prefix) :]
            spec = _match_trigger(remainder)
            if spec is None:
                return None
            min_level, xp, name = spec
            return (name, min_level, xp, name)

    # Bare sex-verb (no prefix) — the one un-prefixed exception.
    if lowered in _BARE_TRIGGERS:
        min_level, xp, name = RP_ACTION_SPEC[lowered]
        return (name, min_level, xp, name)

    # Not a prefixed action and not a bare sex-verb → not an RP action.
    return None


def rp_activity_key(name: str) -> str:
    """The joint-activity-log key an RP action is recorded under.

    Legacy filed RP actions in the SAME ``*_activity_log`` table as the
    paid couple activities, under ``rp_<name>`` — the fourth element of
    every ``RP_ACTION_SPEC`` tuple at ``bot.py:22120-22174``. The port
    trimmed that tuple to three elements and lost the key with it, so
    the pair's history went silent for every RP verb (#231).

    Rebuilt from ``name`` rather than restored as a fourth element: the
    two were identical for all 26 actions in the legacy table, and a
    hand-copied duplicate is one more thing that can drift.
    """
    return f"rp_{name}"
