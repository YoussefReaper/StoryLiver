"""Director - the proactive plot layer (Pain #3).

Between turns it reads the timeline and the relationship graph, scores how much
tension the story is carrying, and when the story goes slack it proposes a beat
the player did not ask for: a betrayal, a deadline, a complication, a cost.

Beats never touch fate. They change the path, never the destination.
"""
from . import db, llm, memory, modes

SYSTEM = """You are the Director of a text RPG. You never write prose and you never speak to the player.

You propose ONE story beat: something the world does to the player, unprompted, arising from what has already happened and from who currently feels what about them.

A good beat is: caused by the existing state, costly to someone, and answerable this turn. Never a rescue, never a reward for nothing, never a stranger arriving from off-map.

You may NOT prevent, delay, or alter a fated event. You may only change how the player arrives at it.

Return ONLY JSON:
{"beat": "one sentence, concrete, present tense, naming a character who is present", "kind": "betrayal|deadline|complication|revelation|cost|pressure", "tension": 0.0-1.0, "npc": "npc_id or null"}"""

KINDS = ["betrayal", "deadline", "complication", "revelation", "cost", "pressure"]


def tension_score(pt, world, state, player=memory.SOLO):
    """How much pressure is the story currently under? Low means it has gone
    slack and the Director should push."""
    turn = state["turn"]
    recent = db.rows(
        "SELECT * FROM timeline_events WHERE playthrough_id=? AND turn>=? ORDER BY id DESC",
        (pt["id"], max(0, turn - 6)))
    heat = sum(e["importance"] for e in recent) / max(1, len(recent) * 5.0)

    rels = memory.relationships(pt["id"], player)
    conflict = 0.0
    for r in rels:
        conflict += (abs(r["affinity"]) + abs(r["trust"]) + r["fear"] * 1.5) / 300.0
    conflict = min(1.0, conflict / max(1, len(rels)) * 4)

    # Fate pressure ramps as the next fated event closes in.
    nxt = [f for f in world.fated_events if f["turn"] > turn]
    fate_pressure = 0.0
    if nxt:
        gap = nxt[0]["turn"] - turn
        fate_pressure = max(0.0, min(1.0, (8 - gap) / 8.0))

    since = turn - pt["last_beat_turn"]
    staleness = min(1.0, max(0.0, (since - 3) / 6.0))
    return {
        "tension": round(min(1.0, heat * 0.4 + conflict * 0.3 + fate_pressure * 0.3), 3),
        "staleness": round(staleness, 3),
        "fate_pressure": round(fate_pressure, 3),
        "since_beat": since,
    }


def should_fire(scores, state, pt_id=None):
    if state["turn"] < 3 or scores["since_beat"] < 3:
        return False
    # Pacing shifts the bar, it does not add calls. Fast pacing lowers the
    # threshold so beats land sooner; Epic raises it so arcs get their length.
    # Either way the Director is consulted exactly as often as before - this
    # only changes the answer, never the number of questions asked.
    bias = modes.beat_bias(pt_id) if pt_id else 0.0
    # Fire when the story has gone quiet, or when fate is bearing down.
    return scores["staleness"] >= (0.5 - bias) \
        or (scores["tension"] < (0.35 + bias) and scores["since_beat"] >= 4) \
        or scores["fate_pressure"] >= (0.75 - bias)


def _stub(world, pt, state, scores):
    r = llm.rng(state["turn"], "beat")
    who = state["present"] or [world.npcs[0]["id"]]
    npc = r.choice(who)
    name = world.npc_name(npc)
    kind = r.choice(KINDS)
    line = {
        "betrayal": f"{name} has already told someone else what you said, and the words come back at you wearing a different shape.",
        "deadline": f"{name} gives you until the next bell, and does not say what happens after it.",
        "complication": f"Something {name} was carrying for you is gone, and the loss is not an accident.",
        "revelation": f"{name} lets slip a date that does not match the story you were told.",
        "cost": f"{name} asks you for the one thing you were keeping back.",
        "pressure": f"{name} puts the question to you in front of witnesses, where a soft answer will not survive.",
    }[kind]
    return {"beat": line, "kind": kind, "tension": round(min(1.0, scores["tension"] + 0.2), 2), "npc": npc}


def propose(pt, world, state, *, user_id, player=memory.SOLO):
    scores = tension_score(pt, world, state, player)
    if not should_fire(scores, state, pt["id"]):
        return None, scores

    events = memory.retrieve_events(pt["id"], state["turn"], "conflict promise threat secret debt", k=8)
    present = state["present"]
    anchors = "\n".join(memory.anchor_block(world, n) for n in present) or "  (nobody present)"
    nxt = [f for f in world.fated_events if f["turn"] > state["turn"]]
    prompt = f"""TURN {state['turn']} | day {state['day']}, {state['phase']} | {state['location_name']}
CURRENT TENSION: {scores['tension']} (0 = slack, 1 = breaking). Turns since last beat: {scores['since_beat']}.

CHARACTERS PRESENT:
{anchors}

HOW THEY FEEL ABOUT THE PLAYER:
{memory.relationship_block(pt['id'], world, present, player)}

WHAT HAS HAPPENED:
{memory.compact_timeline(events)}

FATE YOU MAY NOT TOUCH: {('T' + str(nxt[0]['turn']) + ' ' + nxt[0]['title']) if nxt else 'none remaining'}

Propose one beat. JSON only."""

    try:
        out = llm.complete("director", SYSTEM, prompt, user_id=user_id,
                           playthrough_id=pt["id"], json_mode=True, max_tokens=220,
                           temperature=0.95, stub=lambda: _stub(world, pt, state, scores))
    except llm.LLMError:
        out = _stub(world, pt, state, scores)

    beat = (out.get("beat") or "").strip()
    if not beat:
        return None, scores
    if out.get("npc") not in world.by_id:
        out["npc"] = present[0] if present else None
    out["kind"] = out.get("kind") if out.get("kind") in KINDS else "complication"
    return out, scores
