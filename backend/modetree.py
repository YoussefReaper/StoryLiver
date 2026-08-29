"""The mode tree - what KIND of game this session is. Deterministic, $0 LLM.

Not to be confused with `modes.py`, which is a different axis entirely and
answers a different question:

    modes.py     what kind of WORLD is this?      canon / tone / stakes /
                 pacing / combat / difficulty - dials that compose, and that
                 the table can change mid-run.

    modetree.py  what kind of GAME is this?       solo / multiplayer / pvp,
                 and which of the nineteen sub-modes. Chosen once when the
                 session is created, and never changed after: it decides
                 whether the world survives the session at all, so changing it
                 mid-run would be changing what the story WAS.

The distinction the whole file exists to enforce is the seed model:

    SEED -> WORLD -> RUN.  One seed is one run is one world. A run spans many
    SESSIONS - you leave, you come back, the world is where you left it.
    Inside a run, NPCs remembering you is persistence, not a feature.

    ACROSS runs, only the PLAYER PROFILE survives: legacy score, achievements,
    rank, recorded feuds. NPCs do not carry over, and pretending they do would
    be the cheap version of the thing. The moat is the player's reputation,
    not an NPC's memory.

    PVP worlds are DISPOSABLE. The objective is cleared, the world ends, and
    the next session is a fresh seed with no cross-run memory whatsoever.
    A competitive world that remembered the last match would be a competitive
    world where the first mover has an advantage nobody agreed to.

Everything here is a table plus arithmetic. The tree is data so that a mode is
a row rather than a branch in six different files - the version of this that
scatters `if mode == "pvp"` through the engine is the version where two of
those checks disagree and nobody notices for a month.
"""
from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# The three families
# ---------------------------------------------------------------------------

FAMILIES = {
    "solo": {
        "name": "Solo",
        "blurb": "One player, one world, and it is still there when you come back.",
        "persistent": True,
    },
    "multiplayer": {
        "name": "Multiplayer",
        "blurb": "A shared world that keeps running between sessions.",
        "persistent": True,
    },
    "pvp": {
        "name": "Player versus player",
        "blurb": "A world built to be won and then thrown away.",
        "persistent": False,
    },
}

# How turns are handed out. This is the rt.py turn lock, named rather than
# re-derived: every mode states its policy once, here.
#
#   free         no lock. One player, so there is nothing to contend for.
#   locked       first to act holds the lock until their turn resolves. The
#                existing multiplayer behaviour.
#   round_robin  the lock is handed round in seat order; acting out of turn is
#                refused rather than queued. Competitive modes need this,
#                because "whoever clicked first" is not a fair initiative
#                system when the outcome is a ranking.
TURN_POLICIES = ("free", "locked", "round_robin")

# What a player is allowed to type. The Room's whole frame-break guard rests on
# this being enforced at the ACTION layer rather than asked for in a prompt.
ACTION_SPACES = ("full", "talk")


def _mode(family, name, blurb, **kw):
    spec = {
        "family": family,
        "name": name,
        "blurb": blurb,
        # Inherited from the family unless a mode overrides it. Only PvP is
        # disposable, and it is disposable because it is competitive.
        "persistent": FAMILIES[family]["persistent"],
        "disposable": not FAMILIES[family]["persistent"],
        "turn_policy": "free" if family == "solo" else "locked",
        "actions": "full",
        "min_players": 1 if family == "solo" else 2,
        "max_players": 1 if family == "solo" else 6,
        # Which score this mode feeds. None means it is not scored at all,
        # which is a real answer: a sandbox should not be a leaderboard.
        "scoring": {"solo": "legacy", "multiplayer": "coop", "pvp": "pvp"}[family],
        "seed": "random",
        "asymmetric": False,
        # What ends it. For a disposable world this is load-bearing: the world
        # is torn down the moment it is true.
        "objective": "",
        "permadeath": False,
    }
    spec.update(kw)
    spec["disposable"] = not spec["persistent"]
    return spec


MODE_TREE = {
    # ----------------------------------------------------------------- SOLO
    "story": _mode(
        "solo", "Story", "A seeded arc with an ending already written.",
        objective="the last fated event lands"),
    "ironman": _mode(
        "solo", "Ironman", "One life. When it ends, an heir picks it up.",
        permadeath=True, objective="death, or the last fated event"),
    "sandbox": _mode(
        "solo", "Sandbox", "No main quest. The world simply runs.",
        scoring=None, objective=""),
    "detective": _mode(
        "solo", "Detective",
        "A death nobody explains. The proof is in who saw what.",
        objective="you name the culprit and can show why"),
    "daily": _mode(
        "solo", "Daily Challenge",
        "The same world for everybody, today only.",
        seed="daily", objective="the daily goal, or the turn cap"),
    "builder": _mode(
        "solo", "Builder",
        "Found things. Run them. See how far the name carries.",
        scoring="legacy", objective=""),

    # ---------------------------------------------------------- MULTIPLAYER
    "coop": _mode(
        "multiplayer", "Co-op", "One party, one world, one story.",
        objective="the last fated event lands"),
    "raid": _mode(
        "multiplayer", "Raid",
        "The world itself is the opponent - unrest, and something at the top of it.",
        min_players=2, max_players=6, objective="the world event is put down"),
    "shared_sandbox": _mode(
        "multiplayer", "Shared Sandbox",
        "Several builders in one world, trading and getting in each other's way.",
        scoring=None, objective=""),
    "social_hub": _mode(
        "multiplayer", "Social Hub",
        "A room of characters who remember you for as long as the run lasts.",
        scoring=None, objective=""),
    "chaos": _mode(
        "multiplayer", "Chaos",
        "You can betray each other, and the world keeps the receipt.",
        objective="the last fated event lands"),

    # ------------------------------------------------------------------ PVP
    "duel": _mode(
        "pvp", "Duel", "Two players, one objective, first to it.",
        min_players=2, max_players=2, turn_policy="round_robin",
        objective="one player reaches the objective"),
    "teams": _mode(
        "pvp", "Teams", "Sides, racing the same goal.",
        min_players=4, max_players=8, turn_policy="round_robin",
        objective="one side reaches the objective"),
    "hunt": _mode(
        "pvp", "Hunt",
        "One player IS the thing being hunted. Everyone else is hunting it.",
        min_players=3, max_players=6, turn_policy="round_robin",
        asymmetric=True, objective="the quarry is brought down, or survives the clock"),
    "hidden_mask": _mode(
        "pvp", "Hidden Mask",
        "The hunters are wearing the faces of people who live here.",
        min_players=3, max_players=8, turn_policy="round_robin",
        asymmetric=True, objective="every mask is pulled off, or the clock runs out"),
    "king_of_hill": _mode(
        "pvp", "King of the Hill", "Hold the places that matter.",
        min_players=2, max_players=6, turn_policy="round_robin",
        objective="one player holds the majority of nodes when the clock runs out"),
    "battle_royale": _mode(
        "pvp", "Battle Royale", "Last faction standing.",
        min_players=3, max_players=8, turn_policy="round_robin",
        objective="one faction is left"),
    "async_pvp": _mode(
        "pvp", "Async", "Take your turn whenever. Nobody is waiting on a screen.",
        min_players=2, max_players=6, turn_policy="round_robin",
        objective="the objective is met, or the turn cap"),
    "room": _mode(
        "pvp", "The Room",
        "Everyone is a character in one room. At least one of them is a player.",
        min_players=3, max_players=10, turn_policy="round_robin",
        # The frame-break guard, layer one. Enforced by the parser, not asked
        # for in a prompt - see room.py.
        actions="talk",
        objective="every imposter is banished, or one survives the last vote"),
}

SUB_MODES = tuple(MODE_TREE)
DEFAULT_MODE = "story"

# The room `mode` column predates this and means something narrower (how the
# table treats each other). Mapped so an old room keeps working untouched and
# a new one can be specific.
LEGACY_ROOM_MODE = {"solo": "story", "coop": "coop", "chaos": "chaos"}


class ModeError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------

def spec(mode_id: str) -> dict:
    s = MODE_TREE.get(mode_id)
    if not s:
        raise ModeError(f"unknown mode {mode_id!r}")
    return {"id": mode_id, **s}


def exists(mode_id: str) -> bool:
    return mode_id in MODE_TREE


def normalise(mode_id: str, *, room_mode: str = "") -> str:
    """Accept a sub-mode, or fall back to the legacy room mode, or the default.
    Never raises on empty - an old client that sends nothing still gets a
    playable session."""
    if mode_id and mode_id in MODE_TREE:
        return mode_id
    if mode_id:
        raise ModeError(f"unknown mode {mode_id!r}")
    return LEGACY_ROOM_MODE.get(room_mode, DEFAULT_MODE)


def family(mode_id: str) -> str:
    return spec(mode_id)["family"]


def is_persistent(mode_id: str) -> bool:
    """Does the world survive the session ending? Solo and Multiplayer yes,
    PvP no. This single answer is what the seed model rests on."""
    return spec(mode_id)["persistent"]


def is_disposable(mode_id: str) -> bool:
    return spec(mode_id)["disposable"]


def turn_policy(mode_id: str) -> str:
    return spec(mode_id)["turn_policy"]


def action_space(mode_id: str) -> str:
    return spec(mode_id)["actions"]


def scoring(mode_id: str):
    return spec(mode_id)["scoring"]


def catalogue() -> dict:
    """The whole tree, for the client to render a picker from. Grouped by
    family, because the first question is which of the three you want."""
    out = []
    for fam, meta in FAMILIES.items():
        out.append({
            "id": fam, **meta,
            "modes": [{"id": m, **MODE_TREE[m]} for m in SUB_MODES
                      if MODE_TREE[m]["family"] == fam],
        })
    return {"families": out, "default": DEFAULT_MODE,
            "count": len(SUB_MODES),
            "turn_policies": list(TURN_POLICIES)}


# ---------------------------------------------------------------------------
# Seeding - and the one mode where the seed is not the player's
# ---------------------------------------------------------------------------

DAILY_MULTIPLIER = 31337


def daily_seed(day: str) -> int:
    """Everyone playing the Daily on a given date gets the same world.

    `day` is an ISO date string. Derived from the date alone and nothing else -
    no user, no session - because the entire point is that two players can
    compare a run over identical ground. A seed that mixed in anything
    player-specific would silently make the leaderboard meaningless."""
    digits = re.sub(r"\D", "", day or "")
    if not digits:
        raise ModeError("a daily seed needs a date")
    return (int(digits) * DAILY_MULTIPLIER) % (2 ** 31)


def seed_for(mode_id: str, *, day: str = "", session_id: str = "") -> int:
    """The seed this session's world is built from."""
    s = spec(mode_id)
    if s["seed"] == "daily":
        return daily_seed(day)
    # Everything else is seeded from the session itself, so a PvP rematch is a
    # genuinely new world rather than the same map with the scores reset.
    raw = f"{mode_id}:{session_id}".encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# The action-parser lock - guard layer 1
# ---------------------------------------------------------------------------
# The Room is talk-only, and that is enforced HERE rather than asked for in a
# system prompt. A model told "do not narrate combat" will eventually narrate
# combat; a parser that refuses the verb cannot. The list is deliberately about
# what the action DOES, not about words that sound violent - "I tell them about
# the killing" is talk, and must stay allowed.

# Talk ABOUT a thing is not the thing. "I tell them about the killing outside"
# is a sentence a player in a murder room will type on their first turn, and a
# parser that matched the word `killing` would refuse it - which teaches the
# player that the room is broken rather than that it is talk-only. So the test
# is what the ACTION does, in three ordered steps:
#
#   1. meta first, and unconditionally. Prompt injection is an attack on the
#      system rather than a move in the fiction, so no in-fiction framing
#      excuses it.
#   2. a SPEECH frame allows everything after it. "I say I will kill him" is a
#      threat, and a threat is talk - the room's own line is "you can say it;
#      you cannot do it", and this is where that is made true.
#   3. otherwise, a first-person physical verb is the actor DOING it.
#
# Every pattern below therefore requires an I/we subject. A bare noun never
# refuses anything.

_SUBJECT = r"\b(?:i|we)\s+(?:\w+\s+){0,2}?"

_SPEECH = re.compile(
    r"^\W*(?:i|we)\s+(?:"
    r"tell|told|say|said|says|ask|asked|asks|accuse|accused|accuses|whisper|whispers|"
    r"mention|mentions|claim|claims|admit|admits|confess|confesses|argue|argues|"
    r"insist|insists|deny|denies|denounce|explain|explains|answer|answers|reply|"
    r"replies|suggest|suggests|remind|reminds|warn|warns|point out|call out|"
    r"repeat|repeats|agree|agrees|lie|lies|swear|swears|promise|promises"
    r")\b", re.I)

_PHYSICAL = re.compile(
    _SUBJECT + r"(?:"
    r"attack|attacks|strike|strikes|stab|stabs|shoot|shoots|punch|punches|hit|hits|"
    r"kill|kills|murder|murders|choke|chokes|strangle|throttle|grab|grabs|shove|"
    r"shoves|slap|slaps|wrestle|tackle|tackles|draw|unsheathe|swing|lunge|lunges"
    r")\b", re.I)

_LEAVE = re.compile(
    _SUBJECT + r"(?:"
    r"leave (?:the |this )?(?:room|table|hall)|walk out|step outside|go outside|"
    r"exit (?:the )?room|flee|run away|escape|teleport|vanish|slip away"
    r")\b", re.I)

_SUMMON = re.compile(
    _SUBJECT + r"(?:"
    r"summon|summons|conjure|conjures|cast|casts|transform|transforms|power up|"
    r"go all out|open a portal|unleash|manifest"
    r")\b", re.I)

_META = re.compile(
    r"\b("
    r"as an ai|i am an ai|language model|system prompt|ignore (?:all |your )?previous|"
    r"you are chatgpt|jailbreak|developer mode|prompt injection|disregard (?:all|your) "
    r"(?:previous|prior)"
    r")\b", re.I)

REFUSALS = {
    "physical": "This is a talk-only room. You can say it; you cannot do it.",
    "leave": "Nobody leaves this room until the vote.",
    "summon": "Nothing is summoned here. Whatever you are, in this room you talk.",
    "meta": "Stay in the room.",
}


def check_action(mode_id: str, text: str) -> dict:
    """May this be typed in this mode?

    Returns {"allowed": bool, "reason": str, "kind": str}. A full-action mode
    allows everything and this is one dict construction - it is not a filter
    that everyone pays for so that one mode can be safe."""
    if action_space(mode_id) != "talk":
        return {"allowed": True, "reason": "", "kind": ""}
    body = (text or "").strip()
    # 1. Meta first and unconditionally: an attack on the system, not a move.
    if _META.search(body):
        return {"allowed": False, "reason": REFUSALS["meta"], "kind": "meta"}
    # 2. A speech frame allows whatever follows it. Saying you will do a thing
    #    is exactly what this room is for.
    if _SPEECH.match(body):
        return {"allowed": True, "reason": "", "kind": "speech"}
    # 3. Otherwise a first-person verb means the actor is DOING it.
    for kind, pattern in (("physical", _PHYSICAL), ("leave", _LEAVE),
                          ("summon", _SUMMON)):
        if pattern.search(body):
            return {"allowed": False, "reason": REFUSALS[kind], "kind": kind}
    return {"allowed": True, "reason": "", "kind": ""}


# ---------------------------------------------------------------------------
# Turn order
# ---------------------------------------------------------------------------

def may_act(mode_id: str, *, seats: list, player_id: str, turn: int) -> dict:
    """Whose turn is it? Only round-robin modes have an answer.

    Deterministic from the seat list and the turn number, so every client can
    compute the same answer without asking - and so a disconnect cannot lose
    the order."""
    policy = turn_policy(mode_id)
    if policy != "round_robin" or not seats:
        return {"ok": True, "policy": policy, "whose": None}
    whose = seats[turn % len(seats)]
    return {"ok": whose == player_id, "policy": policy, "whose": whose,
            "reason": "" if whose == player_id else f"it is {whose}'s turn"}


def seat_order(players: list) -> list:
    """Stable seat order: the order people sat down. Sorting by anything
    mutable (name, score) would reshuffle the order mid-match."""
    return [p["player_id"] for p in sorted(
        players, key=lambda p: (p.get("joined_at", ""), p["player_id"]))
        if p.get("role") != "spectator" and not p.get("left_at")]


# ---------------------------------------------------------------------------
# Validation at creation
# ---------------------------------------------------------------------------

def validate_seats(mode_id: str, count: int) -> None:
    s = spec(mode_id)
    if count < s["min_players"]:
        raise ModeError(f"{s['name']} needs at least {s['min_players']} players")
    if count > s["max_players"]:
        raise ModeError(f"{s['name']} seats at most {s['max_players']}")


def public(mode_id: str) -> dict:
    """What the client is told about the kind of game it is in."""
    s = spec(mode_id)
    return {
        "id": mode_id, "name": s["name"], "blurb": s["blurb"],
        "family": s["family"], "family_name": FAMILIES[s["family"]]["name"],
        "persistent": s["persistent"], "disposable": s["disposable"],
        "turn_policy": s["turn_policy"], "actions": s["actions"],
        "scoring": s["scoring"], "objective": s["objective"],
        "asymmetric": s["asymmetric"], "permadeath": s["permadeath"],
        "min_players": s["min_players"], "max_players": s["max_players"],
        # Said plainly, because a player about to spend an evening in a world
        # deserves to know whether it will be there tomorrow.
        "persistence_note": (
            "This world is kept. Leave and come back to it."
            if s["persistent"] else
            "This world ends when the match does. Nothing carries over except "
            "your own record."),
    }
