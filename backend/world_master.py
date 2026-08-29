"""World Master - the rules layer.

Validates every action against the world's rules and the current structured
state, and returns {valid, reason, rule_ref, diff}. The Narrator never sees an
unvalidated action.

Three stages, cheapest first:
  1. universal mechanical checks - presence, the dead, fate, movement
  2. the world's own declarative rule checks (regex + state conditions), so a
     player-forged world gets code-enforced rules without writing code
  3. one cheap structured LLM call for everything semantic
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import arcs, llm, memory, persona

MOVE_VERB = re.compile(
    r"\b(go|walk|head|move|travel|leave for|return|climb|cross|make my way|set off|step|enter)\b")

ADDRESS_VERB = re.compile(
    r"\b(ask|tell|say to|speak to|talk to|show|give|hand|grab|hit|strike|kiss|follow|warn|threaten|"
    r"beg|order|thank|accuse|kill|attack|help)\b")

PREVENT_FATE = re.compile(
    r"\b(stop|prevent|avert|cancel|undo|forestall|call off|hold back)\b", re.I)

SYSTEM = """You are the World Master of a text RPG. You do not write prose. You adjudicate.

Given the world rules, the immutable fate, the current structured state and the player's intended action, decide whether the action can happen as stated.

Reject ONLY when the action contradicts a rule, contradicts an established fact in the timeline, requires something the protagonist does not have, or attempts to prevent a fated event. Ordinary, risky, foolish, or morally ugly actions are VALID - failure is the narrator's job, not yours. When in doubt, allow it.

Return ONLY a JSON object:
{
  "valid": true|false,
  "reason": "one sentence, addressed to the player, explaining why it cannot happen (empty when valid)",
  "rule_ref": "rule id you are citing, or null",
  "consequence": "one factual sentence of what actually results - no prose, no adjectives",
  "importance": 1-5,
  "relationship_deltas": [
    {"npc": "npc_id", "affinity": -25..25, "trust": -25..25, "fear": -25..25, "obligation": -25..25, "note": "few words"}
  ],
  "npc_focus": ["npc_id", ...],
  "new_location": "location_id or null"
}

Only include NPCs actually present. Deltas are small: a sentence moves a number by 2-8, a betrayal or a rescue by 15-25. Most turns move nothing at all."""

CONTEST_SYSTEM = """You are the World Master arbitrating a CONTESTED action between players with competing goals.

Two or more players want incompatible things. Decide who prevails using the structured state you are given - position, what each has established, who the NPCs trust - plus the roll you are handed. The roll breaks ties; state decides the odds. Do not be even-handed for its own sake: if one player has clearly set this up, they win.

Return ONLY JSON:
{"winner": "player_id", "reason": "one sentence naming the state that decided it",
 "consequence": "one factual sentence of the outcome",
 "loser_cost": "one factual sentence of what it cost the other side"}"""


# ---------------------------------------------------------------------------
# Stage 1 + 2: checks that need no model call
# ---------------------------------------------------------------------------

def resolve_movement(world, action: str, state: dict) -> Optional[str]:
    """Travel is mechanical, not semantic - the model never decides where the
    map lets you stand."""
    text = action.lower()
    if not MOVE_VERB.search(text):
        return None
    best: tuple[int, str] | None = None
    for lid in world.connects(state["location"]):
        loc = world.loc_by_id[lid]
        words = [w for w in re.findall(r"[a-z]+", loc["name"].lower())
                 if w not in ("the", "of", "a", "an")]
        hit = [w for w in words if w in text]
        if hit or lid.replace("_", " ") in text:
            score = max((len(w) for w in hit), default=1)
            if best is None or score > best[0]:
                best = (score, lid)
    return best[1] if best else None


def _named_npcs(world, text: str) -> list[dict]:
    out = []
    for npc in world.npcs:
        first = npc["name"].split()[0].lower()
        if len(first) < 3:
            continue
        if re.search(rf"\b{re.escape(first)}\b", text):
            out.append(npc)
    return out


def _rule_id(world, *needles: str) -> Optional[str]:
    for needle in needles:
        for rid in world.rule_by_id:
            if needle in rid.lower():
                return rid
    return None


def _check_matches(check: dict, text: str, state: dict) -> bool:
    pattern = check.get("pattern")
    if pattern:
        try:
            if not re.search(pattern, text, re.I):
                return False
        except re.error:
            return False
    when = check.get("when") or {}
    if "turn_lt" in when and not state["turn"] < int(when["turn_lt"]):
        return False
    if "turn_gte" in when and not state["turn"] >= int(when["turn_gte"]):
        return False
    if "phase_in" in when and state["phase"] not in when["phase_in"]:
        return False
    if "location" in when and state["location"] != when["location"]:
        return False
    if "not_location" in when and state["location"] == when["not_location"]:
        return False
    if "flag_absent" in when and state.get("flags", {}).get(when["flag_absent"]):
        return False
    return True


def _mechanical(world, action: str, state: dict) -> Optional[dict]:
    text = action.lower()

    named = _named_npcs(world, text)
    if named and ADDRESS_VERB.search(text):
        for npc in named:
            if npc["id"] in state["dead"]:
                return {"valid": False,
                        "rule_ref": _rule_id(world, "mortal", "death", "dead"),
                        "reason": f"{npc['name']} is dead. The dead do not answer."}
        absent = [n for n in named if n["id"] not in state["present"]]
        if absent and len(absent) == len(named):
            return {"valid": False,
                    "rule_ref": _rule_id(world, "one_place", "presence", "location"),
                    "reason": f"{absent[0]['name']} is not at {state['location_name']}."}

    if PREVENT_FATE.search(text):
        upcoming = [f for f in world.fated_events if f["turn"] > state["turn"]]
        fate_words = set()
        for f in world.fated_events:
            fate_words |= {w for w in re.findall(r"[a-z]{5,}", f["title"].lower())}
        if upcoming and fate_words & set(re.findall(r"[a-z]{5,}", text)):
            return {"valid": False,
                    "rule_ref": _rule_id(world, "fate", "immutable") or "fate",
                    "reason": "You cannot unwrite what is already written. You can decide who is "
                              "standing beside you when it comes."}

    for rule in world.rules:
        check = rule.get("check")
        if check and _check_matches(check, text, state):
            return {"valid": False, "rule_ref": rule["id"],
                    "reason": check.get("reason") or rule["text"]}
    return None


# ---------------------------------------------------------------------------

def build_state(pt, world, player=memory.SOLO) -> dict:
    turn = pt["current_turn"]
    loc = pt["current_location"]
    states = memory.all_npc_states(pt["id"])
    return {
        "turn": turn,
        "phase": world.phase_for(turn),
        "day": world.day_for(turn),
        "location": loc,
        "location_name": world.loc_name(loc),
        "present": memory.npcs_at(pt["id"], world, loc, turn),
        "dead": [s["npc_id"] for s in states if not s["alive"]],
        "player": player,
        "flags": {},
    }


def fate_block(world, turn: int) -> str:
    past = [f for f in world.fated_events if f["turn"] <= turn]
    nxt = [f for f in world.fated_events if f["turn"] > turn]
    lines = ["  ALREADY HAPPENED (cannot be undone):"]
    lines += [f"    T{f['turn']} {f['title']} - {f['desc']}" for f in past] or ["    (none yet)"]
    if nxt:
        lines.append("  STILL TO COME (cannot be prevented):")
        lines.append(f"    T{nxt[0]['turn']} {nxt[0]['title']} - {nxt[0]['desc']}")
        for f in nxt[1:3]:
            lines.append(f"    T{f['turn']} {f['title']}")
    return "\n".join(lines)


def _stub(action, state):
    r = llm.rng(state["turn"], action)
    focus = state["present"][:2]
    return {
        "valid": True, "reason": "", "rule_ref": None,
        "consequence": f"You {action.strip().rstrip('.')[:110]}. It lands the way such things land here.",
        "importance": r.choice([2, 3, 3, 4]),
        "relationship_deltas": ([{"npc": focus[0], "affinity": r.choice([-4, 0, 3, 5]),
                                  "trust": r.choice([-3, 0, 2, 4]), "fear": 0, "obligation": 0,
                                  "note": "spoke with you"}] if focus and r.random() < 0.7 else []),
        "npc_focus": focus,
        "new_location": None,
    }


def validate(pt, world, action, *, user_id, player=memory.SOLO, party=None):
    state = build_state(pt, world, player)
    moving_to = resolve_movement(world, action, state)
    hard = _mechanical(world, action, state)
    if hard:
        return {**hard, "consequence": "", "importance": 2, "relationship_deltas": [],
                "npc_focus": state["present"], "new_location": None, "state": state,
                "checked_by": "rules"}

    present_block = "\n".join(
        f"  - {memory.anchor_block(world, n)}" for n in state["present"]
    ) or "  (nobody else is here)"

    party_block = ""
    if party:
        party_block = "\nOTHER PLAYERS PRESENT:\n" + "\n".join(
            f"  - {p['name']}" + (f" (their stated goal: {p['goal']})" if p.get("goal") else "")
            for p in party) + "\n"

    events = memory.retrieve_events(pt["id"], state["turn"], action, k=8)
    prompt = f"""WORLD: {world.name} - {world.get('tagline', '')}
RULES:
{chr(10).join('  ' + r['id'] + ': ' + r['text'] for r in world.rules)}

FATE:
{fate_block(world, state['turn'])}

CURRENT STATE:
  turn {state['turn']} | day {state['day']}, {state['phase']} | location: {state['location_name']} ({state['location']})
  exits: {', '.join(world.connects(state['location']))}
  dead: {', '.join(state['dead']) or 'nobody'}

PRESENT CHARACTERS:
{present_block}
{party_block}
WHAT THIS PLAYER HAS DONE THAT MATTERS:
{memory.compact_timeline(events)}

HOW THEY FEEL ABOUT THIS PLAYER:
{memory.relationship_block(pt['id'], world, state['present'], player)}

PLAYER'S INTENDED ACTION: {action}

Adjudicate. JSON only."""

    system = SYSTEM
    extra = "\n".join(x for x in (canon_pressure(pt["id"]), arcs.au_directive(pt["id"])) if x)
    if extra:
        system = SYSTEM + "\n\n" + extra

    try:
        out = llm.complete("world_master", system, prompt, user_id=user_id,
                           playthrough_id=pt["id"], json_mode=True, max_tokens=450,
                           temperature=0.2, stub=lambda: _stub(action, state))
    except llm.LLMError:
        out = _stub(action, state)

    out.setdefault("valid", True)
    out.setdefault("reason", "")
    out.setdefault("consequence", action)
    out.setdefault("importance", 3)
    out.setdefault("relationship_deltas", [])
    out.setdefault("npc_focus", state["present"])
    out.setdefault("rule_ref", None)
    out.setdefault("new_location", None)
    valid_ids = set(world.by_id)
    out["relationship_deltas"] = [d for d in out["relationship_deltas"]
                                  if isinstance(d, dict) and d.get("npc") in valid_ids]
    out["npc_focus"] = [n for n in out["npc_focus"] if n in valid_ids]
    if out["new_location"] not in world.loc_by_id:
        out["new_location"] = None
    if moving_to:                     # the map, not the model, decides where you can stand
        out["new_location"] = moving_to
        if not (out.get("consequence") or "").strip():
            out["consequence"] = f"You walk to {world.loc_name(moving_to)}."
    out["state"] = state
    out["checked_by"] = "model"
    # Which canonical moment this is, decided in code from the action and its
    # consequence. The narrator uses it to unlock only the canon lines that
    # belong to this beat.
    out["beat_key"] = persona.detect_beat(action, out.get("consequence", ""))
    return out


# The canon dial is ADDITIVE: STRICT adds enforcement, LOOSE is the baseline
# the engine has always run. Written this way round on purpose - if LOOSE
# subtracted enforcement instead, a sandbox world would quietly lose the world
# rules and fate guarantees that the anti-collapse proof rests on, which is
# not what "canon is a suggestion" is supposed to buy you.
CANON_PRESSURE = (
    "CANON: strict. This table is playing for lore accuracy. Reject an action "
    "that contradicts established canon fact, not merely one that breaks a "
    "world rule. Characters stay where canon puts them and know what canon "
    "says they know."
)


def canon_pressure(pt_id: str) -> str:
    """Extra instruction appended to the World Master under STRICT. Empty
    under LOOSE, so the default prompt is unchanged."""
    from . import modes
    return CANON_PRESSURE if modes.guardrail_action(pt_id) == "cancel" else ""


# ---------------------------------------------------------------------------
# PVP / chaos arbitration
# ---------------------------------------------------------------------------

def arbitrate(pt, world, contest, *, user_id):
    """Two players want incompatible things. State sets the odds; a roll breaks
    the tie; the World Master says who prevailed and what it cost."""
    state = build_state(pt, world, contest["challenger"]["player_id"])
    rolls = {}
    rng = llm.rng(pt["id"], state["turn"], "contest")
    for side in (contest["challenger"], contest["defender"]):
        rolls[side["player_id"]] = rng.randint(1, 20)

    sides = "\n".join(
        f"  {s['name']} ({s['player_id']}) wants: {s['action']}"
        f"\n    standing with those present: "
        f"{memory.relationship_block(pt['id'], world, state['present'], s['player_id']).strip() or 'unknown'}"
        f"\n    roll: d20 = {rolls[s['player_id']]}"
        for s in (contest["challenger"], contest["defender"]))

    events = memory.retrieve_events(pt["id"], state["turn"],
                                    contest["challenger"]["action"] + " " + contest["defender"]["action"], k=8)
    prompt = (f"PLACE: {state['location_name']} | turn {state['turn']}\n\n"
              f"CONTESTED:\n{sides}\n\n"
              f"WHAT HAS HAPPENED:\n{memory.compact_timeline(events)}\n\nArbitrate. JSON only.")

    def stub():
        win = max(rolls, key=lambda k: rolls[k])
        other = next(k for k in rolls if k != win)
        return {"winner": win, "reason": f"The roll went {rolls[win]} to {rolls[other]}.",
                "consequence": "They get there first, and the room reorganises around it.",
                "loser_cost": "The other loses the moment, and everyone present saw it."}

    try:
        out = llm.complete("world_master", CONTEST_SYSTEM, prompt, user_id=user_id,
                           playthrough_id=pt["id"], json_mode=True, max_tokens=300,
                           temperature=0.4, stub=stub)
    except llm.LLMError:
        out = stub()
    if out.get("winner") not in rolls:
        out["winner"] = max(rolls, key=lambda k: rolls[k])
    out["rolls"] = rolls
    out["state"] = state
    return out
