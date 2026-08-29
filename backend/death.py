"""Death that costs something and never dead-ends a player.

Two failure modes to avoid, and they pull in opposite directions. A death with
no consequence is a null operation - the world shrugs and the stakes were
theatre. A death that ends the session is a player sitting in a chat window
with nothing to type while their friends keep playing. Every resolution here
is designed to sit between those: the character is genuinely gone or genuinely
changed, and the PLAYER still has a seat and a verb.

The resolutions are the proven tabletop and roguelike patterns:
ghost, revive, heir, spirit, legacy, permadeath. Which are OFFERED depends on
the world's modes; which is TAKEN is the table's decision (or the dying
player's pre-set card preference, which exists so nobody is asked to make that
call in the ten seconds after losing a character they liked).

Every death fires a world event. That is the part that makes death mean
something mechanically rather than narratively: the Relationship Economy and
World Awareness both react, deterministically, at $0.
"""
from __future__ import annotations

import json
import uuid

from . import db, memory, modes, relationships, worldstate

# --------------------------------------------------------------------------
# The resolutions
# --------------------------------------------------------------------------

RESOLUTIONS = {
    "ghost": {
        "name": "Spectator", "blurb": "Watch, and whisper to the living. No agency, full awareness.",
        "keeps_character": True, "can_act": False, "can_whisper": True,
        "always_available": True,
    },
    "revive": {
        "name": "Revive", "blurb": "The world's own resurrection rules, at the world's own price.",
        "keeps_character": True, "can_act": True, "can_whisper": True,
        "needs_world_rule": True, "return_penalty": True,
    },
    "heir": {
        "name": "Heir", "blurb": "A new character enters. The world remembers the one you lost.",
        "keeps_character": False, "can_act": True, "can_whisper": True,
        "always_available": True, "new_card": True,
    },
    "spirit": {
        "name": "Spirit Guide", "blurb": "Stay as counsel. Bonuses to the living, no hand of your own.",
        "keeps_character": True, "can_act": False, "can_whisper": True,
        "grants_bonus": True,
    },
    "legacy": {
        "name": "Legacy", "blurb": "You are gone. What you built and broke stays behind you.",
        "keeps_character": False, "can_act": False, "can_whisper": False,
        "always_available": True,
    },
    "permadeath": {
        "name": "Permadeath", "blurb": "The run ends. A fraction of it carries into the next.",
        "keeps_character": False, "can_act": False, "can_whisper": False,
        "hardcore_only": True, "ends_run": True,
    },
}

# What a downed character becomes when the world promised nobody would die.
KNOCKOUT_OUTCOMES = ("injured", "captured", "indebted")


class NotOffered(ValueError):
    pass


def offered(pt_id: str, *, world=None) -> list:
    """Which resolutions this world will actually allow, in offer order."""
    permadeath = modes.permadeath_on(pt_id)
    has_revival = bool(world and getattr(world, "revival_rule", None))
    out = []
    for rid, spec in RESOLUTIONS.items():
        if spec.get("hardcore_only") and not permadeath:
            continue
        if spec.get("needs_world_rule") and not has_revival:
            continue
        out.append({"id": rid, **{k: v for k, v in spec.items()
                                  if k in ("name", "blurb", "can_act", "can_whisper")}})
    return out


# --------------------------------------------------------------------------
# The event itself
# --------------------------------------------------------------------------

def strike(pt_id, *, who, killer="", session_id="", player_id="", world=None,
           cause="") -> dict:
    """A character has been reduced. Decides whether this is a death at all -
    a Cozy world converts it to a knockout - and if it is, records it, fires
    the world event, and returns the resolutions on offer.

    Deterministic and free. No model call happens here; the narrator is told
    about it afterwards through the ordinary turn path."""
    turn = _turn(pt_id)

    if modes.downed_is_knockout(pt_id):
        outcome = KNOCKOUT_OUTCOMES[hash((pt_id, who, turn)) % len(KNOCKOUT_OUTCOMES)]
        memory.add_event(pt_id, turn, who, f"{who} goes down",
                         f"Not dead - {outcome}. This world does not take people.",
                         kind="knockout", importance=4)
        return {"death": False, "knockout": True, "outcome": outcome, "who": who,
                "resolutions": []}

    memory.add_event(pt_id, turn, who, f"{who} dies",
                     cause or "Killed." + (f" By {killer}." if killer else ""),
                     kind="death", importance=5)

    consequences = world_event(pt_id, who=who, killer=killer, world=world)

    if session_id and player_id:
        db.run("UPDATE session_players SET life_state='dead' WHERE session_id=? AND player_id=?",
               (session_id, player_id))

    return {
        "death": True, "knockout": False, "who": who, "killer": killer,
        "turn": turn, "consequences": consequences,
        "resolutions": offered(pt_id, world=world),
        "preset": _preset(pt_id, player_id),
    }


def world_event(pt_id, *, who, killer="", world=None) -> dict:
    """Death is never a null operation.

    Four deterministic consequences, all $0: those who cared grieve (and trust
    the killer less), those who feared them are freed, a power vacuum opens
    where they held standing, and the killer is now someone the world has an
    opinion about. This is the Relationship Economy and World Awareness
    reacting - not prose about them reacting."""
    out = {"mourned_by": [], "killer_cost": [], "vacuum": "", "succession": None}
    npcs = list(getattr(world, "npcs", []) or [])
    turn = _turn(pt_id)

    for npc in npcs:
        nid = npc.get("id")
        if not nid or nid == who:
            continue
        vec = relationships.vector(pt_id, nid, memory.SOLO)
        if not vec:
            continue
        # Grief is proportional to what the relationship actually was. Someone
        # who never met the dead does not mourn them, and the world does not
        # pretend otherwise.
        affinity = float(vec.get("affinity", 0) or 0)
        if affinity >= 15:
            relationships.apply_event(pt_id, nid, memory.SOLO, "showed_weakness",
                                      turn=turn, weight=0.5,
                                      note=f"grieving {who}")
            out["mourned_by"].append(nid)
            # Killing someone's friend is the single most expensive thing you
            # can do to a relationship, and the ledger already has the event.
            if killer and killer != who:
                relationships.apply_event(pt_id, nid, killer, "killed_ally_of",
                                          turn=turn, note=f"killed {who}")
                out["killer_cost"].append(nid)

    dead = next((n for n in npcs if n.get("id") == who), None)
    if dead and dead.get("role"):
        out["vacuum"] = dead["role"]
        memory.add_event(pt_id, turn, "world",
                         f"{dead['role']} stands empty",
                         f"With {dead.get('name', who)} gone, nobody holds it.",
                         kind="vacuum", importance=4)
        # An empty seat used to be a note in the timeline and nothing else -
        # or, worse, would have needed an auto-appointed successor, which is
        # the least interesting thing that can happen when a power dies. It
        # opens a CONTEST instead: everyone with a real claim, an unrest
        # window while it is undecided, and losers who remember losing.
        if world is not None:
            from . import legacy
            out["succession"] = legacy.open_vacuum(
                pt_id, world, dead_id=who, role=dead["role"], turn=turn, killer=killer)

    # A death is a public disturbance whether or not anyone mourned.
    worldstate.set_flag(pt_id, f"death:{who}", True)
    return out


def resolve(pt_id, *, choice, who, session_id="", player_id="", world=None) -> dict:
    """Apply the table's decision."""
    if choice not in RESOLUTIONS:
        raise NotOffered(f"no such resolution {choice!r}")
    available = {r["id"] for r in offered(pt_id, world=world)}
    if choice not in available:
        raise NotOffered(f"{choice} is not offered in this world's modes")

    spec = RESOLUTIONS[choice]
    turn = _turn(pt_id)
    note = ""

    if choice == "revive":
        rule = getattr(world, "revival_rule", None) or {}
        note = rule.get("cost", "The price was paid.")
        memory.add_event(pt_id, turn, who, f"{who} is brought back",
                         f"{note} They return diminished.", kind="revival", importance=5)
    elif choice == "heir":
        note = "A new character enters. The world remembers the one it lost."
        memory.add_event(pt_id, turn, "world", "An heir arrives", note,
                         kind="heir", importance=4)
    elif choice == "spirit":
        note = "They stay as counsel - heard, not felt."
        memory.add_event(pt_id, turn, who, f"{who} lingers", note,
                         kind="spirit", importance=3)
    elif choice == "legacy":
        note = "Gone. What they built and broke stays behind them."
    elif choice == "ghost":
        note = "Present, watching, able only to whisper."
    elif choice == "permadeath":
        note = "The run is over."

    if session_id and player_id:
        db.run("UPDATE session_players SET life_state=?, resolution=? "
               "WHERE session_id=? AND player_id=?",
               ("alive" if spec["can_act"] else "dead", choice, session_id, player_id))

    return {"resolution": choice, "name": spec["name"], "note": note,
            "can_act": spec["can_act"], "can_whisper": spec["can_whisper"],
            "ends_run": bool(spec.get("ends_run")), "needs_new_card": bool(spec.get("new_card"))}


def set_preference(card_id: str, choice: str):
    """A player may decide in advance, on their card, so the question is never
    asked in the worst possible moment."""
    if choice and choice not in RESOLUTIONS:
        raise NotOffered(f"no such resolution {choice!r}")
    db.run("UPDATE cards SET on_death=? WHERE id=?", (choice, card_id))
    return {"on_death": choice}


def _preset(pt_id, player_id):
    if not player_id:
        return ""
    row = db.row("SELECT on_death FROM cards WHERE playthrough_id=? AND player_id=? "
                 "AND on_death != '' ORDER BY updated_at DESC LIMIT 1", (pt_id, player_id))
    return row["on_death"] if row else ""


def _turn(pt_id):
    row = db.row("SELECT current_turn FROM playthroughs WHERE id=?", (pt_id,))
    return row["current_turn"] if row else 0


def catalogue():
    return {"resolutions": [{"id": k, **{kk: vv for kk, vv in v.items()
                                         if kk in ("name", "blurb")}}
                            for k, v in RESOLUTIONS.items()]}
