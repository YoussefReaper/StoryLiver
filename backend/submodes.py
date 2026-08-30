"""What each sub-mode actually IS, mechanically. Deterministic, $0 LLM.

`modetree.py` says a mode exists and how turns are handed out. This says what
you are trying to do in it, and reads the answer out of systems that already
exist rather than scripting one.

The three that needed real machinery rather than a label:

  DETECTIVE     The whodunit is EMERGENT, not authored. A culprit and a victim
                are picked from the world, the killing is staged through the
                ordinary witness path, and the clue trail is simply the set of
                facts that particular act left in particular people's heads.
                Nobody wrote a mystery: the mystery is what the awareness layer
                does when a thing happens where some people can see it. That
                also means an accusation has to be PROVED - you must hold the
                facts, not merely be right, because guessing correctly is not
                detection.

  HIDDEN MASK   Hunters wear the faces of people who live here, and the vote
                has two outcomes that are both real. Pull the mask off a hunter
                and they are exposed. Vote out an actual resident and you have
                killed a person the world knew - grief, a power vacuum, and the
                town remembers that you did it. That asymmetry is the entire
                cost structure of the mode.

  KING OF HILL  Nodes are places on the existing atlas, held through the
                existing authority standing. Holding is not a timer; it is
                having more standing there than anyone else, which means it can
                be taken from you.

The rest state an objective and read progress out of the engine. That is
deliberate: a mode that needed its own subsystem would be a fork, and the
spec's constraint is to surface what is there.
"""
from __future__ import annotations

import json

from . import (atlas, authority, awareness, db, death, legacy, llm, memory,
               modetree, relationships, worldstate)


class SubmodeError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Objectives - one place that answers "what am I doing here"
# ---------------------------------------------------------------------------

def objective(pt_id, world, mode_id, *, player=memory.SOLO, session_id="") -> dict:
    """What this player is trying to do, and how far along they are.

    Every branch reads existing state. None of them stores a parallel progress
    counter, because two counters for one thing is how they end up disagreeing.
    """
    turn = _turn(pt_id)
    spec = modetree.spec(mode_id)
    base = {"mode": mode_id, "name": spec["name"], "goal": spec["objective"],
            "done": False, "progress": 0.0, "detail": ""}

    if mode_id == "detective":
        case = current_case(pt_id)
        if not case:
            return {**base, "detail": "No case open."}
        held = len(_clues_held(pt_id, player, case))
        return {**base, "progress": min(1.0, held / max(1, case["clue_count"])),
                "done": bool(case["solved"]),
                "detail": (f"{held} of {case['clue_count']} threads in hand."
                           if not case["solved"] else "Closed.")}

    if mode_id == "hidden_mask":
        masks = _masks(session_id)
        pulled = [m for m in masks if m["status"] != "hidden"]
        return {**base, "progress": (len(pulled) / len(masks)) if masks else 0.0,
                "done": bool(masks) and all(m["status"] != "hidden" for m in masks),
                "detail": f"{len(pulled)} of {len(masks)} masks off."}

    if mode_id == "king_of_hill":
        held = nodes(pt_id, world, session_id)
        mine = [n for n in held if n["holder"] == player]
        return {**base, "progress": (len(mine) / len(held)) if held else 0.0,
                "done": bool(held) and len(mine) * 2 > len(held),
                "detail": f"You hold {len(mine)} of {len(held)}."}

    if mode_id == "daily":
        return {**base, "progress": min(1.0, turn / DAILY_TURN_CAP),
                "done": turn >= DAILY_TURN_CAP,
                "detail": f"Turn {turn} of {DAILY_TURN_CAP}. Score is what you built."}

    if mode_id in SIDE_RULES:
        state = sides(session_id) if session_id else {"sides": {}, "assigned": False}
        if not state["assigned"]:
            return {**base, "detail": "Sides not dealt yet."}
        if mode_id == "hunt":
            you_are = "the quarry" if state["quarry"] == player else "hunting"
            return {**base, "detail": f"You are {you_are}.",
                    "asymmetric": True, "quarry": state["quarry"]}
        mine = next((t for t, members in state["sides"].items()
                     if any(m["player_id"] == player for m in members)), "")
        return {**base,
                "detail": (f"{len(state['sides'])} sides. You are on {mine}."
                           if mode_id == "teams"
                           else f"{len(state['sides'])} still in it.")}

    if mode_id == "raid":
        v = legacy.current_vacuum(pt_id, world)
        unrest = bool(v.get("unrest"))
        return {**base, "progress": 0.0 if unrest else 1.0, "done": not unrest,
                "detail": ("The unrest is still open." if unrest
                           else "Nothing is currently tearing the world open.")}

    if mode_id == "builder":
        led = legacy.orgs_led(pt_id, world, player)
        reach = sum(o["reach"] for o in led)
        return {**base, "progress": min(1.0, reach / 8.0), "done": False,
                "detail": f"{len(led)} organisation(s), {reach} answering."}

    if mode_id == "ironman":
        alive = not _dead(pt_id, player)
        return {**base, "done": not alive, "progress": min(1.0, turn / 40.0),
                "detail": "One life." if alive else "That life is over."}

    # A mode with no main quest has no progress bar to show, and inventing
    # one out of the turn counter would be the same lie the fate check used
    # to tell.
    if modetree.suppresses_fate(mode_id):
        return {**base, "detail": "No main quest. The world simply runs.",
                "open_ended": True}

    # Story, co-op, chaos, and the plain race modes all end on fate.
    fated = world.fated_events
    if fated:
        last = fated[-1]["turn"]
        return {**base, "progress": min(1.0, turn / max(1, last)),
                "done": turn >= last,
                "detail": f"Turn {turn} of {last}."}
    return {**base, "detail": "Open-ended."}


DAILY_TURN_CAP = 20

# How clearly a witness had to see it before they can give you a face. Below
# this they know a killing happened and nothing more, which is the difference
# between a lead and a proof.
NAMES_A_FACE = 0.6


def _turn(pt_id):
    row = db.row("SELECT current_turn FROM playthroughs WHERE id=?", (pt_id,))
    return row["current_turn"] if row else 0


def _dead(pt_id, player):
    row = db.row("SELECT life_state FROM session_players WHERE player_id=?", (player,))
    return bool(row and row["life_state"] == "dead")


# ---------------------------------------------------------------------------
# DETECTIVE - a whodunit nobody wrote
# ---------------------------------------------------------------------------

PHASES = ("morning", "midday", "evening", "night")


def _stage_killing(pt_id, world, culprit, candidates, turn):
    """Find a victim and an hour with a witness, and make it happen there.

    Scored rather than first-match: an option where somebody saw clearly is
    worth more than one where two people half-heard something, because a case
    with leads and no proof cannot be closed."""
    best = None
    for victim in candidates[:6]:
        for phase in PHASES:
            place = victim["schedule"].get(phase) or victim["start_location"]
            present = [n for n in memory.npcs_at(pt_id, world, place, turn)
                       if n not in (culprit["id"], victim["id"])]
            if not present:
                continue
            clarity = awareness.perception(
                worldstate.local(pt_id, world, place))
            score = (1 if clarity >= NAMES_A_FACE else 0, len(present), clarity)
            if best is None or score > best[0]:
                best = (score, victim, phase, place, present)
    if not best:
        return None
    _, victim, phase, place, present = best
    fact = awareness.witness(
        pt_id, world, actor=culprit["id"], kind="killed_ally_of",
        summary=f"{victim['name']} was killed at {world.loc_name(place)}",
        detail=f"It happened in the {phase}.",
        place_id=place, turn=turn, severity=5, present=present,
        subject=culprit["id"])
    if fact["witnesses"]:
        awareness.spread(pt_id, world, fact, turn=turn,
                         witnesses=fact["witnesses"], severity=5)
    return victim, phase, place, fact


def open_case(pt_id, world, *, turn=0, seed="") -> dict:
    """Stage a killing and let the awareness layer make the mystery.

    Nothing about the solution is authored. A culprit and a victim are chosen
    from the living, the act goes through `awareness.witness` exactly as a
    player's act would, and whoever happened to be in that room at that hour is
    who knows something. The clue trail is the residue."""
    if current_case(pt_id):
        raise SubmodeError("a case is already open")
    living = [n for n in world.npcs
              if memory.npc_state(pt_id, n["id"]) and memory.npc_state(pt_id, n["id"])["alive"]]
    if len(living) < 4:
        raise SubmodeError("this world is too small for a case")

    rng = llm.rng(pt_id, "case", seed)
    order = sorted(living, key=lambda n: n["id"])
    rng.shuffle(order)
    culprit = order[0]

    # A killing nobody could have seen is a legitimate thing for this world to
    # produce and a broken thing for this MODE to open with: the case would be
    # unanswerable, and the player would spend an evening finding that out.
    #
    # So the staging searches the world's own schedules for a (victim, hour)
    # where somebody else is actually in the room, and prefers the one where
    # they could see clearly enough to name a face. Nothing is invented - who
    # is where is the world's data, and the mystery is still whatever the
    # awareness layer makes of it. The only thing guaranteed is that an answer
    # EXISTS to be found.
    staged = _stage_killing(pt_id, world, culprit, order[1:], turn)
    if not staged:
        raise SubmodeError("nowhere in this world could a killing have been seen")
    victim, phase, place, fact = staged

    memory.kill_npc(pt_id, victim["id"])
    death.world_event(pt_id, who=victim["id"], killer=culprit["id"], world=world)

    db.run("INSERT INTO cases (playthrough_id,culprit,victim,place_id,phase,opened_turn,"
           "fact_key,clue_count,solved,created_at) VALUES (?,?,?,?,?,?,?,?,0,?)",
           (pt_id, culprit["id"], victim["id"], place, phase, turn, fact["key"],
            max(1, len(fact["witnesses"])), db.now()))

    # Deliberately NOT `current_case(...)` splatted in: that row holds the
    # culprit, and returning it would hand the answer to the first caller. The
    # case is opened server-side, so only what a player may see comes back.
    return {"opened": True, "victim": victim["id"],
            "victim_name": victim["name"],
            "place": world.loc_name(place), "phase": phase, "turn": turn,
            "witness_count": len(fact["witnesses"]),
            "note": "Nobody wrote this. Ask who was where."}


def current_case(pt_id):
    row = db.row("SELECT * FROM cases WHERE playthrough_id=? ORDER BY id DESC LIMIT 1",
                 (pt_id,))
    return dict(row) if row else None


def _clues_held(pt_id, player, case) -> list:
    """The facts THIS player has actually gathered about the killing. Holding
    them is what makes an accusation proof rather than a guess."""
    return db.rows(
        "SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind='player'"
        " AND holder_id=? AND (fact_key=? OR subject=?)",
        (pt_id, player, case["fact_key"], case["culprit"]))


def casefile(pt_id, world, player=memory.SOLO) -> dict:
    """What the player can show. Witness-gated like everything else: an NPC who
    saw it will tell you if you ask them, and until they do you have nothing."""
    case = current_case(pt_id)
    if not case:
        return {"open": False}
    clues = _clues_held(pt_id, player, case)
    # Who is KNOWN to have seen something. The player learns this by asking, so
    # it grows as they work - it is not a list handed over at the start.
    knowers = [k for k in awareness.who_knows(pt_id, case["fact_key"])
               if k["holder_kind"] == "npc"]
    told = {k["holder_id"] for k in awareness.who_knows(pt_id, case["fact_key"])
            if k["holder_kind"] == "player" and k["holder_id"] == player}
    return {
        "open": True, "solved": bool(case["solved"]),
        "victim": world.npc_name(case["victim"]),
        "place": world.loc_name(case["place_id"]),
        "phase": case["phase"], "turn": case["opened_turn"],
        "clues": [{"summary": c["summary"], "detail": c["detail"],
                   "source": c["source"], "confidence": c["confidence"],
                   "turn": c["turn_learned"],
                   # A clue that names somebody is the only kind you can accuse
                   # on. Showing which is which is how a player knows whether
                   # they have a case or only a suspicion.
                   "names": world.npc_name(c["subject"]) if c["subject"] else "",
                   "is_proof": bool(c["subject"])} for c in clues],
        "proof_held": len([c for c in clues if c["subject"]]),
        "witnesses_known": len(told),
        "witnesses_total": len(knowers),
        "can_accuse": bool(clues),
        # Who is worth asking. The UNION of two things on purpose:
        #
        #   who WORKS there at that hour  - from the world's own schedules
        #   who WAS there                 - from where people actually stood
        #
        # Those two lists can be disjoint, and when the leads came from the
        # schedule alone every lead was a dead end - the player asked three
        # people who were not there, got nothing three times, and learned that
        # the leads are noise. The union keeps the cold leads (a person whose
        # routine puts them there but who was elsewhere is a real and useful
        # dead end) while guaranteeing the witnesses are findable at all.
        "worth_asking": _leads(pt_id, world, case),
        # The suspects are simply everyone still alive who was not the victim.
        # No shortlist, because a shortlist is an author's fingerprint.
        "suspects": [{"id": n["id"], "name": n["name"], "role": n["role"]}
                     for n in world.npcs
                     if n["id"] != case["victim"]
                     and (memory.npc_state(pt_id, n["id"]) or {}).get("alive", 1)],
    }


def question(pt_id, world, npc_id, *, player=memory.SOLO, turn=0) -> dict:
    """Ask somebody what they saw.

    This is the whole verb of the mode, and it is witness-gated on both sides:
    they can only tell you something if they actually know it, and they will
    only tell YOU if the ledger says they would. A stranger who watched the
    killing will look at you and say nothing, which is not an obstacle - it is
    the reason relationships matter in a detective story."""
    case = current_case(pt_id)
    if not case:
        raise SubmodeError("no case is open")
    if npc_id not in world.by_id:
        raise SubmodeError("no such person")

    knows = db.row(
        "SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind='npc'"
        " AND holder_id=? AND fact_key=?", (pt_id, npc_id, case["fact_key"]))
    if not knows:
        return {"told": False, "npc_id": npc_id, "name": world.npc_name(npc_id),
                "reason": "They were not there and have not heard."}

    vec = relationships.vector(pt_id, npc_id, player) or {}
    trust = float(vec.get("trust", 0) or 0)
    fear = float(vec.get("fear", 0) or 0)
    # Talking to somebody about a killing is a risk they take for you. Trust
    # buys it; fear buys it worse and costs something; indifference buys
    # nothing, and saying so plainly is more useful than a vague refusal.
    willing = trust >= 10 or fear >= 40
    if not willing:
        return {"told": False, "npc_id": npc_id, "name": world.npc_name(npc_id),
                "trust": round(trust, 1),
                "reason": "They know something. They will not tell you."}

    # How WELL they saw it is already on the record - awareness computed it from
    # the light and the noise in that room at that hour. Whether this witness
    # can give you a face or only a shape is that number, and nothing else.
    # Without this every witness is equally decisive and the mode is one
    # question long.
    confidence = min(1.0, float(knows["confidence"]))
    saw_face = confidence >= NAMES_A_FACE
    awareness.learn(pt_id, "player", player, key=case["fact_key"],
                    summary=knows["summary"], detail=knows["detail"],
                    # The subject is the culprit. Passing it on ONLY when they
                    # genuinely saw who it was is what makes a held clue proof
                    # rather than a rumour with a name attached.
                    subject=knows["subject"] if saw_face else "",
                    place_id=knows["place_id"],
                    turn=turn, confidence=confidence, source="heard", severity=5)
    if fear >= 40 and trust < 10:
        relationships.apply_event(pt_id, npc_id, player, "threatened", turn=turn,
                                  weight=0.4, note="made to talk")
    return {"told": True, "npc_id": npc_id, "name": world.npc_name(npc_id),
            "summary": knows["summary"], "detail": knows["detail"],
            "confidence": round(confidence, 2),
            "named": world.npc_name(knows["subject"]) if saw_face else "",
            "how": ("They saw who it was."
                    if saw_face else
                    "They saw it happen. They could not tell you who."),
            "coerced": fear >= 40 and trust < 10}


def _leads(pt_id, world, case) -> list:
    alive = lambda nid: (memory.npc_state(pt_id, nid) or {}).get("alive", 1)
    were_there = set(memory.npcs_at(pt_id, world, case["place_id"], case["opened_turn"]))
    out, seen = [], set()
    for n in world.npcs:
        nid = n["id"]
        if nid == case["victim"] or nid in seen or not alive(nid):
            continue
        rota = n["schedule"].get(case["phase"]) == case["place_id"]
        there = nid in were_there
        if not (rota or there):
            continue
        seen.add(nid)
        out.append({
            "id": nid, "name": n["name"],
            # Deliberately does not say "saw it" - being in the room is not the
            # same as having seen, and the witness roll already decided that.
            "why": (f"works {case['phase']} at {world.loc_name(case['place_id'])}"
                    if rota else f"was at {world.loc_name(case['place_id'])} that day"),
        })
    return out


def accuse(pt_id, world, npc_id, *, player=memory.SOLO, turn=0) -> dict:
    """Name them. Being right is not enough - you have to hold something.

    A correct guess with no evidence is refused rather than rewarded, because a
    detective mode that pays out on a coin flip teaches players to flip coins."""
    case = current_case(pt_id)
    if not case:
        raise SubmodeError("no case is open")
    if case["solved"]:
        raise SubmodeError("this case is closed")
    clues = _clues_held(pt_id, player, case)
    # Proof is a clue that NAMES them, not merely a clue about the case.
    # Otherwise one vague lead lets a player point at anybody and be right by
    # arithmetic, which is guessing with extra steps.
    naming = [c for c in clues if c["subject"] == npc_id]
    if not clues:
        return {"accused": npc_id, "correct": False, "proved": False,
                "verdict": "You have nothing to show. Find someone who saw it.",
                "closed": False}
    if not naming:
        return {"accused": npc_id, "name": world.npc_name(npc_id),
                "correct": False, "proved": False, "closed": False,
                "leads": len(clues),
                "verdict": ("You know a killing happened. Nobody has put THEM "
                            "in the room. Find someone who saw a face.")}

    correct = npc_id == case["culprit"]
    db.run("UPDATE cases SET solved=?, accused=? WHERE id=?",
           (1 if correct else 0, npc_id, case["id"]))

    if correct:
        legacy.record_world_event(
            pt_id, world, turn=turn, kind="reveal",
            label=f"{world.npc_name(npc_id)} did it",
            detail="Named, and shown.", actor=npc_id, place_id=case["place_id"],
            weight=5, severity=4, subject=npc_id, told=[player])
    else:
        # A wrong accusation is a public act with a cost, like any other.
        relationships.apply_event(pt_id, npc_id, player, "insulted", turn=turn,
                                  note="accused of a killing they did not do")
    return {
        "accused": npc_id, "name": world.npc_name(npc_id),
        "correct": correct, "proved": True, "closed": correct,
        "evidence": len(naming), "leads": len(clues),
        "verdict": ("That is who it was, and you could show it."
                    if correct else
                    "It was not them. You said it out loud, and they heard."),
    }


# ---------------------------------------------------------------------------
# HIDDEN MASK - the vote that kills somebody either way
# ---------------------------------------------------------------------------

def assign_masks(session_id, pt_id, world, *, hunters, seed="") -> list:
    """Each hunter is given the face of somebody who lives here."""
    if db.row("SELECT 1 FROM masks WHERE session_id=?", (session_id,)):
        raise SubmodeError("masks are already dealt")
    living = sorted([n["id"] for n in world.npcs
                     if (memory.npc_state(pt_id, n["id"]) or {}).get("alive", 1)])
    if len(living) < len(hunters) + 2:
        raise SubmodeError("not enough people here to hide behind")
    rng = llm.rng(session_id, "masks", seed)
    rng.shuffle(living)
    for i, hunter in enumerate(sorted(hunters)):
        db.run("INSERT INTO masks (session_id,playthrough_id,player_id,npc_id,status,"
               "created_at) VALUES (?,?,?,?,'hidden',?)",
               (session_id, pt_id, hunter, living[i], db.now()))
    return _masks(session_id)


def _masks(session_id) -> list:
    return db.rows("SELECT * FROM masks WHERE session_id=?", (session_id,)) if session_id else []


def mask_vote(session_id, pt_id, world, *, npc_id, by=memory.SOLO, turn=0) -> dict:
    """The room votes somebody out. Two outcomes, both of which cost.

    Right: a mask comes off and a hunter is exposed.
    Wrong: a person who actually lived here is dead, the people who cared about
    them grieve, whatever they held stands empty, and the world knows who
    called for it. Nobody gets a free guess."""
    masked = db.row("SELECT * FROM masks WHERE session_id=? AND npc_id=? AND status='hidden'",
                    (session_id, npc_id))
    if masked:
        db.run("UPDATE masks SET status='pulled', pulled_turn=? WHERE id=?",
               (turn, masked["id"]))
        legacy.record_world_event(
            pt_id, world, turn=turn, kind="reveal",
            label=f"The face of {world.npc_name(npc_id)} comes off",
            detail="There was somebody else underneath.",
            actor=npc_id, weight=5, severity=4, subject=npc_id, told=[by])
        return {"pulled": True, "npc_id": npc_id, "name": world.npc_name(npc_id),
                "hunter": masked["player_id"],
                "verdict": "There was somebody else under that face.",
                "cost": None}

    # They were exactly who they appeared to be.
    memory.kill_npc(pt_id, npc_id)
    consequences = death.world_event(pt_id, who=npc_id, killer=by, world=world)
    return {
        "pulled": False, "npc_id": npc_id, "name": world.npc_name(npc_id),
        "verdict": "They were exactly who they said they were.",
        "cost": {
            "mourned_by": len(consequences.get("mourned_by", [])),
            "vacuum": consequences.get("vacuum", ""),
            "succession": bool((consequences.get("succession") or {}).get("opened")),
        },
    }


def masks_public(session_id, world, *, viewer="") -> dict:
    """What the room may see. A hidden mask is never named - that is the game."""
    rows = _masks(session_id)
    return {
        "total": len(rows),
        "pulled": [{"npc_id": r["npc_id"], "name": world.npc_name(r["npc_id"]),
                    "hunter": r["player_id"], "turn": r["pulled_turn"]}
                   for r in rows if r["status"] == "pulled"],
        "hidden": len([r for r in rows if r["status"] == "hidden"]),
        "mine": next((r["npc_id"] for r in rows if r["player_id"] == viewer), None),
    }


# ---------------------------------------------------------------------------
# KING OF THE HILL - held places, not a timer
# ---------------------------------------------------------------------------

def nodes(pt_id, world, session_id="") -> list:
    """Every place worth holding, and who holds it.

    A place is held by whoever the local authority thinks best of - which means
    control is the reputation system, not a separate capture bar, and it can be
    lost by doing something stupid three towns away."""
    out = []
    for loc in world.locations:
        faction = next((f for f in awareness.factions(world)
                        if f.get("seat") == loc["id"]), None)
        holder, best = "", 0.0
        players = _players(session_id)
        for p in players:
            standing = 0.0
            for f in awareness.factions(world):
                if faction and f["id"] != faction["id"]:
                    continue
                rep = awareness.rep(pt_id, f["id"], p)
                standing += float(rep["standing"])
            if standing > best:
                holder, best = p, standing
        out.append({"place_id": loc["id"], "name": loc["name"],
                    "faction": (faction or {}).get("name", ""),
                    "holder": holder, "standing": round(best, 1),
                    "contested": bool(holder) and best < 25})
    return out


# What it takes to take a place, and what taking it costs. Deliberately not a
# capture bar: holding is standing with the faction that owns the ground, so a
# node is won the same way anything else in this engine is won - by being
# somebody those people will side with - and can be lost the same way.
CONTEST_GAIN = 14.0
CONTEST_COST = 6.0


def contest(pt_id, world, *, place_id, player=memory.SOLO, session_id="", turn=0) -> dict:
    """Make a move on a place.

    Public by construction: it goes through the ordinary witness path, so
    taking a node is something the world SEES you do, and the people who see
    it are the people whose opinion decides whether you hold it. Trying to
    take somewhere quietly is a contradiction, and the engine treats it as one.
    """
    loc = world.loc_by_id.get(place_id)
    if not loc:
        raise SubmodeError("no such place")

    faction = next((f for f in awareness.factions(world)
                    if f.get("seat") == place_id), None)
    if not faction:
        faction = next(iter(awareness.factions(world)), None)
    if not faction:
        raise SubmodeError("nobody here holds anything")

    before = float(awareness.rep(pt_id, faction["id"], player)["standing"])
    held = [n for n in nodes(pt_id, world, session_id) if n["place_id"] == place_id]
    holder = held[0]["holder"] if held else ""

    # Standing with the owning faction rises for you and falls for whoever
    # held it - taking a place is taken personally by the people who lose it.
    db.run("UPDATE faction_rep SET standing=?, known_events=known_events+1, last_turn=?"
           " WHERE playthrough_id=? AND faction_id=? AND player_id=?",
           (min(100.0, before + CONTEST_GAIN), turn, pt_id, faction["id"], player))
    if holder and holder != player:
        prior = float(awareness.rep(pt_id, faction["id"], holder)["standing"])
        db.run("UPDATE faction_rep SET standing=?, last_turn=? WHERE playthrough_id=?"
               " AND faction_id=? AND player_id=?",
               (max(-100.0, prior - CONTEST_COST), turn, pt_id, faction["id"], holder))

    recorded = legacy.record_world_event(
        pt_id, world, turn=turn, kind="contest",
        label=f"{world.loc_name(place_id)} changes hands",
        detail=f"A move was made on {world.loc_name(place_id)}.",
        actor=player, place_id=place_id, weight=4, severity=3,
        subject=player, told=[player] + ([holder] if holder else []))

    after = [n for n in nodes(pt_id, world, session_id) if n["place_id"] == place_id]
    return {
        "place_id": place_id, "place": world.loc_name(place_id),
        "faction": faction["name"],
        "was": holder, "now": after[0]["holder"] if after else "",
        "took_it": bool(after) and after[0]["holder"] == player,
        "standing": round(after[0]["standing"], 1) if after else 0.0,
        "witnesses": recorded["fact"]["witnesses"],
        "node_id": recorded["node_id"],
    }


def _players(session_id):
    if not session_id:
        return [memory.SOLO]
    return [r["player_id"] for r in db.rows(
        "SELECT player_id FROM session_players WHERE session_id=? AND role!='spectator'",
        (session_id,))] or [memory.SOLO]


# ---------------------------------------------------------------------------
# SIDES - who is with whom, and who is the quarry
# ---------------------------------------------------------------------------
# Teams needs sides. Hunt needs one player to BE the thing being hunted.
# Battle Royale needs everyone on their own. All three are the same question -
# what side is this seat on - and it lives on session_players.team, which was
# added as a migration and then written by nothing.

SIDE_RULES = {
    "teams": {"sides": 2, "quarry": False},
    "hunt": {"sides": 2, "quarry": True},
    "battle_royale": {"sides": 0, "quarry": False},   # 0 = everyone alone
}


def assign_sides(session_id, mode_id, *, seed="") -> list:
    """Deal sides once, deterministically, so a reconnect cannot reroll them.

    Hunt is the asymmetric one: exactly one seat is the QUARRY and everybody
    else hunts it. That seat is not a disadvantage - it is the other half of
    the mode, and it is dealt before anyone can ask for it."""
    rule = SIDE_RULES.get(mode_id)
    if not rule:
        return []
    seats = [r["player_id"] for r in db.rows(
        "SELECT player_id FROM session_players WHERE session_id=? AND role!='spectator'"
        " ORDER BY joined_at, player_id", (session_id,))]
    if not seats:
        return []
    rng = llm.rng(session_id, "sides", mode_id, seed)
    order = list(seats)
    rng.shuffle(order)

    out = []
    if rule["quarry"]:
        quarry = order[0]
        for pid in seats:
            side = "quarry" if pid == quarry else "hunters"
            role = "quarry" if pid == quarry else "hunter"
            db.run("UPDATE session_players SET team=?, seat_role=? WHERE session_id=?"
                   " AND player_id=?", (side, role, session_id, pid))
            out.append({"player_id": pid, "team": side, "seat_role": role})
        return out

    sides = rule["sides"]
    for i, pid in enumerate(order):
        # 0 sides means Battle Royale: everybody is their own faction, which
        # is what "last faction standing" means when nobody is allied.
        side = pid if sides == 0 else f"side_{i % sides + 1}"
        db.run("UPDATE session_players SET team=?, seat_role='' WHERE session_id=?"
               " AND player_id=?", (side, session_id, pid))
        out.append({"player_id": pid, "team": side, "seat_role": ""})
    return out


def sides(session_id) -> dict:
    rows = db.rows("SELECT player_id, name, team, seat_role FROM session_players"
                   " WHERE session_id=? AND role!='spectator'", (session_id,))
    grouped = {}
    for r in rows:
        grouped.setdefault(r["team"] or "unassigned", []).append(
            {"player_id": r["player_id"], "name": r["name"],
             "seat_role": r["seat_role"]})
    return {"sides": grouped,
            "quarry": next((r["player_id"] for r in rows
                            if r["seat_role"] == "quarry"), ""),
            "assigned": all(r["team"] for r in rows) and bool(rows)}


# ---------------------------------------------------------------------------
# RAID - the world event a party is actually raiding
# ---------------------------------------------------------------------------

def open_raid(pt_id, world, *, turn=0) -> dict:
    """Give a Raid something to put down.

    The objective read legacy.current_vacuum for unrest, so a Raid that nobody
    had died in reported itself finished on turn one. This kills the figure
    with the most standing and lets the succession machinery do the rest - the
    unrest IS the raid, and it is the same unrest any death produces."""
    from . import death as _death
    if legacy.current_vacuum(pt_id, world).get("unrest"):
        return {"opened": False, "reason": "something is already tearing this world open"}

    states = {st["npc_id"]: st for st in memory.all_npc_states(pt_id)}
    candidates = [n for n in world.npcs
                  if n.get("role") and states.get(n["id"], {}).get("alive", 1)]
    if not candidates:
        raise SubmodeError("nobody here holds anything worth fighting over")

    # Whoever the most people defer to. Scored, not random: a raid that opened
    # on a well-digger would not read as a raid.
    def weight(npc):
        peers = db.rows("SELECT respect, fear FROM relationships WHERE playthrough_id=?"
                        " AND dst=? AND src!=?", (pt_id, npc["id"], memory.SOLO))
        return sum(float(p["respect"] or 0) + float(p["fear"] or 0) for p in peers)

    target = max(candidates, key=weight)
    memory.kill_npc(pt_id, target["id"])
    out = _death.world_event(pt_id, who=target["id"], killer="", world=world)
    return {"opened": True, "who": target["id"], "name": target["name"],
            "role": target.get("role", ""),
            "succession": out.get("succession"),
            "note": "The seat is open and the world is coming apart over it."}


# ---------------------------------------------------------------------------
# DAILY - the score everybody is compared on
# ---------------------------------------------------------------------------

def daily_score(pt_id, world, player=memory.SOLO) -> dict:
    """One number, from things the player visibly did.

    Built out of what the world became rather than turns survived, so the board
    rewards playing well rather than playing long - which matters when everyone
    is on the same map and the only variable is what you did with it."""
    places = len(db.rows("SELECT 1 FROM atlas_places WHERE playthrough_id=? AND"
                         " status!='hidden'", (pt_id,)))
    known = len(db.rows("SELECT 1 FROM knowledge WHERE playthrough_id=? AND"
                        " holder_kind='player' AND holder_id=?", (pt_id, player)))
    bonds = db.rows("SELECT trust, loyalty FROM relationships WHERE playthrough_id=?"
                    " AND src=?", (pt_id, player))
    warmth = sum(max(0.0, float(b["trust"] or 0)) + max(0.0, float(b["loyalty"] or 0))
                 for b in bonds)
    orgs = legacy.orgs_led(pt_id, world, player)
    reach = sum(o["reach"] for o in orgs)

    parts = {
        "places found": places * 10,
        "things learned": known * 4,
        "trust built": int(warmth),
        "people who answer you": reach * 25,
    }
    return {"score": int(sum(parts.values())), "parts": parts,
            "turns": _turn(pt_id), "cap": DAILY_TURN_CAP}


# ---------------------------------------------------------------------------
# One public payload
# ---------------------------------------------------------------------------

def public(pt_id, world, mode_id, *, player=memory.SOLO, session_id="") -> dict:
    out = {"objective": objective(pt_id, world, mode_id, player=player,
                                  session_id=session_id)}
    if mode_id == "detective":
        out["case"] = casefile(pt_id, world, player)
    if mode_id == "hidden_mask":
        out["masks"] = masks_public(session_id, world, viewer=player)
    if mode_id == "king_of_hill":
        out["nodes"] = nodes(pt_id, world, session_id)
    if mode_id in SIDE_RULES and session_id:
        out["sides"] = sides(session_id)
    if mode_id == "daily":
        out["daily"] = daily_score(pt_id, world, player)
    return out
