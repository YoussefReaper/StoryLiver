"""Layer 6 - legacy and world events. Deterministic, $0 LLM.

Everything above this line in the stack answers "what just happened to you".
This layer answers the question a long campaign actually turns on: **what has
the world become because you were in it**, and how much of that do you know?

It builds nothing new from scratch. Every mechanic here is an existing module
surfaced or extended, because the parts were already there and were simply
never joined up:

  CHRONICLE        narrgraph already time-locates every event and awareness
                   already decides who learned what. Joining them gives a
                   world feed that is witness-gated by construction - it can
                   only show a node whose fact this player actually holds.
                   The asymmetry IS the product: a chronicle that showed
                   everything would be a spoiler engine.

  MORAL DRIFT      relationships.py already prices betrayal at trust -38 and
                   already computes betrayal_pressure. An NPC turns villain
                   because YOU broke THEIR trust - there is no world evil
                   meter, and the arc is rendered next to the specific graph
                   node that caused it.

  BELIEFS          awareness already gives facts only to those who perceived
                   them, but a fact an NPC held never became a FEELING. So a
                   character could watch you murder their friend and remain
                   cordial. Gossip closes that: a held fact about a subject
                   moves that holder's opinion of the subject, once, weighted
                   by how sure they are.

  SUCCESSION       death.py opened a vacuum and named it. A vacuum with one
                   obvious heir is an administrative note; a vacuum with three
                   claimants and an unrest window is a civil war. Claims are
                   scored from world lore - faction, role, standing, and who
                   the dead actually trusted.

  THE TRAITOR      betrayal.py already tracks deception on a split, and the
                   relationship ledger already knows who is closest to
                   turning. Teasing that SOMEONE is turning, without naming
                   them, is the whole payload; naming them early destroys it.

  ORGANISATIONS    power here is not an inventory. It is people who will do
                   what you ask. An org makes that addressable, and an order
                   given through a subordinate is witnessed as THEIR act.

  LATE REVEALS     a node the player never learned is not lost - it is owed.
                   A skip or a milestone can hand one back: "while you were
                   away, Layla died." That only lands because the engine
                   genuinely withheld it at the time.

Not one model call in this file. The narrator is told what already happened,
the same contract every other deterministic layer uses.
"""
from __future__ import annotations

import json
import uuid

from . import (authority, awareness, db, llm, memory, narrgraph, relationships,
               worldstate)

# ---------------------------------------------------------------------------
# Moral drift - stages of a character turning, and why
# ---------------------------------------------------------------------------
# Ordered worst-last. Each stage is a read of the SAME seven scalars the rest
# of the engine uses; nothing here is a separate hidden score.
ARC_STAGES = (
    ("sworn",     "Sworn",     "They would take a blow meant for you."),
    ("loyal",     "Loyal",     "They are on your side and say so."),
    ("steady",    "Steady",    "Nothing is wrong between you."),
    ("strained",  "Strained",  "Something has not been said."),
    ("resentful", "Resentful", "They remember what you did."),
    ("plotting",  "Plotting",  "They are looking for the moment."),
    ("villain",   "Villain",   "They are working against you now."),
)
STAGE_INDEX = {s[0]: i for i, s in enumerate(ARC_STAGES)}

# Where an NPC has to sit before the world will call them a villain. Tuned so
# one bad turn cannot do it and a sustained pattern always does: a single
# betrayal (trust -38) lands at "resentful", and it takes a second grievance
# on top of that to reach the end of the ladder.
VILLAIN_PRESSURE = 0.55
PLOTTING_PRESSURE = 0.34


def _stage(vec) -> str:
    """Pure read of the ledger. No randomness, no hidden state."""
    if not vec:
        return "steady"
    pressure = float(vec.get("betrayal_pressure", 0) or 0)
    trust = float(vec.get("trust", 0) or 0)
    loyalty = float(vec.get("loyalty", 0) or 0)
    betrayals = int(vec.get("betrayals", 0) or 0)

    if pressure >= VILLAIN_PRESSURE or (betrayals >= 2 and trust <= -45):
        return "villain"
    if pressure >= PLOTTING_PRESSURE or (betrayals >= 1 and trust <= -30):
        return "plotting"
    if trust <= -15 or betrayals >= 1:
        return "resentful"
    if trust < 0 or float(vec.get("affinity", 0) or 0) < -10:
        return "strained"
    if loyalty >= 55:
        return "sworn"
    # Tuned against what ordinary play actually produces: four public defences
    # of somebody lands loyalty in the low twenties, and a gate above that
    # made the entire good half of the ladder decorative - every character a
    # player had gone out of their way for still read as "steady".
    if loyalty >= 20 or trust >= 35:
        return "loyal"
    return "steady"


def drift(pt_id, world, player=memory.SOLO, *, only_turned=False) -> list:
    """Where every character stands, and the event that put them there.

    The causal node is the point. "Nessa is a villain now" is a status bar.
    "Nessa is a villain now, because on turn 11 you handed her brother to the
    Warden" is a story the player can argue with."""
    out = []
    states = {s["npc_id"]: s for s in memory.all_npc_states(pt_id)}
    for npc in world.npcs:
        nid = npc["id"]
        vec = relationships.vector(pt_id, nid, player)
        if not vec:
            continue
        stage = _stage(vec)
        if only_turned and STAGE_INDEX[stage] < STAGE_INDEX["resentful"]:
            continue
        row = db.row("SELECT cause_node, cause_turn, bond_node, bond_turn"
                     " FROM relationships WHERE playthrough_id=? AND src=? AND dst=?",
                     (pt_id, player, nid))
        cause = _node_view(pt_id, world, row["cause_node"] if row else 0)
        # The act that made them YOURS, kept separately so a later grievance
        # cannot overwrite it and a later kindness cannot erase the grievance.
        # A character can be both things at once, and usually is.
        bond = _node_view(pt_id, world, row["bond_node"] if row else 0)
        label, blurb = next((l, b) for k, l, b in ARC_STAGES if k == stage)
        out.append({
            "npc_id": nid, "name": npc["name"], "role": npc["role"],
            "alive": bool(states.get(nid, {}).get("alive", 1)),
            "stage": stage, "stage_label": label, "stage_blurb": blurb,
            "turned": STAGE_INDEX[stage] >= STAGE_INDEX["resentful"],
            "trust": vec["trust"], "loyalty": vec["loyalty"], "fear": vec["fear"],
            "betrayals": vec["betrayals"],
            "pressure": vec["betrayal_pressure"],
            "disposition": vec["disposition"],
            # None is honest: they drifted, but not from anything recorded.
            # A fabricated cause would be worse than admitting there isn't one.
            "cause": cause,
            "bond": bond,
            # Which way they are facing. The spec asks for the reverse arc in
            # the same breath as the villain one, and an engine that renders
            # only the villains remembers only the bad half of what you did.
            "toward_you": STAGE_INDEX[stage] <= STAGE_INDEX["loyal"],
        })
    out.sort(key=lambda d: (-STAGE_INDEX[d["stage"]], -d["pressure"]))
    return out


def _node_view(pt_id, world, node_id):
    if not node_id:
        return None
    n = narrgraph.node(pt_id, node_id)
    if not n:
        return None
    return {"node_id": n["id"], "turn": n["turn"], "kind": n["kind"],
            "label": n["label"], "detail": n["detail"], "place_id": n["place_id"],
            "place": world.loc_name(n["place_id"]) if n["place_id"] else "",
            "tone": narrgraph.tone_of(n["kind"])}


def allies(pt_id, world, player=memory.SOLO) -> list:
    """Who is yours, and the moment that made them so.

    The exact mirror of the villain board. Both are read out of the same seven
    scalars, and both carry the graph node of the act - "Nessa would take a
    blow for you, because on turn 6 you stood up for her in front of the
    Warden" is the same kind of sentence as the betrayal one, and the product
    is worse for only ever printing one of them."""
    return [d for d in drift(pt_id, world, player) if d["toward_you"]]


# ---------------------------------------------------------------------------
# Beliefs - a fact an NPC holds becomes a feeling they have
# ---------------------------------------------------------------------------
# Which held fact moves which needle. Keyed on the event kind awareness stored,
# so this table and the relationship vocabulary stay the same vocabulary.
BELIEF_EVENTS = {
    "killed_ally_of": "killed_ally_of",
    "harmed": "harmed",
    "threatened": "threatened",
    "betrayed": "betrayed",
    "took_from": "took_from",
    "insulted": "insulted",
    "broke_promise": "broke_promise",
    "secret_leaked": "secret_leaked",
    "helped": "helped",
    "saved_life": "saved_life",
    "defended_publicly": "defended_publicly",
}

# Hearing about it is not seeing it. A second-hand fact moves a relationship
# less than a witnessed one, and confidence already carries how sure they are.
HEARSAY_WEIGHT = {"witnessed": 1.0, "did-it": 0.0, "heard": 0.55,
                  "overheard": 0.45, "private": 0.0, "learned-late": 0.5}


def gossip(pt_id, world, turn) -> list:
    """Turn held facts into held opinions, exactly once per fact per holder.

    This is the missing half of the awareness layer. Facts were distributed
    correctly and then did nothing: an NPC could know you killed their friend
    and greet you warmly, because knowing and feeling were never joined.

    Applies to NPC holders only. The player's own feelings are the player's
    business, and the engine does not get to decide them."""
    pending = db.rows(
        "SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind='npc'"
        " AND applied=0 AND severity>=2 ORDER BY id LIMIT 200", (pt_id,))
    moved = []
    for k in pending:
        db.run("UPDATE knowledge SET applied=1 WHERE id=?", (k["id"],))
        holder, subject = k["holder_id"], k["subject"]
        if not subject or subject == holder:
            continue
        event = _belief_event(k)
        if not event:
            continue
        weight = HEARSAY_WEIGHT.get(k["source"], 0.5) * float(k["confidence"] or 0)
        if weight < 0.12:
            continue
        # apply_event(npc_id=subject, player=holder) writes row(src=holder,
        # dst=subject) - the holder's opinion OF the subject. That is the same
        # direction npc_edges are seeded in and the same direction
        # memory.between_block reads, so the narrator is told it correctly.
        applied = relationships.apply_event(
            pt_id, subject, holder, event, turn=turn, weight=round(weight, 3),
            note=f"{'saw' if k['source'] == 'witnessed' else 'heard about'} it")
        if applied:
            # ...and they can RECALL it. Moving the numbers without writing a
            # memory left a character who had turned against you unable to say
            # why when the narrator asked them - the feeling was there and the
            # reason was not.
            memory.npc_observe(
                pt_id, holder, turn,
                f"I {'saw' if k['source'] == 'witnessed' else 'heard'} it: {k['summary']}",
                importance=min(5, int(k["severity"] or 2) + 1), kind="learned")
            moved.append({"holder": holder, "holder_name": world.npc_name(holder),
                          "subject": subject, "subject_name": world.npc_name(subject),
                          "event": event, "source": k["source"],
                          "confidence": round(float(k["confidence"] or 0), 2),
                          "weight": round(weight, 2),
                          "deltas": applied["deltas"]})
    return moved


def _belief_event(k):
    """The fact key encodes the act kind as its first field before hashing, so
    it cannot be read back. The summary carries it in practice; the stored
    severity plus the subject is what we actually have, so map conservatively
    and never invent an event the record does not support."""
    summary = (k["summary"] or "").lower()
    for kind, event in BELIEF_EVENTS.items():
        if kind.replace("_", " ") in summary or kind in summary:
            return event
    sev = int(k["severity"] or 2)
    if sev >= 5:
        return "killed_ally_of"
    if sev == 4:
        return "harmed"
    if sev == 3:
        return "threatened"
    return None


# ---------------------------------------------------------------------------
# The Chronicle - a world feed that can only show what is known
# ---------------------------------------------------------------------------

def _known_keys(pt_id, player) -> set:
    return {r["fact_key"] for r in db.rows(
        "SELECT fact_key FROM knowledge WHERE playthrough_id=? AND holder_kind='player'"
        " AND holder_id=?", (pt_id, player))}


def chronicle(pt_id, world, player=memory.SOLO, *, limit=60) -> dict:
    """Every event this player is entitled to know, newest first.

    The gate is one rule with no exceptions: a node carrying a fact_key is
    visible only if this player holds that fact. A node with no key was
    written during this player's own turn, which is why it needs no gate - the
    player was standing there. Anything the WORLD did while nobody watched
    carries a key, and stays invisible until somebody tells them."""
    known = _known_keys(pt_id, player)
    rows = db.rows(
        "SELECT * FROM graph_nodes WHERE playthrough_id=? ORDER BY id DESC LIMIT ?",
        (pt_id, limit * 3))
    out = []
    for n in rows:
        key = n["fact_key"] or ""
        if key and key not in known:
            continue
        out.append({
            "id": n["id"], "turn": n["turn"], "day": world.day_for(n["turn"]),
            "kind": n["kind"], "tone": narrgraph.tone_of(n["kind"]),
            "label": n["label"], "detail": n["detail"],
            "actor": n["actor"],
            "actor_name": world.npc_name(n["actor"]) if n["actor"] in world.by_id else n["actor"],
            "place_id": n["place_id"],
            "place": world.loc_name(n["place_id"]) if n["place_id"] else "",
            "weight": n["weight"],
            # How they came to know it. A rumour reads differently from
            # something they watched, and the feed should not flatten that.
            "source": _source_of(pt_id, player, key),
            "gated": bool(key),
        })
        if len(out) >= limit:
            break
    return {"entries": out, "known_facts": len(known), "player": player}


def _source_of(pt_id, player, key):
    if not key:
        return "lived"
    row = db.row("SELECT source FROM knowledge WHERE playthrough_id=? AND holder_kind='player'"
                 " AND holder_id=? AND fact_key=?", (pt_id, player, key))
    return row["source"] if row else "lived"


def unseen_count(pt_id, world, player=memory.SOLO) -> int:
    """How many entries have appeared since this player last opened the feed.
    Stored per player so a room does not share one another's badge."""
    seen = _cursor(pt_id, player)
    entries = chronicle(pt_id, world, player, limit=80)["entries"]
    return sum(1 for e in entries if e["id"] > seen)


def mark_read(pt_id, world, player=memory.SOLO) -> dict:
    entries = chronicle(pt_id, world, player, limit=1)["entries"]
    top = entries[0]["id"] if entries else 0
    _set_cursor(pt_id, player, top)
    return {"cursor": top, "unseen": 0}


def _cursor_key(pt_id, player):
    return f"chronicle:{pt_id}:{player}"


def _cursor(pt_id, player) -> int:
    row = db.row("SELECT data FROM prefs WHERE user_id=?", (_cursor_key(pt_id, player),))
    return int(db.jload(row["data"], {}).get("cursor", 0)) if row else 0


def _set_cursor(pt_id, player, value):
    db.run("INSERT INTO prefs (user_id,data,updated_at) VALUES (?,?,?)"
           " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data,"
           " updated_at=excluded.updated_at",
           (_cursor_key(pt_id, player), json.dumps({"cursor": int(value)}), db.now()))


def _players_at(pt_id, place_id) -> list:
    """Which human players could see this. awareness.witness walks NPCs and the
    actor only, so on its own it teaches a player nothing they did not do
    themselves - a succession opening in the room they are standing in would
    have been invisible to them. There is one shared location today, and split
    players carry their own, exactly as the hunt layer already reads it."""
    from . import betrayal
    pt = db.row("SELECT current_location, session_id FROM playthroughs WHERE id=?", (pt_id,))
    if not pt:
        return []
    ids = [r["player_id"] for r in db.rows(
        "SELECT player_id FROM session_players WHERE session_id=?", (pt["session_id"],))]         if pt["session_id"] else [memory.SOLO]
    out = []
    for pid in ids or [memory.SOLO]:
        split = betrayal.active(pt_id, pid)
        where = (split["destination"] if split and split["destination"]
                 else pt["current_location"])
        if where == place_id:
            out.append(pid)
    return out


def record_world_event(pt_id, world, *, turn, kind, label, detail, actor="",
                       place_id="", weight=3, severity=3, present=None,
                       subject="", told=None) -> dict:
    """The world does something. Who learns it is decided by the same witness
    machinery a player action goes through - no privileged broadcast.

    `told` names players who learn it regardless of where they are standing:
    the one who commissioned it, because their own subordinate reported back.
    Everyone else has to have been there or be told later.

    Returns the fact and the node, so a caller can link a cause to it."""
    place_id = place_id or (world.get("start_location") or "")
    present = present if present is not None else memory.npcs_at(pt_id, world, place_id, turn)
    fact = awareness.witness(
        pt_id, world, actor=actor or "world", kind=kind, summary=label,
        detail=detail, place_id=place_id, turn=turn, severity=severity,
        present=present, subject=subject or actor)
    node_id = narrgraph.add(pt_id, turn, kind, label, detail=detail,
                            place_id=place_id, actor=actor, weight=weight,
                            fact_key=fact["key"])
    # Not gated on `present`: a player alone in a room still sees what happens
    # in it. Location is the gate, and it is the only one.
    seen_by = set(told or []) | set(_players_at(pt_id, place_id))
    for pid in sorted(seen_by):
        awareness.learn(pt_id, "player", pid, key=fact["key"], summary=label,
                        detail=detail, subject=subject or actor, place_id=place_id,
                        turn=turn, confidence=1.0, source="witnessed",
                        severity=severity)
    fact["player_witnesses"] = sorted(seen_by)
    if fact["witnesses"]:
        awareness.spread(pt_id, world, fact, turn=turn,
                         witnesses=fact["witnesses"], severity=severity)
    return {"fact": fact, "node_id": node_id, "key": fact["key"]}


# ---------------------------------------------------------------------------
# Contested succession - a vacuum with claimants, not an heir
# ---------------------------------------------------------------------------

UNREST_TURNS = 8            # how long the seat stays contested
MIN_CLAIMANTS = 2           # a contest with one claimant is not a contest


def open_vacuum(pt_id, world, *, dead_id, role, turn, seat="", killer="") -> dict:
    """A named figure is gone and the seat is open to whoever can take it.

    Claims are scored from what the world already declared - faction, rank,
    proximity to the dead, and standing - so a world that wrote a chancellor
    and a captain gets a chancellor and a captain fighting over it, not two
    interchangeable villagers."""
    vacuum_key = f"vacuum:{dead_id}:{turn}"
    if db.row("SELECT 1 FROM claimants WHERE playthrough_id=? AND vacuum_key=?",
              (pt_id, vacuum_key)):
        return current_vacuum(pt_id, world, vacuum_key=vacuum_key)

    seat = seat or _seat_of(world, dead_id)
    scored = _score_claims(pt_id, world, dead_id=dead_id, role=role, turn=turn)
    if len(scored) < MIN_CLAIMANTS:
        # Never fabricate a person. If the world genuinely has nobody else
        # standing, the seat stays empty and that is the true outcome.
        return {"opened": False, "reason": "nobody left with a claim",
                "vacuum_key": vacuum_key, "claimants": []}

    unrest_until = turn + UNREST_TURNS
    recorded = record_world_event(
        pt_id, world, turn=turn, kind="rupture",
        label=f"{role} stands empty",
        detail=(f"With {world.npc_name(dead_id)} gone, "
                + " and ".join(world.npc_name(c["claimant_id"]) for c in scored[:3])
                + " each have a claim. Nothing is settled."),
        actor=killer or dead_id, place_id=seat, weight=5, severity=4,
        subject=dead_id)

    for c in scored:
        db.run(
            "INSERT OR IGNORE INTO claimants (playthrough_id,vacuum_key,role,seat,dead_id,"
            "claimant_id,claim,basis,status,opened_turn,unrest_until,fact_key)"
            " VALUES (?,?,?,?,?,?,?,?,'claiming',?,?,?)",
            (pt_id, vacuum_key, role, seat, dead_id, c["claimant_id"],
             c["claim"], c["basis"], turn, unrest_until, recorded["key"]))

    worldstate.set_flag(pt_id, f"unrest:{vacuum_key}", True)
    memory.add_event(pt_id, turn, "world", f"{role} is contested",
                     f"{len(scored)} claimants. Unrest until turn {unrest_until}.",
                     kind="vacuum", importance=5, location=seat)
    return current_vacuum(pt_id, world, vacuum_key=vacuum_key)


def _seat_of(world, npc_id):
    npc = world.by_id.get(npc_id) or {}
    faction = awareness.faction_of(world, npc_id) or {}
    return faction.get("seat") or npc.get("start_location") or world.get("start_location") or ""


def _score_claims(pt_id, world, *, dead_id, role, turn) -> list:
    """Deterministic and explainable. Every point of a claim has a stated
    basis, because "why is HE in charge now" is the first thing a player asks."""
    dead_faction = awareness.faction_of(world, dead_id) or {}
    members = set(dead_faction.get("members") or [])
    officers = {o.get("id") for o in (dead_faction.get("officers") or []) if o.get("id")}
    states = {s["npc_id"]: s for s in memory.all_npc_states(pt_id)}
    role_words = {w for w in (role or "").lower().split() if len(w) > 3}

    out = []
    for npc in world.npcs:
        nid = npc["id"]
        if nid == dead_id or not states.get(nid, {}).get("alive", 1):
            continue
        claim, basis = 0.0, []
        if nid in officers:
            claim += 40
            basis.append("already held rank")
        elif nid in members:
            claim += 22
            basis.append("of the same house")
        if role_words and role_words & {w for w in npc["role"].lower().split()}:
            claim += 18
            basis.append("does the same work")
        # Who the DEAD trusted is a claim: an heir named by association is how
        # succession actually works when nothing is written down.
        edge = db.row("SELECT trust, loyalty, respect FROM relationships"
                      " WHERE playthrough_id=? AND src=? AND dst=?", (pt_id, dead_id, nid))
        if edge:
            claim += max(0.0, float(edge["trust"] or 0)) * 0.25
            claim += max(0.0, float(edge["respect"] or 0)) * 0.2
            if (edge["trust"] or 0) >= 30:
                basis.append(f"trusted by {world.npc_name(dead_id)}")
        # What the rest of the town thinks of THEM. This used to read
        # awareness.rep(faction, player) - the faction's standing toward the
        # PLAYER - which says nothing about whether this person can hold a
        # seat, and applied the same number to every member of the faction, so
        # it could not even change the ordering it was pretending to inform.
        # Their peers' respect is the signal that actually discriminates, and
        # it is seeded from world lore (npc_edges) and moved by everything
        # since.
        peers = db.rows("SELECT respect, trust, fear FROM relationships"
                        " WHERE playthrough_id=? AND dst=? AND src!=? AND src!=?",
                        (pt_id, nid, memory.SOLO, nid))
        if peers:
            regard = sum(float(r["respect"] or 0) + float(r["trust"] or 0) * 0.5
                         + float(r["fear"] or 0) * 0.3 for r in peers) / len(peers)
            claim += max(-15.0, min(15.0, regard * 0.25))
            if regard >= 25:
                basis.append("the others already listen to them")
            elif regard <= -25:
                basis.append("nobody here will follow them")
        # A tiny, STABLE tiebreak so two identical claims still order the same
        # way on every read. Deterministic per world, never a die roll.
        claim += (int(uuid.uuid5(uuid.NAMESPACE_OID, f"{dead_id}:{nid}").int) % 100) / 100.0
        # Was 8, which in a small world admitted exactly two claimants - so a
        # succession was always a duel and the beaten side was always one
        # person, who is a grudge rather than an institution. Lowered so a
        # contest usually has a field, which is what makes losing it mean
        # something.
        if claim < 4:
            continue
        out.append({"claimant_id": nid, "name": npc["name"], "role": npc["role"],
                    "claim": round(claim, 2),
                    "basis": ", ".join(basis) or "is simply standing closest"})
    out.sort(key=lambda c: -c["claim"])
    return out[:4]


def current_vacuum(pt_id, world, *, vacuum_key=None) -> dict:
    if vacuum_key:
        rows = db.rows("SELECT * FROM claimants WHERE playthrough_id=? AND vacuum_key=?"
                       " ORDER BY claim DESC", (pt_id, vacuum_key))
    else:
        rows = db.rows("SELECT * FROM claimants WHERE playthrough_id=? AND status='claiming'"
                       " ORDER BY claim DESC", (pt_id,))
    if not rows:
        return {"opened": False, "claimants": [], "unrest": False}
    first = rows[0]
    return {
        "opened": True,
        "vacuum_key": first["vacuum_key"],
        "role": first["role"],
        "seat": first["seat"],
        "seat_name": world.loc_name(first["seat"]) if first["seat"] else "",
        "dead": first["dead_id"],
        "dead_name": world.npc_name(first["dead_id"]),
        "unrest": any(r["status"] == "claiming" for r in rows),
        "unrest_until": first["unrest_until"],
        "opened_turn": first["opened_turn"],
        "fact_key": first["fact_key"],
        "claimants": [{"npc_id": r["claimant_id"], "name": world.npc_name(r["claimant_id"]),
                       "claim": round(r["claim"], 1), "basis": r["basis"],
                       "status": r["status"]} for r in rows],
    }


def vacuums(pt_id, world, player=memory.SOLO) -> list:
    """Every contest this player KNOWS about. A succession crisis three towns
    away that nobody has mentioned is not on their board."""
    known = _known_keys(pt_id, player)
    keys = [r["vacuum_key"] for r in db.rows(
        "SELECT DISTINCT vacuum_key, fact_key FROM claimants WHERE playthrough_id=?", (pt_id,))
        if (r["fact_key"] or "") in known or not r["fact_key"]]
    return [current_vacuum(pt_id, world, vacuum_key=k) for k in keys]


def _jockey(pt_id, world, key, rows, turn) -> list:
    """What happens DURING the unrest. A window in which nothing moves is a
    countdown; a claim that gains and loses ground while the player watches is
    a civil war they can put their thumb on.

    Support is read from the same ledger everything else uses: a claimant the
    town respects gains, one the law wants loses, and a dead one drops out."""
    alive = {s["npc_id"] for s in memory.all_npc_states(pt_id) if s["alive"]}
    shifted = []
    for r in rows:
        cid = r["claimant_id"]
        if cid not in alive:
            if r["status"] == "claiming":
                db.run("UPDATE claimants SET status='dead' WHERE id=?", (r["id"],))
                shifted.append({"claimant": cid, "name": world.npc_name(cid),
                                "delta": 0.0, "why": "is dead"})
            continue
        delta, why = 0.0, []
        # Whether the LAW is interested in this claimant. Read from what the
        # authorities actually know about them, not - as this first did - from
        # the player's own standing, which is the same number for every
        # claimant in a faction and therefore decides nothing.
        wanted = db.row(
            "SELECT COUNT(*) AS n FROM knowledge WHERE playthrough_id=?"
            " AND holder_kind='npc' AND subject=? AND severity>=4", (pt_id, cid))
        if wanted and wanted["n"]:
            delta -= 2.0
            why.append("too many people know what they did")
        # Whoever the PLAYER has backed gains: helping a claimant is the most
        # direct lever the player has on a succession, and it should show.
        vec = relationships.vector(pt_id, cid, memory.SOLO) or {}
        support = float(vec.get("loyalty", 0) or 0) + float(vec.get("trust", 0) or 0)
        if support >= 25:
            delta += 1.5
            why.append("has friends who are speaking for them")
        elif support <= -25:
            delta -= 1.5
            why.append("has made the wrong enemies")
        if delta:
            db.run("UPDATE claimants SET claim=claim+? WHERE id=?", (delta, r["id"]))
            shifted.append({"claimant": cid, "name": world.npc_name(cid),
                            "delta": round(delta, 2), "why": " and ".join(why)})
    return shifted


def unrest_tick(pt_id, world, turn) -> list:
    """Advance every open contest. When the window closes the strongest claim
    holds the seat; the others are recorded as having lost, which is what makes
    a grudge the next arc can use."""
    events = []
    open_keys = {r["vacuum_key"] for r in db.rows(
        "SELECT DISTINCT vacuum_key FROM claimants WHERE playthrough_id=? AND status='claiming'",
        (pt_id,))}
    for key in sorted(open_keys):
        rows = db.rows("SELECT * FROM claimants WHERE playthrough_id=? AND vacuum_key=?"
                       " ORDER BY claim DESC", (pt_id, key))
        if not rows:
            continue
        if turn < rows[0]["unrest_until"]:
            shifted = _jockey(pt_id, world, key, rows, turn)
            if shifted:
                events.append({"vacuum_key": key, "settled": False,
                               "role": rows[0]["role"], "shifts": shifted,
                               "turns_left": rows[0]["unrest_until"] - turn})
            continue
        rows = db.rows("SELECT * FROM claimants WHERE playthrough_id=? AND vacuum_key=?"
                       " ORDER BY claim DESC", (pt_id, key))
        alive = {s["npc_id"] for s in memory.all_npc_states(pt_id) if s["alive"]}
        standing = [r for r in rows if r["claimant_id"] in alive] or rows
        winner = standing[0]
        db.run("UPDATE claimants SET status='won' WHERE playthrough_id=? AND vacuum_key=?"
               " AND claimant_id=?", (pt_id, key, winner["claimant_id"]))
        db.run("UPDATE claimants SET status='lost' WHERE playthrough_id=? AND vacuum_key=?"
               " AND status='claiming'", (pt_id, key))
        worldstate.set_flag(pt_id, f"unrest:{key}", False)
        recorded = record_world_event(
            pt_id, world, turn=turn, kind="twist",
            label=f"{world.npc_name(winner['claimant_id'])} holds {winner['role']}",
            detail=(f"The contest closed. {winner['basis'].capitalize()}. "
                    f"{len(rows) - 1} other claim(s) came to nothing."),
            actor=winner["claimant_id"], place_id=winner["seat"], weight=4, severity=3,
            subject=winner["claimant_id"])
        # The losers do not forget. This is a grudge the traitor layer can read.
        for r in rows:
            if r["claimant_id"] == winner["claimant_id"]:
                continue
            relationships.apply_event(pt_id, winner["claimant_id"], r["claimant_id"],
                                      "sided_against", turn=turn, weight=0.8,
                                      note=f"took {winner['role']} from them")
        # The world does not merely swap one name for another. Whoever LOST
        # closes ranks: a settled contest leaves behind people who came second
        # and remember it, and them forming something is the more interesting
        # half of a succession. This is the "new higher-ups" the spec asks
        # for, and it is founded by the WORLD rather than by a player.
        risen = _higher_ups(pt_id, world, key, rows, winner, turn)

        events.append({"vacuum_key": key, "settled": True,
                       "winner": winner["claimant_id"],
                       "winner_name": world.npc_name(winner["claimant_id"]),
                       "role": winner["role"], "losers": len(rows) - 1,
                       "risen": risen,
                       "node_id": recorded["node_id"], "fact_key": recorded["key"]})
    return events


# What the beaten claimants call themselves. Named from the seat that was lost
# rather than invented, so the new power reads as a consequence of the old one.
RISEN_FORMS = ("The Second {role}", "Those Who Were Passed Over",
               "The {role}'s Shadow", "The Quiet {role}")


def _higher_ups(pt_id, world, key, rows, winner, turn):
    """The losers of a succession become a bloc.

    Only when there were enough of them to be one - a single disappointed
    person is a grudge, not an institution, and pretending otherwise would
    make every death spawn a faction."""
    beaten = [r for r in rows if r["claimant_id"] != winner["claimant_id"]]
    if len(beaten) < 2:
        return None
    if db.row("SELECT 1 FROM orgs WHERE playthrough_id=? AND founder=? AND origin='world'",
              (pt_id, f"world:{key}")):
        return None

    role = winner["role"]
    idx = int(llm.rng(pt_id, key, "risen").random() * len(RISEN_FORMS))
    name = RISEN_FORMS[min(idx, len(RISEN_FORMS) - 1)].format(role=role)[:60]
    org_id = uuid.uuid4().hex[:12]
    db.run("INSERT INTO orgs (id,playthrough_id,founder,name,kind,charter,seat,"
           "founded_turn,doctrine,origin,created_at)"
           " VALUES (?,?,?,?,'order',?,?,?,'envy','world',?)",
           (org_id, pt_id, f"world:{key}", name,
            f"Formed by those who did not get {role}.", winner["seat"], turn,
            db.now()))
    for r in beaten:
        db.run("INSERT OR IGNORE INTO org_members (playthrough_id,org_id,member_kind,"
               "member_id,rank,seniority,joined_turn) VALUES (?,?,'npc',?,'member',?,?)",
               (pt_id, org_id, r["claimant_id"], 2.0, turn))

    recorded = record_world_event(
        pt_id, world, turn=turn, kind="twist",
        label=f"{name} forms",
        detail=(f"The ones who did not get {role} are not going home. "
                f"{len(beaten)} of them, and they have a name now."),
        actor=beaten[0]["claimant_id"], place_id=winner["seat"],
        weight=5, severity=4, subject=beaten[0]["claimant_id"])
    return {"org_id": org_id, "name": name, "members": len(beaten),
            "node_id": recorded["node_id"], "fact_key": recorded["key"]}


# ---------------------------------------------------------------------------
# The hidden traitor - teased, then named, and only ever causally
# ---------------------------------------------------------------------------

TEASE_THRESHOLD = 0.22      # below this, nothing is stirring worth hinting at
REVEAL_THRESHOLD = 0.55     # the ledger says they have already decided

# What the player is told while the name is still withheld. Ordered by heat, so
# the tease genuinely escalates instead of repeating.
TEASE_LINES = (
    (0.22, "Something is off. Someone here has stopped meeting your eye."),
    (0.34, "A conversation stops when you walk in. It has happened twice now."),
    (0.45, "Someone has been asking where you sleep."),
    (0.55, "Whoever it is has stopped waiting for a reason."),
)


def _conceals(vec) -> bool:
    """They are turning, and it does not show.

    Someone who is openly hostile is not a hidden traitor - they are a known
    enemy, and the drift panel already names them out loud. The entire payload
    of this mechanic is the GAP between what the ledger says and what the
    player can see, so a candidate only counts while the surface still reads
    as friendly: they still smile at you, or they would still cover for you.

    The line sits at -35 rather than somewhere tighter because of what the two
    cases actually look like on the ledger. Turning someone through open acts
    (betrayal is affinity -22 a time) drives affinity far past it in two moves;
    turning them through quiet ones - a leaked secret, a grudge picked up
    second-hand - costs a fraction of that per step and reaches the same
    pressure while they are still civil to your face. That gap is the mechanic."""
    return float(vec.get("affinity", 0) or 0) > -35 or bool(vec.get("will_cover"))


def _candidates(pt_id, world, player, *, concealed_only=True) -> list:
    states = {s["npc_id"]: s for s in memory.all_npc_states(pt_id)}
    out = []
    for npc in world.npcs:
        if not states.get(npc["id"], {}).get("alive", 1):
            continue
        vec = relationships.vector(pt_id, npc["id"], player)
        if not vec or float(vec["betrayal_pressure"]) <= 0:
            continue
        if concealed_only and not _conceals(vec):
            continue
        out.append((float(vec["betrayal_pressure"]), npc["id"], vec))
    out.sort(reverse=True, key=lambda c: (c[0], c[1]))
    return out


def traitor_signal(pt_id, world, player=memory.SOLO, *, turn=0,
                   session_id="") -> dict:
    """How close somebody is to turning, WITHOUT saying who.

    The certainty is the thing being withheld. Among Us works because you know
    a traitor might exist and cannot prove which; naming them at heat 0.3 turns
    the best beat in the campaign into a status effect.

    Only CONCEALED candidates count. An NPC who has openly become your enemy is
    already on the drift board with their reasons printed next to them - a
    tease about someone the player can plainly see is not a tease."""
    candidates = _candidates(pt_id, world, player)

    # A player who lied on a split is their own kind of traitor, and the room
    # deserves the same tease about them.
    deceptions = db.rows(
        "SELECT player_id, announced, truth, started_turn FROM splits"
        " WHERE playthrough_id=? AND deception=1", (pt_id,))

    heat = candidates[0][0] if candidates else 0.0
    if deceptions:
        heat = max(heat, 0.4)
    # Someone whose turn is already public does not keep the tease alive.
    open_enemies = len(_candidates(pt_id, world, player, concealed_only=False)) - len(candidates)

    hints = [line for threshold, line in TEASE_LINES if heat >= threshold]
    return {
        "active": heat >= TEASE_THRESHOLD,
        "heat": round(heat, 3),
        # Deliberately absent until reveal_traitor is called. There is no
        # "name" key here to leak into a client that renders whatever it gets.
        "hint": hints[-1] if hints else "",
        "hints": hints,
        "ready": heat >= REVEAL_THRESHOLD,
        "suspects": len(candidates),
        # Counted, never named here - it tells the player how much of the room
        # is already accounted for without telling them who is left.
        "open_enemies": max(0, open_enemies),
        "player_deceptions": len(deceptions),
        "note": "Nobody is named until it is earned.",
    }


def _grudge_chain(pt_id, world, player, npc_id, vec) -> list:
    """Why this person. Never an empty answer, and never an invented one.

    Tried in order of how directly it explains them, because a reveal that
    cannot say WHY is a coin flip with a name on it:

      1. the node the relationship ledger stamped when the harm landed
      2. failing that, anything in the graph at the turn the harm landed
      3. failing that, the worst thing this player knows this person did
      4. and always, last, the grudge state itself - the numbers are a
         reason even when no single scene is

    Step 4 is why this returns something for a character who turned from
    gossip the player never saw. That case is real: an NPC can be poisoned
    against you entirely off-screen, and the honest reveal is "they had their
    reasons and you were never in the room for any of them"."""
    row = db.row("SELECT cause_node, cause_turn FROM relationships"
                 " WHERE playthrough_id=? AND src=? AND dst=?", (pt_id, player, npc_id))
    chain = []
    node = None
    if row and row["cause_node"]:
        node = narrgraph.node(pt_id, row["cause_node"])
    if node is None and row and row["cause_turn"] is not None and row["cause_turn"] >= 0:
        node = db.row("SELECT * FROM graph_nodes WHERE playthrough_id=? AND turn=?"
                      " ORDER BY weight DESC, id DESC LIMIT 1", (pt_id, row["cause_turn"]))
    if node is None:
        node = db.row("SELECT * FROM graph_nodes WHERE playthrough_id=? AND actor=?"
                      " ORDER BY weight DESC, id DESC LIMIT 1", (pt_id, npc_id))
    if node:
        chain.append({"turn": node["turn"], "label": node["label"],
                      "detail": node["detail"], "node_id": node["id"]})

    fact = db.row("SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind='player'"
                  " AND holder_id=? AND subject=? ORDER BY severity DESC, id DESC LIMIT 1",
                  (pt_id, player, npc_id))
    if fact:
        chain.append({"turn": fact["turn_learned"], "label": fact["summary"],
                      "detail": fact["detail"] or f"You learned this: {fact['source']}.",
                      "node_id": None})

    chain.append({
        "turn": vec["last_interaction_turn"],
        "label": f"trust {vec['trust']:+.0f}, loyalty {vec['loyalty']:+.0f}, "
                 f"fear {vec['fear']:+.0f}",
        "detail": (f"{vec['betrayals']} betrayal(s) on the ledger. "
                   + ("Nothing in your record explains the rest of it - whatever "
                      "turned them, you were not in the room for it."
                      if len(chain) == 0 else
                      "This is the grudge state the reveal was read out of.")),
        "node_id": None,
    })
    return chain


def reveal_traitor(pt_id, world, player=memory.SOLO, *, turn=0, force=False) -> dict:
    """Name them, and show the chain that made them.

    Refuses below the threshold unless forced by a climax. A reveal that can
    fire at any moment is not a reveal - the whole payload is that it was
    withheld while it was still true."""
    signal = traitor_signal(pt_id, world, player, turn=turn)
    if not signal["ready"] and not force:
        return {"revealed": False, **signal}

    # A PLAYER who lied about where they went outranks any NPC grievance. In a
    # room, "it was Hazem" is the beat, and betrayal.py has been recording the
    # gap between what they announced and what they did the whole time. Naming
    # an NPC while a human at the table is sitting on an unrevealed deception
    # would be answering the wrong question.
    liar = db.row(
        "SELECT * FROM splits WHERE playthrough_id=? AND deception=1 AND player_id!=?"
        " ORDER BY started_turn DESC LIMIT 1", (pt_id, player))
    if liar:
        chain = [{"turn": liar["started_turn"],
                  "label": f"They said: {liar['announced'][:110]}",
                  "detail": f"What they actually did: {liar['truth'][:160]}",
                  "node_id": None}]
        recorded = record_world_event(
            pt_id, world, turn=turn, kind="betrayal",
            label="The story did not hold",
            detail="What they said and what they did were two different things.",
            actor=liar["player_id"], weight=5, severity=4, subject=liar["player_id"])
        return {"revealed": True, "kind": "player", "npc_id": None,
                "player_id": liar["player_id"], "name": liar["player_id"],
                "role": "one of you", "was_hidden": True,
                "heat": round(signal["heat"], 3),
                "trust": 0, "betrayals": 1, "because": chain,
                "node_id": recorded["node_id"], "fact_key": recorded["key"]}

    # The concealed candidate is the reveal. Only when nobody is concealing -
    # every enemy already declared themselves - does a forced reveal fall back
    # to naming the loudest of them, and it says so rather than pretending it
    # was a secret.
    hidden = _candidates(pt_id, world, player)
    ranked = hidden or (_candidates(pt_id, world, player, concealed_only=False)
                        if force else [])
    if not ranked:
        return {"revealed": False, **signal}
    _, best_id, best_vec = ranked[0]
    best = world.by_id.get(best_id) or {"id": best_id, "name": world.npc_name(best_id),
                                        "role": ""}
    was_hidden = bool(hidden)

    chain = _grudge_chain(pt_id, world, player, best["id"], best_vec)

    recorded = record_world_event(
        pt_id, world, turn=turn, kind="betrayal",
        label=f"It was {best['name']}",
        detail=chain[0]["label"] if chain else "The ledger had been saying so for a while.",
        actor=best["id"], weight=5, severity=4, subject=best["id"])
    return {
        "revealed": True, "kind": "npc", "npc_id": best["id"], "name": best["name"],
        "role": best.get("role", ""),
        # An honest label. "You already knew" is a worse beat than "it was
        # Hazem", and pretending otherwise is how a twist stops being trusted.
        "was_hidden": was_hidden,
        "heat": round(best_vec["betrayal_pressure"], 3),
        "trust": best_vec["trust"], "betrayals": best_vec["betrayals"],
        "because": chain,
        "node_id": recorded["node_id"],
        "fact_key": recorded["key"],
    }


# The traitor does not merely simmer. "An unknown assassin keeps destroying
# everything you build" is the scenario, and a ledger that ticks toward a
# reveal without ever costing the player anything is a progress bar with a
# name at the end of it. So above a threshold, and BEFORE the reveal, the
# concealed one starts working - and the player sees the damage without seeing
# the hand.
ACTS_FROM = 0.34


def grudge_act(pt_id, world, turn, player=memory.SOLO) -> dict | None:
    """One quiet act of sabotage from whoever is turning, unattributed.

    Chosen from what the player actually HAS, so it costs something real: a
    subordinate is turned, or word is put about. Never fires twice in a turn
    and never names anybody - the fact it writes is about the damage, and only
    the reveal ever connects it to a person."""
    hidden = _candidates(pt_id, world, player)
    if not hidden or hidden[0][0] < ACTS_FROM:
        return None
    pressure, npc_id, _vec = hidden[0]
    if db.row("SELECT 1 FROM graph_nodes WHERE playthrough_id=? AND turn=?"
              " AND kind='twist' AND actor=?", (pt_id, turn, npc_id)):
        return None

    # Prefer hurting what they built. An org with people in it is the thing a
    # player would actually miss.
    for org_row in db.rows("SELECT id, name FROM orgs WHERE playthrough_id=? AND"
                           " founder=? AND dissolved=0", (pt_id, player)):
        member = db.row("SELECT * FROM org_members WHERE playthrough_id=? AND org_id=?"
                        " AND member_kind='npc' AND member_id!=? ORDER BY seniority",
                        (pt_id, org_row["id"], npc_id))
        if not member:
            continue
        relationships.apply_event(pt_id, member["member_id"], player, "sided_against",
                                  turn=turn, weight=0.8,
                                  note="somebody got to them")
        db.run("UPDATE org_members SET orders_refused=orders_refused+1 WHERE id=?",
               (member["id"],))
        recorded = record_world_event(
            pt_id, world, turn=turn, kind="twist",
            label=f"Somebody got to {world.npc_name(member['member_id'])}",
            detail=f"{org_row['name']} is a little less yours than it was.",
            actor=npc_id, weight=4, severity=3, subject=npc_id, told=[player])
        return {"kind": "turned_a_subordinate", "pressure": round(pressure, 3),
                "hit": member["member_id"], "org": org_row["name"],
                "node_id": recorded["node_id"]}

    # Nothing built yet, so they go after the name instead.
    for f in awareness.factions(world):
        if npc_id not in (f.get("members") or []):
            continue
        rep = awareness.rep(pt_id, f["id"], player)
        db.run("UPDATE faction_rep SET standing=?, last_turn=? WHERE playthrough_id=?"
               " AND faction_id=? AND player_id=?",
               (max(-100.0, float(rep["standing"]) - 8.0), turn, pt_id, f["id"], player))
        recorded = record_world_event(
            pt_id, world, turn=turn, kind="twist",
            label="Something is being said about you",
            detail=f"{f['name']} has heard a version of events you did not give them.",
            actor=npc_id, weight=3, severity=3, subject=npc_id, told=[player])
        return {"kind": "poisoned_your_name", "pressure": round(pressure, 3),
                "faction": f["id"], "node_id": recorded["node_id"]}
    return None


# ---------------------------------------------------------------------------
# Organisations - power as people who will do what you ask
# ---------------------------------------------------------------------------

ORG_KINDS = {
    "cell":     {"name": "Cell", "blurb": "Small, quiet, deniable."},
    "house":    {"name": "House", "blurb": "A name people already know."},
    "company":  {"name": "Company", "blurb": "Trades, hires, and owes."},
    "order":    {"name": "Order", "blurb": "Bound by a rule everyone swore to."},
    "crew":     {"name": "Crew", "blurb": "Loyal to each other before anything."},
}

# Seven doctrines a cell can be founded on. Not a morality system and not a
# power set - a doctrine is a stated reason people joined, which is what makes
# one cell different from another when both are five people in a room. They
# are offered because the spec asks for them by name; they change what the org
# SAYS it is for, and nothing about what it can do.
DOCTRINES = {
    "pride":    {"name": "Pride", "blurb": "We will be seen. That is the point."},
    "greed":    {"name": "Greed", "blurb": "Everything has a price and we set it."},
    "wrath":    {"name": "Wrath", "blurb": "Somebody is going to answer for it."},
    "envy":     {"name": "Envy", "blurb": "What they have was ours first."},
    "gluttony": {"name": "Gluttony", "blurb": "More. Of everything. Now."},
    "sloth":    {"name": "Sloth", "blurb": "Let it rot. We will be here after."},
    "lust":     {"name": "Lust", "blurb": "Wanting is the whole engine."},
}

RANKS = {"lieutenant": 3.0, "officer": 2.0, "member": 1.0, "hand": 0.5}

# What it takes for a character to accept a place under you. Loyalty is the
# real gate: someone who merely likes you will not take orders from you.
RECRUIT_LOYALTY = 18.0
RECRUIT_TRUST = 10.0


class OrgError(ValueError):
    pass


def found_org(pt_id, world, *, player, name, kind="cell", charter="", turn=0,
              seat="", doctrine="") -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise OrgError("an organisation needs a name")
    if kind not in ORG_KINDS:
        raise OrgError(f"kind must be one of {sorted(ORG_KINDS)}")
    if doctrine and doctrine not in DOCTRINES:
        raise OrgError(f"doctrine must be one of {sorted(DOCTRINES)}")
    if db.row("SELECT 1 FROM orgs WHERE playthrough_id=? AND founder=? AND name=? AND dissolved=0",
              (pt_id, player, name)):
        raise OrgError("you already lead something by that name")

    org_id = uuid.uuid4().hex[:12]
    seat = seat or _player_place(pt_id)
    db.run("INSERT INTO orgs (id,playthrough_id,founder,name,kind,charter,seat,founded_turn,"
           "doctrine,origin,created_at) VALUES (?,?,?,?,?,?,?,?,?,'player',?)",
           (org_id, pt_id, player, name, kind, charter[:400], seat, turn,
            doctrine, db.now()))
    db.run("INSERT OR IGNORE INTO org_members (playthrough_id,org_id,member_kind,member_id,rank,"
           "seniority,joined_turn) VALUES (?,?,'player',?,'founder',?,?)",
           (pt_id, org_id, player, 5.0, turn))
    # Founding something is public. It is witnessed like anything else, which
    # is why a cell founded alone in a cellar is genuinely unknown.
    record_world_event(pt_id, world, turn=turn, kind="bond",
                       label=f"{name} is founded",
                       detail=charter[:200] or f"A {ORG_KINDS[kind]['name'].lower()} takes shape.",
                       actor=player, place_id=seat, weight=3, severity=2, subject=player)
    return org(pt_id, world, org_id)


def recruit(pt_id, world, *, org_id, npc_id, player, turn=0, rank="member") -> dict:
    """Ask a character to serve. Deterministic: they say yes if the ledger says
    they would, and the reason they refuse is the number that stopped them."""
    o = db.row("SELECT * FROM orgs WHERE id=? AND playthrough_id=? AND dissolved=0",
               (org_id, pt_id))
    if not o:
        raise OrgError("no such organisation")
    if o["founder"] != player:
        raise OrgError("only whoever founded it can recruit for it")
    if rank not in RANKS:
        raise OrgError(f"rank must be one of {sorted(RANKS)}")
    if npc_id not in world.by_id:
        raise OrgError("no such character")

    vec = relationships.vector(pt_id, npc_id, player) or {}
    loyalty = float(vec.get("loyalty", 0) or 0)
    trust = float(vec.get("trust", 0) or 0)
    fear = float(vec.get("fear", 0) or 0)
    # Fear recruits too, and it recruits worse people. That asymmetry is the
    # point: an org built on fear carries out fewer orders and refuses louder.
    persuaded = loyalty >= RECRUIT_LOYALTY and trust >= RECRUIT_TRUST
    coerced = not persuaded and fear >= 45
    if not (persuaded or coerced):
        return {"joined": False, "npc_id": npc_id, "name": world.npc_name(npc_id),
                "loyalty": round(loyalty, 1), "trust": round(trust, 1),
                "reason": ("they do not trust you enough yet" if loyalty >= RECRUIT_LOYALTY
                           else "they are not loyal enough to take orders from you"),
                "needs": {"loyalty": RECRUIT_LOYALTY, "trust": RECRUIT_TRUST}}

    db.run("INSERT OR IGNORE INTO org_members (playthrough_id,org_id,member_kind,member_id,"
           "rank,seniority,joined_turn) VALUES (?,?,'npc',?,?,?,?)",
           (pt_id, org_id, npc_id, rank, RANKS[rank], turn))
    relationships.apply_event(pt_id, npc_id, player, "shared_secret", turn=turn,
                              weight=0.7, note=f"took a place in {o['name']}")
    return {"joined": True, "npc_id": npc_id, "name": world.npc_name(npc_id),
            "rank": rank, "coerced": coerced,
            "loyalty": round(loyalty, 1), "trust": round(trust, 1),
            "org": {"id": o["id"], "name": o["name"]}}


def rival_orgs(pt_id, world, player=memory.SOLO) -> list:
    """Organisations you do NOT lead. Somewhere to point an infiltrator."""
    return [org(pt_id, world, r["id"]) for r in db.rows(
        "SELECT id FROM orgs WHERE playthrough_id=? AND founder!=? AND dissolved=0"
        " ORDER BY founded_turn", (pt_id, player))]


def infiltrate(pt_id, world, *, org_id, npc_id, player, turn=0) -> dict:
    """Place one of your people inside somebody else's house.

    Gated on the same ledger everything else uses: they have to be loyal
    enough to take the risk, and the house has to not already know them. What
    it buys is KNOWLEDGE - facts that org's members hold start reaching you,
    which is the only currency this engine has that is worth spying for."""
    target = db.row("SELECT * FROM orgs WHERE id=? AND playthrough_id=? AND dissolved=0",
                    (org_id, pt_id))
    if not target:
        raise OrgError("no such organisation")
    if target["founder"] == player:
        raise OrgError("you cannot infiltrate your own house")
    if db.row("SELECT 1 FROM org_members WHERE playthrough_id=? AND org_id=? AND member_id=?",
              (pt_id, org_id, npc_id)):
        raise OrgError("they are already inside")

    vec = relationships.vector(pt_id, npc_id, player) or {}
    loyalty = float(vec.get("loyalty", 0) or 0)
    if loyalty < RECRUIT_LOYALTY * 1.5:
        return {"placed": False, "npc_id": npc_id, "name": world.npc_name(npc_id),
                "loyalty": round(loyalty, 1),
                "needs": round(RECRUIT_LOYALTY * 1.5, 1),
                "reason": "Not for you. Not for that."}

    db.run("INSERT OR IGNORE INTO org_members (playthrough_id,org_id,member_kind,"
           "member_id,rank,seniority,joined_turn,planted_by)"
           " VALUES (?,?,'npc',?,'member',1,?,?)",
           (pt_id, org_id, npc_id, turn, player))
    # Placed quietly. Deliberately NOT a world event: an infiltration that
    # announced itself would be the one act in this engine that defeats its
    # own purpose.
    return {"placed": True, "npc_id": npc_id, "name": world.npc_name(npc_id),
            "org": target["name"], "org_id": org_id,
            "note": "Nobody was told. That is the point."}


def spy_report(pt_id, world, player=memory.SOLO, *, turn=0) -> list:
    """What your people inside other houses have passed back.

    Reads the knowledge store rather than inventing intelligence: an
    infiltrator can only tell you what they themselves know, which keeps the
    witness gate intact on the one feature designed to get around it."""
    out = []
    for m in db.rows("SELECT * FROM org_members WHERE playthrough_id=? AND planted_by=?",
                     (pt_id, player)):
        # What the HOUSE knows, not what your own person knows - they would
        # have told you that anyway, and reading it back would make an
        # infiltration an expensive way to learn nothing.
        housemates = [r["member_id"] for r in db.rows(
            "SELECT member_id FROM org_members WHERE playthrough_id=? AND org_id=?"
            " AND member_kind='npc' AND member_id!=?",
            (pt_id, m["org_id"], m["member_id"]))]
        if not housemates:
            continue
        marks = ",".join("?" * len(housemates))
        for fact in db.rows(
                f"SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind='npc'"
                f" AND holder_id IN ({marks}) ORDER BY severity DESC, id DESC LIMIT 3",
                (pt_id, *housemates)):
            if awareness.knows(pt_id, "player", player, fact["fact_key"]):
                continue
            # Second-hand and one room removed, so the confidence drops twice.
            # An infiltrator is a channel, not a camera.
            awareness.learn(pt_id, "player", player, key=fact["fact_key"],
                            summary=fact["summary"], detail=fact["detail"],
                            subject=fact["subject"], place_id=fact["place_id"],
                            turn=turn, confidence=max(0.35, float(fact["confidence"]) - 0.25),
                            source="heard", severity=fact["severity"])
            org_row = db.row("SELECT name FROM orgs WHERE id=?", (m["org_id"],))
            out.append({"from": m["member_id"],
                        "name": world.npc_name(m["member_id"]),
                        "inside": (org_row or {}).get("name", ""),
                        "overheard_from": world.npc_name(fact["holder_id"]),
                        "summary": fact["summary"]})
    return out


def command(pt_id, world, *, org_id, npc_id, player, order, turn=0,
            severity=3) -> dict:
    """Give an order THROUGH a subordinate.

    The act enters the world as THEIRS. That is the whole reason to have an
    organisation: the thing gets done, and the person the witnesses saw doing
    it is not you. Whether it is carried out is arithmetic over loyalty, fear
    and how much they have already done for you."""
    o = db.row("SELECT * FROM orgs WHERE id=? AND playthrough_id=? AND dissolved=0",
               (org_id, pt_id))
    if not o:
        raise OrgError("no such organisation")
    if o["founder"] != player:
        raise OrgError("you do not command it")
    m = db.row("SELECT * FROM org_members WHERE playthrough_id=? AND org_id=? AND member_id=?",
               (pt_id, org_id, npc_id))
    if not m:
        raise OrgError("they are not yours to command")
    order = (order or "").strip()[:300]
    if not order:
        raise OrgError("order them to do what?")

    vec = relationships.vector(pt_id, npc_id, player) or {}
    loyalty = float(vec.get("loyalty", 0) or 0)
    fear = float(vec.get("fear", 0) or 0)
    trust = float(vec.get("trust", 0) or 0)
    weight_ = (loyalty + fear * 0.5 + trust * 0.3 + m["seniority"] * 4
               - m["orders_refused"] * 12 - severity * 6)
    carried = weight_ >= 0

    where = memory.npc_state(pt_id, npc_id).get("location") or o["seat"]
    if not carried:
        db.run("UPDATE org_members SET orders_refused=orders_refused+1 WHERE id=?", (m["id"],))
        relationships.apply_event(pt_id, npc_id, player, "insulted", turn=turn,
                                  weight=0.5, note="asked for too much")
        return {"carried": False, "npc_id": npc_id, "name": world.npc_name(npc_id),
                "order": order, "margin": round(weight_, 1),
                "reason": ("they are afraid of you but not enough" if fear > loyalty
                           else "they will not go that far for you")}

    db.run("UPDATE org_members SET orders_carried=orders_carried+1, seniority=seniority+0.25"
           " WHERE id=?", (m["id"],))
    recorded = record_world_event(
        pt_id, world, turn=turn, kind="action",
        label=f"{world.npc_name(npc_id)}: {order[:80]}",
        detail=f"Done on behalf of {o['name']}.",
        actor=npc_id, place_id=where, weight=3, severity=severity,
        subject=npc_id,
        # You commissioned it, so you know it was done - they reported back.
        # The witnesses still saw THEM, which is the whole point of the org.
        told=[player])
    return {"carried": True, "npc_id": npc_id, "name": world.npc_name(npc_id),
            "order": order, "margin": round(weight_, 1),
            "place_id": where, "place": world.loc_name(where),
            "witnesses": recorded["fact"]["witnesses"],
            # The line that matters: the world saw THEM, not you.
            "attributed_to": world.npc_name(npc_id),
            "node_id": recorded["node_id"], "fact_key": recorded["key"]}


def org(pt_id, world, org_id) -> dict:
    o = db.row("SELECT * FROM orgs WHERE id=? AND playthrough_id=?", (org_id, pt_id))
    if not o:
        raise OrgError("no such organisation")
    members = db.rows("SELECT * FROM org_members WHERE playthrough_id=? AND org_id=?"
                      " ORDER BY seniority DESC, id", (pt_id, org_id))
    roster = []
    for m in members:
        if m["member_kind"] == "player":
            roster.append({"kind": "player", "id": m["member_id"], "name": "You",
                           "rank": m["rank"], "seniority": round(m["seniority"], 2),
                           "carried": m["orders_carried"], "refused": m["orders_refused"]})
            continue
        vec = relationships.vector(pt_id, m["member_id"], o["founder"]) or {}
        roster.append({
            "kind": "npc", "id": m["member_id"], "name": world.npc_name(m["member_id"]),
            "role": (world.by_id.get(m["member_id"]) or {}).get("role", ""),
            "rank": m["rank"], "seniority": round(m["seniority"], 2),
            "loyalty": vec.get("loyalty", 0), "fear": vec.get("fear", 0),
            "trust": vec.get("trust", 0),
            "carried": m["orders_carried"], "refused": m["orders_refused"],
            # Somebody who has drifted is still on your roster and is exactly
            # who will refuse the next order. Showing it is the warning.
            "stage": _stage(vec),
        })
    doctrine = o["doctrine"] if "doctrine" in o.keys() else ""
    return {
        "id": o["id"], "name": o["name"], "kind": o["kind"],
        "kind_name": ORG_KINDS.get(o["kind"], {}).get("name", o["kind"]),
        "doctrine": doctrine,
        "doctrine_name": DOCTRINES.get(doctrine, {}).get("name", ""),
        "doctrine_blurb": DOCTRINES.get(doctrine, {}).get("blurb", ""),
        "origin": o["origin"] if "origin" in o.keys() else "player",
        "charter": o["charter"], "founder": o["founder"],
        "seat": o["seat"], "seat_name": world.loc_name(o["seat"]) if o["seat"] else "",
        "founded_turn": o["founded_turn"], "dissolved": bool(o["dissolved"]),
        "members": roster,
        "seniority": round(sum(m["seniority"] for m in members), 2),
        "reach": len([m for m in members if m["member_kind"] == "npc"]),
    }


def orgs_led(pt_id, world, player=memory.SOLO) -> list:
    return [org(pt_id, world, r["id"]) for r in db.rows(
        "SELECT id FROM orgs WHERE playthrough_id=? AND founder=? AND dissolved=0"
        " ORDER BY founded_turn", (pt_id, player))]


def dissolve(pt_id, org_id, player) -> dict:
    o = db.row("SELECT * FROM orgs WHERE id=? AND playthrough_id=?", (org_id, pt_id))
    if not o or o["founder"] != player:
        raise OrgError("not yours to dissolve")
    db.run("UPDATE orgs SET dissolved=1 WHERE id=?", (org_id,))
    return {"dissolved": True, "id": org_id, "name": o["name"]}


# ---------------------------------------------------------------------------
# Monuments - what a dead character left standing
# ---------------------------------------------------------------------------
# "Death is not a reset; make the inheritance VISIBLE." An heir who is told
# nothing about what the last life built has inherited a number, not a legacy.

def monuments(pt_id, world, player=memory.SOLO) -> list:
    """What outlived the people who are gone.

    Reads the same tables everything else does. Nothing is stored as a
    monument - a monument is what an organisation, a seat, or a grief LOOKS
    like once the person at the centre of it is dead."""
    states = {s["npc_id"]: s for s in memory.all_npc_states(pt_id)}
    dead = [n for n in world.npcs if not states.get(n["id"], {}).get("alive", 1)]
    out = []
    for npc in dead:
        built = db.rows(
            "SELECT o.id, o.name, o.kind FROM orgs o JOIN org_members m"
            " ON m.org_id = o.id AND m.playthrough_id = o.playthrough_id"
            " WHERE o.playthrough_id=? AND m.member_id=? AND o.dissolved=0",
            (pt_id, npc["id"]))
        seat = db.row("SELECT role, vacuum_key, status FROM claimants"
                      " WHERE playthrough_id=? AND dead_id=? LIMIT 1",
                      (pt_id, npc["id"]))
        mourners = [r["src"] for r in db.rows(
            "SELECT src FROM relationships WHERE playthrough_id=? AND dst=?"
            " AND affinity >= 25", (pt_id, npc["id"]))]
        if not (built or seat or mourners):
            continue
        out.append({
            "npc_id": npc["id"], "name": npc["name"], "role": npc["role"],
            "built": [{"id": b["id"], "name": b["name"], "kind": b["kind"]}
                      for b in built],
            "seat": (seat or {}).get("role", ""),
            "seat_settled": (seat or {}).get("status", "") in ("won", "lost"),
            "mourners": len(mourners),
            # Said plainly. A monument that needed a paragraph of prose to
            # explain it would not be a monument.
            "line": _monument_line(npc, built, seat, mourners),
        })
    return out


def _monument_line(npc, built, seat, mourners) -> str:
    bits = []
    if built:
        bits.append(f"{built[0]['name']} is still standing")
    if seat:
        bits.append(f"{seat['role']} passed to somebody else")
    if mourners:
        bits.append(f"{len(mourners)} still miss them")
    return (npc["name"] + ": " + ", and ".join(bits) + ".") if bits else ""


# ---------------------------------------------------------------------------
# The Power Panel - what you actually command
# ---------------------------------------------------------------------------

def power_panel(pt_id, world, player=memory.SOLO, *, turn=0) -> dict:
    """Inventory reframed. This world does not measure you in objects; it
    measures you in what you can do, who answers you, and which institutions
    have an opinion about your name."""
    from . import fastforward

    led = orgs_led(pt_id, world, player)
    subordinates = []
    for o in led:
        for m in o["members"]:
            if m["kind"] != "npc":
                continue
            subordinates.append({**m, "org_id": o["id"], "org": o["name"]})
    subordinates.sort(key=lambda s: -s["seniority"])

    skills = fastforward.skills(pt_id, player)
    standing = []
    for f in awareness.factions(world):
        r = awareness.rep(pt_id, f["id"], player)
        if r["known_events"] == 0 and abs(r["standing"]) < 1:
            continue
        tier = authority.tier_for(float(r["standing"]))
        standing.append({"id": f["id"], "name": f["name"],
                         "standing": round(r["standing"], 1),
                         "fear": round(r["fear"], 1),
                         "member": player in (f.get("members") or []),
                         "tier": tier, "tier_blurb": authority.tier_blurb(tier)})

    return {
        "skills": [{"id": k, "name": k.replace("_", " ").title(), "value": v}
                   for k, v in sorted(skills.items(), key=lambda kv: -kv[1])],
        "orgs": led,
        "subordinates": subordinates,
        "standing": standing,
        # One honest number for "how much can you actually make happen".
        "reach": sum(o["reach"] for o in led),
        "seniority": round(sum(o["seniority"] for o in led), 2),
        "empty": not led and not skills and not standing,
    }


# ---------------------------------------------------------------------------
# Late reveals - what happened while you were not looking
# ---------------------------------------------------------------------------

REVEAL_MIN_AGE = 3          # turns; a thing you missed yesterday is not a reveal


def pending_reveals(pt_id, world, player=memory.SOLO, *, turn=0) -> list:
    """Nodes this player was never told about. This is the debt the engine owes
    for having been honest: it withheld these at the time, so it has something
    real to hand back later."""
    known = _known_keys(pt_id, player)
    out = []
    for n in db.rows("SELECT * FROM graph_nodes WHERE playthrough_id=? AND fact_key!=''"
                     " ORDER BY id DESC LIMIT 120", (pt_id,)):
        if n["fact_key"] in known or turn - n["turn"] < REVEAL_MIN_AGE:
            continue
        # Weight by how much this player should CARE. A stranger's quarrel is
        # not a revelation; the death of someone they were close to is.
        care = float(n["weight"])
        if n["actor"] in world.by_id:
            vec = relationships.vector(pt_id, n["actor"], player) or {}
            care += abs(float(vec.get("affinity", 0) or 0)) / 12.0
            care += abs(float(vec.get("trust", 0) or 0)) / 15.0
        out.append({"node_id": n["id"], "turn": n["turn"], "kind": n["kind"],
                    "label": n["label"], "detail": n["detail"],
                    "actor": n["actor"], "fact_key": n["fact_key"],
                    "place_id": n["place_id"], "care": round(care, 2),
                    "age": turn - n["turn"]})
    out.sort(key=lambda r: (-r["care"], -r["turn"]))
    return out


def surface_reveal(pt_id, world, player=memory.SOLO, *, turn=0) -> dict | None:
    """Hand one back. The player learns it the way they learn anything - late,
    second-hand, and from somebody who assumed they already knew."""
    pending = pending_reveals(pt_id, world, player, turn=turn)
    if not pending:
        return None
    top = pending[0]
    awareness.learn(pt_id, "player", player, key=top["fact_key"],
                    summary=top["label"], detail=top["detail"],
                    subject=top["actor"], place_id=top["place_id"],
                    turn=turn, confidence=0.7, source="learned-late",
                    severity=3)
    memory.add_event(pt_id, turn, "world", f"You learn: {top['label']}",
                     f"It happened on turn {top['turn']}. Nobody told you until now.",
                     kind="reveal", importance=4, location=top["place_id"])
    return {**top, "learned_turn": turn,
            "note": f"This happened {top['age']} turns ago."}


# ---------------------------------------------------------------------------
# One call the engine makes per turn
# ---------------------------------------------------------------------------

def at_climax(world, turn) -> bool:
    """The last fated event is the climax, and fate is already scheduled, so
    this needs no separate flag. One turn of slack on either side, because a
    reveal that fires on exactly the fated turn competes with the fated event
    for the same passage."""
    if not world.fated_events:
        return False
    return turn >= world.fated_events[-1]["turn"] - 1


def tick(pt_id, world, turn, *, player=memory.SOLO) -> dict:
    """Everything this layer does on an ordinary turn. Deterministic and $0.

    Order matters: beliefs form from facts that have already been distributed,
    and a succession resolves after the beliefs that might have decided it."""
    beliefs = gossip(pt_id, world, turn)
    settled = unrest_tick(pt_id, world, turn)
    # Whoever is turning does something about it, before the reveal names them.
    sabotage = grudge_act(pt_id, world, turn, player)
    # And anyone you planted passes back what they have heard.
    intel = spy_report(pt_id, world, player, turn=turn)

    # The reveal is supposed to LAND, not sit behind a button the player may
    # never press. At the climax, if the ledger is ready, it fires itself -
    # once, and only once, because the node it writes is its own guard.
    reveal = None
    if at_climax(world, turn) and not _already_revealed(pt_id):
        signal = traitor_signal(pt_id, world, player, turn=turn)
        if signal["ready"]:
            out = reveal_traitor(pt_id, world, player, turn=turn)
            reveal = out if out.get("revealed") else None
    return {"beliefs": beliefs, "successions": settled, "reveal": reveal,
            "sabotage": sabotage, "intel": intel}


def _already_revealed(pt_id) -> bool:
    return bool(db.row("SELECT 1 FROM graph_nodes WHERE playthrough_id=? AND kind='betrayal'"
                       " AND (label LIKE 'It was %' OR label='The story did not hold')",
                       (pt_id,)))


def narrate_line(world, ticked, *, player=memory.SOLO) -> str:
    """What the narrator is told about what the world did on its own.

    Facts only, and terse: this is additive prompt text, and the flat-context
    guarantee outranks a well-phrased instruction. Empty on a quiet turn, so
    an ordinary turn's prompt is byte-identical to what it always was."""
    if not ticked:
        return ""
    lines = []
    for s in ticked.get("successions") or []:
        if s.get("settled"):
            lines.append(f"  - {s['winner_name']} now holds {s['role']}. "
                         f"{s['losers']} other claim(s) failed.")
        elif s.get("shifts"):
            top = s["shifts"][0]
            lines.append(f"  - The contest over {s['role']} is still open "
                         f"({s['turns_left']} turns): {top['name']} {top['why']}.")
    sab = ticked.get("sabotage")
    if sab:
        lines.append("  - Something the player built came apart this turn, and "
                     "nobody can point at who did it. Do NOT name anyone.")
    rev = ticked.get("reveal")
    if rev:
        lines.append(f"  - It is {rev['name']}. They have been working against the "
                     f"player, and it is now out.")
    seen = {m["holder"] for m in (ticked.get("beliefs") or [])}
    if len(seen) >= 2:
        lines.append(f"  - {len(seen)} people changed their mind about someone "
                     f"this turn, from what they saw or were told.")
    if not lines:
        return ""
    return ("\nTHE WORLD MOVED ON ITS OWN (facts - report them, do not invent "
            "more):\n" + "\n".join(lines))


def public(pt_id, world, player=memory.SOLO, *, turn=0) -> dict:
    """The whole layer, as one payload for the client."""
    return {
        "chronicle": chronicle(pt_id, world, player, limit=40),
        "unseen": unseen_count(pt_id, world, player),
        "drift": drift(pt_id, world, player, only_turned=True),
        # The other half of the same ladder. Rendered beside the villains,
        # because "who is yours and why" is the answer to the same question.
        "allies": allies(pt_id, world, player),
        "monuments": monuments(pt_id, world, player),
        "rivals": rival_orgs(pt_id, world, player),
        "vacuums": vacuums(pt_id, world, player),
        "traitor": traitor_signal(pt_id, world, player, turn=turn),
        "power": power_panel(pt_id, world, player, turn=turn),
        "owed": len(pending_reveals(pt_id, world, player, turn=turn)),
    }


def _player_place(pt_id):
    row = db.row("SELECT current_location FROM playthroughs WHERE id=?", (pt_id,))
    return row["current_location"] if row else ""
