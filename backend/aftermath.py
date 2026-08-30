"""What the world does after a fight, a death, or an ending.

This module exists because of a specific structural gap: combat was a sealed
subsystem. It tracked hit points and produced a winner, and then nothing
happened. Nobody died in the world, no relationship moved, nothing entered the
timeline, no faction heard about it. You could kill a village's warden in a
fight and the village would greet you warmly the next turn.

That is the seam every layer meets at, so it gets its own module rather than
being scattered through the combat resolver:

  combat ends  ->  the fallen actually die (or are knocked out, per modes)
               ->  each death fires its world event (grief, vacuum, standing)
               ->  the fight enters the timeline as a real event
               ->  whoever could SEE it learns it, and rumours spread from there
               ->  factions who know react; a killing may put a hunt on you
               ->  if a PLAYER fell, resolutions are offered - never a dead end
               ->  in Hardcore, a player death ends the run and pays out meta

Everything here is deterministic and $0. Not one model call. The narrator is
told about the outcome afterwards through the ordinary turn path, exactly like
every other consequence in this engine: the maths is the truth, the prose
reports it.
"""
from __future__ import annotations

from . import (atlas, awareness, death, memory, modes, narrgraph,
               relationships, runs, worldstate)


def after_combat(pt_id, world, combat_state, outcome, *, session_id="",
                 place_id="", turn=None) -> dict:
    """Called once, when a fight reaches status 'over'.

    `combat_state` is combat.get(combat_id); `outcome` is the winning side
    ('a' or 'b'). Returns everything that changed, for the client to show and
    for the narrator to be handed."""
    turn = _turn(pt_id) if turn is None else turn
    place_id = place_id or _place(pt_id)

    fallen = [e for e in combat_state["combatants"] if e["down"]]
    standing = [e for e in combat_state["combatants"] if not e["down"]]
    winners = [e for e in standing if e["side"] == outcome]

    out = {"deaths": [], "knockouts": [], "player_down": None,
           "witnesses": [], "reputation": [], "hunts": [], "run": None}

    # Who struck the final blow matters for the relationship ledger: killing
    # someone's friend is the most expensive thing you can do to them, and the
    # ledger already knows how to price it.
    killer = _killer(winners)

    for e in fallen:
        struck = death.strike(
            pt_id, who=e["entity_id"], killer=killer,
            cause=f"Fell in the fighting at {world.loc_name(place_id)}.",
            session_id=session_id,
            player_id=e["entity_id"] if e["kind"] == "player" else "",
            world=world)

        if struck.get("knockout"):
            out["knockouts"].append({"who": e["entity_id"], "name": e["name"],
                                     "outcome": struck["outcome"]})
            continue

        # A dead NPC is dead in the WORLD, not merely on the board. Without
        # this the corpse stands back up the moment the fight closes.
        if e["kind"] == "npc":
            memory.kill_npc(pt_id, e["entity_id"])

        entry = {"who": e["entity_id"], "name": e["name"], "kind": e["kind"],
                 "consequences": struck.get("consequences", {}),
                 "resolutions": struck.get("resolutions", []),
                 "preset": struck.get("preset", "")}
        out["deaths"].append(entry)
        if e["kind"] == "player":
            out["player_down"] = entry

    # The fight itself is an event. Without this the timeline has a hole where
    # the most consequential thing in the story happened.
    summary = _summary(combat_state, outcome, out, world)
    memory.add_event(pt_id, turn, "combat", "A fight at " + world.loc_name(place_id),
                     summary, kind="combat", importance=5, location=place_id)
    narrgraph.add(pt_id, turn, "combat", f"Fight at {world.loc_name(place_id)}",
                  detail=summary, place_id=place_id, weight=5)
    atlas.echo(pt_id, turn, summary, place_id=place_id, kind="combat", magnitude=4)

    # Nobody is omniscient - a fight is known only to those who could perceive
    # it, and travels outward from there at the world's own speed.
    if out["deaths"] or out["knockouts"]:
        present = memory.npcs_at(pt_id, world, place_id, turn)
        fact = awareness.witness(
            pt_id, world, actor=killer or "someone",
            kind="killed_ally_of" if out["deaths"] else "harmed",
            summary=summary, detail=summary[:200],
            place_id=place_id, turn=turn, severity=5,
            present=present, subject=killer)
        out["witnesses"] = fact["witnesses"]
        if fact["witnesses"]:
            out["rumours"] = awareness.spread(pt_id, world, fact, turn=turn,
                                              witnesses=fact["witnesses"], severity=5)
            if killer:
                # A killing is the clearest negative act the ledger has.
                out["reputation"] = awareness.adjust_rep(
                    pt_id, world, player=killer, fact=fact, turn=turn, valence=-1)
                if any(d["kind"] == "npc" for d in out["deaths"]):
                    out["hunts"] = awareness.maybe_order_hunt(
                        pt_id, world, player=killer, fact=fact, turn=turn,
                        reps=out["reputation"]) or []

    worldstate.set_flag(pt_id, f"fight:{place_id}:{turn}", True)

    # Hardcore: a player falling ends the run, and the run pays out. This is
    # what makes permadeath a loop instead of a wall.
    if out["player_down"] and modes.permadeath_on(pt_id):
        out["run"] = runs.end(pt_id, reason="died", world_id=_world_id(pt_id), turn=turn)

    out["summary"] = summary
    return out


def _killer(winners):
    """Prefer a player: a player landing the killing blow is the case with
    real consequences attached, and it is the one the ledger should record."""
    for e in winners:
        if e["kind"] == "player":
            return e["entity_id"]
    return winners[0]["entity_id"] if winners else ""


def _summary(combat_state, outcome, out, world):
    dead = [d["name"] for d in out["deaths"]]
    ko = [k["name"] for k in out["knockouts"]]
    who_won = "you" if outcome == "a" else "they"
    bits = [f"The fighting ended; {who_won} held the ground."]
    if dead:
        bits.append(("Killed: " if len(dead) > 1 else "Killed: ") + ", ".join(dead) + ".")
    if ko:
        bits.append("Left standing but broken: " + ", ".join(ko) + ".")
    return " ".join(bits)


def narrate_prompt(result, world) -> str:
    """What the narrator is handed. It reports the outcome; it never decides
    it - the same contract every other consequence in this engine uses."""
    lines = [result["summary"], "",
             "Narrate the aftermath in the moment it happens. These are facts:"]
    for d in result["deaths"]:
        c = d.get("consequences") or {}
        lines.append(f"- {d['name']} is dead.")
        if c.get("vacuum"):
            lines.append(f"  The role of {c['vacuum']} now stands empty.")
        if c.get("mourned_by"):
            lines.append(f"  {len(c['mourned_by'])} character(s) here cared about them.")
    for k in result["knockouts"]:
        lines.append(f"- {k['name']} is down but alive - {k['outcome']}.")
    if result.get("hunts"):
        lines.append("- Someone has been sent after you for this.")
    lines.append("")
    lines.append("Do not invent a rescue, a survivor, or a reversal. Do not ask what "
                 "the player does next.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Endings
# ---------------------------------------------------------------------------

def check_ending(pt_id, world, *, turn=None) -> dict | None:
    """Has this story reached its end? Fate is finite - once the last fated
    event has passed, the run has a natural close, and a story that just keeps
    accepting turns past its own ending is a story with no shape."""
    turn = _turn(pt_id) if turn is None else turn
    if not world.fated_events:
        return None
    last = world.fated_events[-1]
    if turn < last["turn"]:
        return None
    return {"ended": True, "final_event": last["title"], "turn": turn,
            "reason": "fate_complete"}


def close_story(pt_id, world, *, reason="victory", turn=None) -> dict:
    """Settle the run and hand back everything the ending screen needs."""
    turn = _turn(pt_id) if turn is None else turn
    result = runs.end(pt_id, reason=reason, world_id=_world_id(pt_id), turn=turn)
    memory.add_event(pt_id, turn, "world", "The story closes",
                     f"This run ended: {reason}.", kind="ending", importance=5)
    # C4 - the town keeps what it learned about you. Institutions outlive runs.
    from . import authority
    remembered = authority.remember_across_runs(
        pt_id, world, account_id=_user_id(pt_id), player=memory.SOLO, turn=turn)
    # A finished run is where a Legacy or Co-op score is actually earned. It
    # was computed nowhere before this, so two of the three scores on a
    # player's profile could never move at all.
    from . import ladder, modetree
    from . import engine as _engine
    mode_id = _engine.mode_of(pt_id)
    scored = ladder.score_run(
        pt_id, world, player=memory.SOLO, account_id=_user_id(pt_id),
        session_id=_row(pt_id).get("session_id", ""),
        family=modetree.family(mode_id))
    return {"closed": True, "reason": reason, "run": result,
            "town_memory": remembered, "scored": scored,
            "meta": runs.progress(_user_id(pt_id), _world_id(pt_id))}


# ---------------------------------------------------------------------------

def _row(pt_id):
    from . import db
    return db.row("SELECT * FROM playthroughs WHERE id=?", (pt_id,)) or {}


def _turn(pt_id):
    return _row(pt_id).get("current_turn", 0)


def _place(pt_id):
    return _row(pt_id).get("current_location", "")


def _world_id(pt_id):
    return _row(pt_id).get("world_id", "")


def _user_id(pt_id):
    return _row(pt_id).get("user_id", "")
