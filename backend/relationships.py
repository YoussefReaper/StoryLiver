"""Layer 4 - the relationship economy. Fully deterministic, $0 LLM.

Seven scalars per (npc, player): affinity, trust, fear, obligation, love,
loyalty, respect. Nothing here asks a model what a character now feels; the
model is told what they feel.

The delta rule is the repeated trust game. Berg/Dickhaut/McCabe: trust extended
and returned compounds, trust extended and broken collapses far faster than it
was built. So:

  * reciprocated cooperation moves trust up by a shrinking increment
    (diminishing returns - the tenth kindness is worth less than the first)
  * betrayal after trust costs a MULTIPLE of what was built, scaled by how much
    trust existed to break (betraying someone who trusted you is worse)
  * everything decays gently toward a character's disposition, so a relationship
    you stop tending drifts back rather than freezing

Love is slow and gated on trust; loyalty is what actually predicts whether an
NPC covers for you; respect moves on competence, not warmth.
"""
from __future__ import annotations

from . import db, rt

KEYS = ("affinity", "trust", "fear", "obligation", "love", "loyalty", "respect")
LO, HI = -100.0, 100.0

# Deterministic event vocabulary. Every gameplay system emits one of these.
# (affinity, trust, fear, obligation, love, loyalty, respect), then modifiers.
EVENTS = {
    "spoke_kindly":      dict(affinity=4, trust=1),
    "listened":          dict(affinity=3, trust=2),
    "insulted":          dict(affinity=-9, respect=-3),
    "threatened":        dict(affinity=-8, fear=12, trust=-6),
    "helped":            dict(affinity=6, trust=5, obligation=-4, respect=3),
    "helped_at_cost":    dict(affinity=9, trust=9, loyalty=6, respect=6, obligation=-8),
    "kept_promise":      dict(trust=10, respect=5, loyalty=4),
    "broke_promise":     dict(trust=-22, respect=-8, loyalty=-10, affinity=-6),
    "betrayed":          dict(trust=-38, affinity=-22, loyalty=-30, fear=8, respect=-6),
    "saved_life":        dict(affinity=16, trust=16, loyalty=20, love=8, obligation=-25, respect=10),
    "abandoned":         dict(trust=-18, loyalty=-16, affinity=-12),
    "shared_secret":     dict(trust=7, loyalty=5, affinity=4),
    "secret_leaked":     dict(trust=-26, loyalty=-14, affinity=-10),
    "gave_gift":         dict(affinity=5, obligation=-3),
    "took_from":         dict(affinity=-7, trust=-8, respect=-2),
    "defended_publicly": dict(loyalty=9, affinity=7, respect=6, trust=5),
    "sided_against":     dict(loyalty=-14, affinity=-9, trust=-8),
    "showed_competence": dict(respect=8),
    "showed_weakness":   dict(respect=-5, fear=-3),
    "spared":            dict(fear=-6, respect=4, obligation=-6),
    "harmed":            dict(affinity=-14, fear=14, trust=-10, respect=-2),
    "killed_ally_of":    dict(affinity=-30, trust=-25, fear=20, loyalty=-25),
    "romanced":          dict(love=7, affinity=5),
    "rebuffed":          dict(love=-9, affinity=-4),
    # Applied to a WITNESS, not a target - `apply_event` treats it like any
    # other event; the caller (engine._apply_action) is what decides who
    # gets it. Before this, harm moved only the target's scalars: a witness
    # got a memory entry from awareness.witness() and nothing else, so
    # will_snitch/betrayal_pressure - which read these scalars - never
    # reacted to what they had just seen.
    "witnessed_violence": dict(trust=-12, fear=10, respect=-4),
}

# Events where trust collapses rather than erodes.
BETRAYAL_EVENTS = {"betrayed", "broke_promise", "secret_leaked", "abandoned",
                   "killed_ally_of", "sided_against"}

# Which keys decide whether an event was a HARM. Fear is deliberately absent:
# frightening someone raises fear, and counting that as a gain would read
# terrifying a friend as a good turn for the friendship.
_STANDING = ("affinity", "trust", "loyalty", "respect", "love")


def is_bonding(event) -> bool:
    """The mirror of is_harmful, derived from the same table for the same
    reason: a hand-kept second list is a list that falls out of date."""
    spec = EVENTS.get(event)
    return bool(spec) and sum(float(spec.get(k, 0)) for k in _STANDING) > 0


def is_harmful(event) -> bool:
    """Derived from the event table rather than kept as a second hand-written
    list, so adding an event to EVENTS cannot leave it silently unclassified."""
    spec = EVENTS.get(event)
    return bool(spec) and sum(float(spec.get(k, 0)) for k in _STANDING) < 0

# What a character drifts back toward when you stop tending the relationship.
DECAY_PER_TURN = {
    "affinity": 0.985, "trust": 0.99, "fear": 0.94,
    "obligation": 0.97, "love": 0.995, "loyalty": 0.99, "respect": 0.995,
}


def _clamp(v):
    return max(LO, min(HI, float(v)))


def _row(pt_id, npc_id, player):
    return db.row("SELECT * FROM relationships WHERE playthrough_id=? AND src=? AND dst=?",
                  (pt_id, player, npc_id))


def ensure(pt_id, npc_id, player):
    row = _row(pt_id, npc_id, player)
    if row:
        return row
    db.run("INSERT OR IGNORE INTO relationships (playthrough_id,src,dst) VALUES (?,?,?)",
           (pt_id, player, npc_id))
    return _row(pt_id, npc_id, player)


def _diminishing(current, delta):
    """The tenth kindness is worth less than the first; the first cruelty after
    warmth costs more than the tenth after coldness."""
    if delta > 0:
        headroom = (HI - current) / (HI - LO)
        return delta * (0.35 + 0.65 * headroom)
    depth = (current - LO) / (HI - LO)
    return delta * (0.35 + 0.65 * depth)


def apply_event(pt_id, npc_id, player, event, *, turn=0, weight=1.0, note="",
                cause_node=0):
    """The single entry point every other system uses. Returns the new vector
    and the deltas actually applied, for the UI to show."""
    spec = EVENTS.get(event)
    if spec is None:
        return None
    row = ensure(pt_id, npc_id, player)
    if not row:
        return None

    trust_before = row["trust"]
    applied = {}
    new = {}
    betrayal = event in BETRAYAL_EVENTS
    for key in KEYS:
        base = float(spec.get(key, 0)) * float(weight)
        if base == 0:
            new[key] = row[key]
            continue
        if betrayal and key in ("trust", "loyalty", "affinity"):
            # The asymmetry is the whole point. Diminishing returns govern how
            # slowly trust is built; they must NOT soften how fast it collapses,
            # and betraying someone who trusted you costs more than betraying a
            # stranger. Berg/Dickhaut/McCabe, applied literally.
            step = base * (1.0 + max(0.0, trust_before) / 45.0)
        else:
            step = _diminishing(row[key], base)
        step = max(-70.0, min(60.0, step))
        value = _clamp(row[key] + step)
        applied[key] = round(value - row[key], 2)
        new[key] = value

    counters = {
        "interactions": row["interactions"] + 1,
        "betrayals": row["betrayals"] + (1 if event in ("betrayed", "secret_leaked") else 0),
        "kept_promises": row["kept_promises"] + (1 if event == "kept_promise" else 0),
    }
    # Love cannot outrun trust: infatuation without trust is capped.
    new["love"] = _clamp(min(new["love"], max(0.0, new["trust"]) + 25))
    # Loyalty is bounded by affinity plus obligation - nobody is loyal to
    # someone they dislike and owe nothing to.
    new["loyalty"] = _clamp(min(new["loyalty"], new["affinity"] + new["obligation"] + 30))

    db.run(
        "UPDATE relationships SET affinity=?,trust=?,fear=?,obligation=?,love=?,loyalty=?,respect=?,"
        "last_interaction_turn=?,interactions=?,betrayals=?,kept_promises=?"
        " WHERE playthrough_id=? AND src=? AND dst=?",
        (new["affinity"], new["trust"], new["fear"], new["obligation"], new["love"],
         new["loyalty"], new["respect"], turn, counters["interactions"],
         counters["betrayals"], counters["kept_promises"], pt_id, player, npc_id))
    # Remember WHICH event did the damage, not just that damage was done. An
    # NPC drifting toward villain has to be traceable to one thing the player
    # can be shown; a drift with no cause node is a mood meter with a story
    # pasted over it. Only harms overwrite it - a later kindness does not erase
    # the grievance that is still driving them.
    if cause_node and is_harmful(event):
        db.run("UPDATE relationships SET cause_node=?, cause_turn=?"
               " WHERE playthrough_id=? AND src=? AND dst=?",
               (int(cause_node), turn, pt_id, player, npc_id))
    # ...and the same for the act that made somebody YOURS. An engine that
    # can show why a character turned on you and cannot show why one stood by
    # you is an engine that only remembers the bad half.
    elif cause_node and is_bonding(event):
        db.run("UPDATE relationships SET bond_node=?, bond_turn=?"
               " WHERE playthrough_id=? AND src=? AND dst=?",
               (int(cause_node), turn, pt_id, player, npc_id))
    rt.cache_drop(f"sl:pt:{pt_id}:rels", f"sl:pt:{pt_id}:snapshot")
    return {"npc": npc_id, "player": player, "event": event, "note": note,
            "deltas": applied, "values": {k: round(v, 1) for k, v in new.items()}}


def decay(pt_id, turn):
    """Untended relationships drift. One UPDATE for the whole table."""
    sets = ", ".join(f"{k}={k}*{DECAY_PER_TURN[k]}" for k in KEYS)
    db.run(f"UPDATE relationships SET {sets} WHERE playthrough_id=? AND last_interaction_turn < ?",
           (pt_id, turn - 3))
    rt.cache_drop(f"sl:pt:{pt_id}:rels")


# ---------------------------------------------------------------------------
# Derived, still deterministic: what the numbers MEAN
# ---------------------------------------------------------------------------

def disposition(rel) -> str:
    if not rel:
        return "stranger"
    if rel["fear"] >= 55 and rel["affinity"] < 10:
        return "terrified"
    if rel["loyalty"] >= 55:
        return "sworn"
    if rel["love"] >= 45:
        return "devoted"
    if rel["trust"] >= 45 and rel["affinity"] >= 25:
        return "close"
    if rel["betrayals"] > 0 and rel["trust"] < -20:
        return "burned"
    if rel["trust"] <= -35:
        return "hostile"
    if rel["respect"] >= 45 and rel["affinity"] < 15:
        return "wary respect"
    if abs(rel["affinity"]) < 12 and abs(rel["trust"]) < 12:
        return "stranger"
    return "warm" if rel["affinity"] > 0 else "cold"


def will_cover_for(rel) -> bool:
    """Would this character lie to protect the player? Pure arithmetic - this is
    what the betrayal and witness layers call instead of asking a model."""
    if not rel:
        return False
    return (rel["loyalty"] + rel["love"] * 0.5 + rel["trust"] * 0.4
            - rel["fear"] * 0.3 - rel["betrayals"] * 25) >= 40


def will_snitch(rel, severity=3) -> bool:
    """Would they tell someone what you did? Bad treatment plus low loyalty."""
    if not rel:
        return False
    pressure = severity * 8
    resistance = rel["loyalty"] + rel["affinity"] * 0.5 + rel["love"] * 0.6
    grievance = max(0.0, -rel["affinity"]) + max(0.0, -rel["trust"]) * 0.8 + rel["fear"] * 0.5
    return (grievance + pressure - resistance) > 45


def betrayal_pressure(rel) -> float:
    """0..1 - how close this character is to turning on the player. The NPC
    layer only spends a model call when this crosses the threshold."""
    if not rel:
        return 0.0
    grievance = max(0.0, -rel["affinity"]) * 0.5 + max(0.0, -rel["trust"]) * 0.8
    bond = max(0.0, rel["loyalty"]) + max(0.0, rel["love"]) * 0.7 + max(0.0, rel["affinity"]) * 0.4
    raw = (grievance + rel["betrayals"] * 20 - bond) / 100.0
    return round(max(0.0, min(1.0, raw)), 3)


def vector(pt_id, npc_id, player):
    row = _row(pt_id, npc_id, player)
    if not row:
        return None
    return {
        **{k: round(row[k], 1) for k in KEYS},
        "interactions": row["interactions"],
        "betrayals": row["betrayals"],
        "kept_promises": row["kept_promises"],
        "last_interaction_turn": row["last_interaction_turn"],
        "disposition": disposition(row),
        "will_cover": will_cover_for(row),
        "betrayal_pressure": betrayal_pressure(row),
    }


def all_for(pt_id, player):
    rows = db.rows("SELECT * FROM relationships WHERE playthrough_id=? AND src=?", (pt_id, player))
    return {r["dst"]: {
        **{k: round(r[k], 1) for k in KEYS},
        "interactions": r["interactions"], "betrayals": r["betrayals"],
        "kept_promises": r["kept_promises"],
        "disposition": disposition(r), "will_cover": will_cover_for(r),
        "betrayal_pressure": betrayal_pressure(r),
    } for r in rows}


# ---------------------------------------------------------------------------
# Mapping free text -> a deterministic event, so an ordinary typed action still
# moves the numbers without a model being asked to score it.
# ---------------------------------------------------------------------------

import re  # noqa: E402

_PATTERNS = [
    (r"\b(save|saved|rescue|pull(ed)? .* (out|free)|drag(ged)? .* clear)\b", "saved_life"),
    (r"\b(betray|sell(ing)? .* out|turn(ed)? .* in|hand(ed)? .* over)\b", "betrayed"),
    (r"\b(threaten|kill you|hurt you|knife|at knifepoint|or else)\b", "threatened"),
    (r"\b(attack|strike|hit|stab|shoot|punch|beat)\b", "harmed"),
    (r"\b(insult|mock|sneer|spit at|call(ed)? (him|her|them) a)\b", "insulted"),
    (r"\b(promise|swear|give you my word|i will)\b", "kept_promise"),
    (r"\b(help|carry|lift|mend|heal|tend|fix)\b", "helped"),
    (r"\b(defend|stand (up )?for|speak for|vouch)\b", "defended_publicly"),
    (r"\b(tell|confide|admit|confess|share).{0,24}\b(secret|truth|everything)\b", "shared_secret"),
    (r"\b(give|offer|hand|pay|buy).{0,24}\b(gift|coin|money|bread|drink|food|round|meal|ale)\b", "gave_gift"),
    (r"\b(steal|take|rob|pickpocket|lift)\b", "took_from"),
    (r"\b(kiss|love you|hold (his|her|their) hand|embrace)\b", "romanced"),
    (r"\b(listen|hear (him|her|them) out|let (him|her|them) (talk|finish))\b", "listened"),
    (r"\b(spare|let (him|her|them) (go|live))\b", "spared"),
    (r"\b(ask|greet|thank|talk to|speak (to|with)|say)\b", "spoke_kindly"),
]


def classify(text: str) -> str | None:
    """Best-effort deterministic read of a typed action. Returns an event key or
    None - and None simply means the numbers do not move this turn, which is the
    correct outcome for most turns."""
    t = (text or "").lower()
    for pattern, event in _PATTERNS:
        if re.search(pattern, t):
            return event
    return None


def targets(world, text: str, present: list[str]) -> list[str]:
    """Who the action was aimed at. A named character who is present wins; only
    when nobody is named does it fall back to the room, and then only to those
    who could plausibly be the object of it.

    Without this, "I threaten Nessa" moves whoever happens to be standing
    nearest in the state dict, which is worse than not moving anything."""
    t = (text or "").lower()
    named = []
    for npc_id in present:
        npc = world.by_id.get(npc_id)
        if not npc:
            continue
        first = npc["name"].split()[0].lower()
        if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", t):
            named.append(npc_id)
        elif re.search(rf"\b{re.escape(npc_id)}\b", t):
            named.append(npc_id)
    if named:
        return named
    # Nobody named: a broadcast act (a threat to the room, buying a round) still
    # lands on everyone present; a directed one with no name lands on nobody.
    broadcast = re.search(
        r"\b(everyone|the room|them all|the crowd|all of them|aloud|publicly|whole tavern)\b", t)
    return list(present) if broadcast else []
