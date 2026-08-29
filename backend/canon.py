"""Layer 7 - canon fidelity as a neurosymbolic guardrail. Deterministic, $0 LLM.

The pattern the 2026 agent literature converged on: a symbolic hook that
intercepts a proposed action BEFORE execution and cancels it, so the model
never gets a vote on whether its own output was allowed. Prompting a model to
stay in character is a request. This is a gate.

Everything an NPC or the Narrator is about to do passes `check()`. A violation
is cancelled, logged, and replaced - the caller gets a safe substitute, and the
model is never told "try again", because that is how you get a model arguing
with its own guardrail.

Three classes of violation, in the order they are cheapest to detect:
  1. taboo      - the character's own NEVER list, from their persona anchors
  2. constraint - their hard limits (mute, dead, absent, twelve years old)
  3. safety     - the table's Lines, which outrank everything including canon
"""
from __future__ import annotations

import json
import re

from . import db

# Phrases that mean the model dropped the fiction entirely.
OOC_PATTERNS = [
    r"\bas an ai\b", r"\bas a language model\b", r"\bi cannot (help|assist|comply)\b",
    r"\bi'?m sorry,? but\b", r"\bi do not have (personal|access)\b",
    r"\bmy training data\b", r"\bopenai\b", r"\bknowledge cutoff\b",
    r"\bi'?m an? (ai|assistant|chatbot)\b", r"\bcontent policy\b",
    r"\bas the (narrator|game master|dungeon master),? i\b",
    r"\bwhat (would you|do you want to) (like to )?do next\?", r"\blet me know if\b",
    r"\bfeel free to\b", r"\bi hope (this|that) helps\b",
]
_OOC = re.compile("|".join(OOC_PATTERNS), re.I)

# Meta-fiction the Narrator must not produce even in character.
META_PATTERNS = [
    r"\byour (turn|move)\b", r"\broll (a|for) (d20|initiative)\b",
    r"\b(option|choice) (one|two|three|1|2|3)\b", r"\btype your\b",
]
_META = re.compile("|".join(META_PATTERNS), re.I)


class Cancelled(Exception):
    def __init__(self, reason, rule, layer="canon"):
        super().__init__(reason)
        self.reason = reason
        self.rule = rule
        self.layer = layer


def log(pt_id, *, turn, actor, attempted, verdict, rule="", layer="canon"):
    db.run(
        "INSERT INTO canon_log (playthrough_id,turn,actor,attempted,verdict,rule,layer,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (pt_id, turn, actor, attempted[:400], verdict, rule, layer, db.now()))


def recent(pt_id, limit=40):
    return db.rows("SELECT * FROM canon_log WHERE playthrough_id=? ORDER BY id DESC LIMIT ?",
                   (pt_id, limit))[::-1]


# ---------------------------------------------------------------------------
# Safety tools - Lines outrank canon, always
# ---------------------------------------------------------------------------

def safety(pt_id):
    row = db.row("SELECT * FROM safety WHERE playthrough_id=?", (pt_id,))
    if not row:
        db.run("INSERT OR IGNORE INTO safety (playthrough_id, updated_at) VALUES (?,?)",
               (pt_id, db.now()))
        row = db.row("SELECT * FROM safety WHERE playthrough_id=?", (pt_id,))
    return {"lines": db.jload(row["lines"], []) or [],
            "veils": db.jload(row["veils"], []) or [],
            "events": db.jload(row["events"], []) or []}


def set_safety(pt_id, *, lines=None, veils=None):
    s = safety(pt_id)
    db.run("UPDATE safety SET lines=?, veils=?, updated_at=? WHERE playthrough_id=?",
           (json.dumps(lines if lines is not None else s["lines"]),
            json.dumps(veils if veils is not None else s["veils"]),
            db.now(), pt_id))
    return safety(pt_id)


def x_card(pt_id, turn, player_id, note=""):
    """Anyone, any time, no reason required. The scene is struck."""
    s = safety(pt_id)
    events = s["events"] + [{"kind": "x_card", "turn": turn, "by": player_id,
                             "note": note[:200], "at": db.now()}]
    db.run("UPDATE safety SET events=?, updated_at=? WHERE playthrough_id=?",
           (json.dumps(events[-60:]), db.now(), pt_id))
    log(pt_id, turn=turn, actor=player_id, attempted="scene", verdict="x-carded",
        rule="x_card", layer="safety")
    return {"struck": True, "turn": turn}


def _hits(text, phrases):
    t = (text or "").lower()
    return [p for p in phrases if p and p.strip().lower() in t]


def safety_gate(pt_id, text):
    """Lines cancel. Veils do not cancel - they tell the Narrator to cut away."""
    s = safety(pt_id)
    crossed = _hits(text, s["lines"])
    if crossed:
        raise Cancelled(f"a line the table drew: {crossed[0]}", crossed[0], layer="safety")
    return {"veiled": _hits(text, s["veils"])}


# ---------------------------------------------------------------------------
# The guardrail itself
# ---------------------------------------------------------------------------

def check_npc_action(pt_id, world, npc_id, proposed, *, turn, state=None):
    """Intercept a proposed NPC action before it becomes real. Returns the
    action, or raises Cancelled. The model never sees the verdict.

    This gate is NOT mode-dependent, deliberately. The canon STRICT/LOOSE dial
    governs what a PLAYER may attempt - it is a freedom setting for the table.
    What passes through here is the MODEL's output, and a character breaking
    their own persona (or breaking frame entirely) is never something a player
    opted into. Making this relax under LOOSE would mean a sandbox world gets
    the persona drift the whole product exists to beat.

    Where the dial does apply is player-action adjudication - see
    world_master.canon_pressure().
    """
    npc = world.by_id.get(npc_id)
    if not npc:
        raise Cancelled("that character is not in this world", "unknown_npc")
    text = (proposed or "").strip()
    if not text:
        raise Cancelled("empty action", "empty")

    safety_gate(pt_id, text)

    if _OOC.search(text):
        raise Cancelled("the character stepped outside the fiction", "ooc", layer="ooc")

    anchors = npc["anchors"]
    lowered = text.lower()

    for taboo in anchors.get("taboos", []):
        # A taboo is a NEVER; the check is for the verb of the taboo appearing
        # as something this character is doing.
        core = re.sub(r"^never\s+", "", taboo.strip().lower()).rstrip(".")
        verb = core.split()[0] if core.split() else ""
        if len(verb) > 3 and re.search(rf"\b{re.escape(verb)}\w*\b", lowered):
            raise Cancelled(f"{npc['name']} never {core}", taboo)

    for constraint in anchors.get("constraints", []):
        c = constraint.lower()
        if ("does not speak" in c or "cannot speak" in c or "mute" in c) and \
                re.search(r'\b(says?|said|speaks?|spoke|whispers?|shouts?|answers? aloud|tells?)\b', lowered):
            raise Cancelled(f"{npc['name']} does not speak", constraint)
        if "will not leave" in c and re.search(r"\b(leaves?|departs?|walks out|flees?|abandons)\b", lowered):
            raise Cancelled(f"{npc['name']} will not leave", constraint)
        if "will not set foot" in c or "will not enter" in c:
            place = c.split("in the")[-1].strip().rstrip(".") if "in the" in c else ""
            if place and place[:12] in lowered:
                raise Cancelled(f"{npc['name']} does not go there", constraint)

    st = state or {}
    if npc_id in (st.get("dead") or []):
        raise Cancelled(f"{npc['name']} is dead", "mortal_stakes")

    log(pt_id, turn=turn, actor=npc_id, attempted=text, verdict="allowed")
    return text


def check_prose(pt_id, world, text, *, turn, actor="narrator"):
    """The Narrator's output. Cancelling here means the caller falls back to a
    deterministic passage rather than shipping broken fiction."""
    body = (text or "").strip()
    if not body:
        raise Cancelled("empty passage", "empty")
    safety_gate(pt_id, body)
    if _OOC.search(body):
        log(pt_id, turn=turn, actor=actor, attempted=body[:200], verdict="cancelled",
            rule="ooc", layer="ooc")
        raise Cancelled("the narrator broke frame", "ooc", layer="ooc")
    if _META.search(body):
        log(pt_id, turn=turn, actor=actor, attempted=body[:200], verdict="cancelled",
            rule="meta", layer="ooc")
        raise Cancelled("the narrator offered a menu", "meta", layer="ooc")
    return body


def anchors_block(world, npc_ids):
    """Re-injected verbatim every turn. This is the other half of the defence
    against drift: the gate catches what slips, the anchors stop most of it."""
    from . import memory
    return "\n".join(memory.anchor_block(world, n) for n in npc_ids)


# ---------------------------------------------------------------------------
# AIMS - hidden motivation the players never see
# ---------------------------------------------------------------------------

def aims(world, npc_id):
    """Agenda / Instinct / Moves / Secrets, derived deterministically from the
    persona anchors so every world gets it without extra authoring."""
    npc = world.by_id.get(npc_id)
    if not npc:
        return None
    a = npc["anchors"]
    goals = a.get("goals") or []
    constraints = a.get("constraints") or []
    return {
        "agenda": goals[0] if goals else "Get through what is coming.",
        "instinct": constraints[0] if constraints else "Protect what is theirs.",
        "moves": [g for g in goals[1:]] or ["Ask for something they will not give."],
        "secrets": [c for c in constraints if any(
            w in c.lower() for w in ("knows", "hiding", "has told no one", "secret", "only person"))],
    }


def hidden_pressure(world, npc_id, rel_vector):
    """What the NPC layer consults before spending a model call on a decision."""
    a = aims(world, npc_id) or {}
    return {
        "agenda": a.get("agenda", ""),
        "has_secret": bool(a.get("secrets")),
        "pressure": (rel_vector or {}).get("betrayal_pressure", 0.0),
    }
