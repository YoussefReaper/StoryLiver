"""Training-arc skip - time passes, and it COSTS something.

The failure mode this exists to prevent: "/skip" producing "three months
passed" and nothing else. That is a null operation dressed as pacing, and it
teaches players that skipping is free, which makes every skipped stretch
meaningless.

So a skip here applies the SAME deterministic deltas the played version would
have. Relationships move. Skills improve. The world ticks. Rumours in flight
arrive. Hunters en route get closer. Then - and only then - the narrator is
given the computed result and asked to narrate what already happened.

That ordering is the whole design. The maths is the truth; the prose reports
it. A model is never asked to decide what a skip did, so a skip cannot
hallucinate progress that the state does not reflect. One model call for the
recap, priced as one action.
"""
from __future__ import annotations

import json

from . import db, memory, modes, relationships, worldstate

# What a stretch of focused time can be spent on. Each maps to deterministic
# consequences - this is a table of causes, not of flavour text.
KINDS = {
    "training": {
        "name": "Training", "blurb": "Drilling a skill until it holds under pressure.",
        "skill_gain": 2.0, "relationship_events": ["showed_competence"],
        "with_mentor": ["listened", "showed_competence"],
    },
    "travel": {
        "name": "Travel", "blurb": "Ground covered. Distance is its own kind of time.",
        "skill_gain": 0.3, "relationship_events": ["spoke_kindly"],
        "with_mentor": ["listened", "spoke_kindly"],
    },
    "recovery": {
        "name": "Recovery", "blurb": "Mending. Slower than anyone wants.",
        "skill_gain": 0.5, "relationship_events": [],
        "with_mentor": ["helped", "listened"],
    },
    "study": {
        "name": "Study", "blurb": "Reading, asking, piecing it together.",
        "skill_gain": 1.4, "relationship_events": ["showed_competence"],
        "with_mentor": ["listened", "shared_secret"],
    },
    "downtime": {
        "name": "Downtime", "blurb": "Ordinary days. They add up anyway.",
        "skill_gain": 0.2, "relationship_events": ["spoke_kindly"],
        "with_mentor": ["spoke_kindly", "listened"],
    },
}

MAX_TURNS = 40


class SkipError(ValueError):
    pass


def plan(pt_id, *, kind="training", turns=None, with_whom=None) -> dict:
    """What this skip WOULD do, computed but not applied.

    Shown to the player before they confirm, because a skip that silently
    moves relationships is a skip players learn to distrust."""
    if kind not in KINDS:
        raise SkipError(f"unknown skip kind {kind!r}")
    turns = _turns(pt_id, turns)
    spec = KINDS[kind]
    with_whom = [w for w in (with_whom or []) if w]

    return {
        "kind": kind, "name": spec["name"], "turns": turns,
        "skill_gain": round(spec["skill_gain"] * turns / 4, 2),
        "with": with_whom,
        "relationship_events": spec["with_mentor"] if with_whom else spec["relationship_events"],
        "note": "Applied as real deltas, not narration. The world moves while you do this.",
    }


def apply(pt_id, world, *, kind="training", turns=None, with_whom=None,
          player=memory.SOLO) -> dict:
    """Run the skip for real. Deterministic and $0 - the narration that
    follows is the caller's one budgeted model call."""
    if kind not in KINDS:
        raise SkipError(f"unknown skip kind {kind!r}")

    turns = _turns(pt_id, turns)
    spec = KINDS[kind]
    with_whom = [w for w in (with_whom or []) if w]
    pt = db.row("SELECT * FROM playthroughs WHERE id=?", (pt_id,))
    if not pt:
        raise SkipError("playthrough not found")

    start_turn = pt["current_turn"]
    end_turn = start_turn + turns
    changes = {"relationships": [], "world_ticks": 0, "skill": {}, "arrivals": []}

    # 1. The world does not hold still while you train.
    for t in range(start_turn + 1, end_turn + 1):
        worldstate.tick(pt_id, world, t)
        changes["world_ticks"] += 1

    # 2. Relationships with whoever you spent the time alongside. Events are
    #    applied per stretch-of-four-turns, so a longer skip moves more - but
    #    diminishing returns in the relationship engine keep it from running away.
    events = spec["with_mentor"] if with_whom else spec["relationship_events"]
    reps = max(1, turns // 4)
    for npc_id in (with_whom or []):
        before = relationships.vector(pt_id, npc_id, player) or {}
        for _ in range(reps):
            for event in events:
                relationships.apply_event(pt_id, npc_id, player, event,
                                          turn=end_turn, weight=0.6,
                                          note=f"{spec['name'].lower()} together")
        after = relationships.vector(pt_id, npc_id, player) or {}
        changes["relationships"].append({
            "npc_id": npc_id,
            "trust": round(after.get("trust", 0) - before.get("trust", 0), 1),
            "affinity": round(after.get("affinity", 0) - before.get("affinity", 0), 1),
            "respect": round(after.get("respect", 0) - before.get("respect", 0), 1),
            "now": after.get("disposition", ""),
        })

    # 3. Skill. Scaled by difficulty - a Brutal world yields less per hour.
    _, scarcity = modes.difficulty_scalars(pt_id)
    gain = round(spec["skill_gain"] * turns / 4 / max(0.5, scarcity), 2)
    changes["skill"] = {"kind": kind, "gain": gain}
    _bump_skill(pt_id, player, kind, gain)

    # 4. Advance the clock and record it as a real event, not a gap.
    db.run("UPDATE playthroughs SET current_turn=? WHERE id=?", (end_turn, pt_id))
    memory.add_event(pt_id, end_turn, player, f"{spec['name']} for {turns} turns",
                     _consequence_line(spec, changes), kind="fastforward",
                     importance=3, location=pt["current_location"])

    # 5. What you missed. A skip is the honest moment to hand back something
    #    the engine genuinely withheld at the time - you were not there, so
    #    nobody told you, and now somebody has. This only lands because the
    #    witness gate was real: if the Chronicle had shown it live, there would
    #    be nothing left to reveal.
    from . import legacy
    changes["reveal"] = legacy.surface_reveal(pt_id, world, player, turn=end_turn)

    changes["from_turn"] = start_turn
    changes["to_turn"] = end_turn
    return changes


def _bump_skill(pt_id, player, kind, gain):
    """Skills live in prefs-shaped storage per playthrough so a world that
    does not track power is unaffected by this existing."""
    row = db.row("SELECT world_json FROM playthroughs WHERE id=?", (pt_id,))
    skills_row = db.row("SELECT data FROM prefs WHERE user_id=?", (f"skills:{pt_id}:{player}",))
    skills = db.jload(skills_row["data"], {}) if skills_row else {}
    skills[kind] = round(float(skills.get(kind, 0)) + gain, 2)
    db.run("INSERT INTO prefs (user_id,data,updated_at) VALUES (?,?,?)"
           " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data,"
           " updated_at=excluded.updated_at",
           (f"skills:{pt_id}:{player}", json.dumps(skills), db.now()))
    return skills


def skills(pt_id, player=memory.SOLO) -> dict:
    row = db.row("SELECT data FROM prefs WHERE user_id=?", (f"skills:{pt_id}:{player}",))
    return db.jload(row["data"], {}) if row else {}


def _consequence_line(spec, changes) -> str:
    bits = [f"{spec['name']} paid off: +{changes['skill']['gain']} to {changes['skill']['kind']}."]
    for r in changes["relationships"]:
        if abs(r["trust"]) >= 1 or abs(r["affinity"]) >= 1:
            bits.append(f"{r['npc_id']}: trust {r['trust']:+.1f}, affinity {r['affinity']:+.1f}.")
    return " ".join(bits)


def recap_prompt(changes, *, kind, with_whom) -> str:
    """What the narrator is given. It receives the RESULT and reports it -
    it is never asked to decide what happened."""
    lines = [f"A stretch of {kind} passed - {changes['to_turn'] - changes['from_turn']} turns.",
             "These are the facts. Narrate them as events that already happened,",
             "specific and concrete. Do not say 'time passed'. Show what changed.",
             f"Skill gained: +{changes['skill']['gain']} {changes['skill']['kind']}."]
    for r in changes["relationships"]:
        lines.append(f"{r['npc_id']}: trust {r['trust']:+.1f}, affinity {r['affinity']:+.1f}, "
                     f"now reads as '{r['now']}'.")
    if with_whom:
        lines.append(f"Spent alongside: {', '.join(with_whom)}.")
    rev = changes.get("reveal")
    if rev:
        lines.append(f"While they were away they learned something old: {rev['label']} "
                     f"- it happened on turn {rev['turn']}, and nobody had told them. "
                     f"Land this as news arriving late, not as a flashback.")
    return "\n".join(lines)


def _turns(pt_id, turns):
    if turns is None:
        turns = modes.fastforward_turns(pt_id)
    turns = int(turns)
    if turns < 1:
        raise SkipError("a skip must cover at least one turn")
    return min(turns, MAX_TURNS)


def catalogue():
    return {"kinds": [{"id": k, "name": v["name"], "blurb": v["blurb"]}
                      for k, v in KINDS.items()], "max_turns": MAX_TURNS}
