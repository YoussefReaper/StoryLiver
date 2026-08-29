"""Timeline, entry point, and the AU fork.

Three related ideas, all about the same thing: canon is a starting position,
not a cage.

  TIMELINE - a world has ordered arcs. The World Master maps turns onto them so
  the story has a shape and a sense of where it is, without forcing anyone to
  play every hour of in-world time.

  ENTRY POINT - you may start anywhere on that timeline. Beginning at the last
  arc of a long series is a completely reasonable thing to want, and making
  players replay from episode one to get there is the kind of friction that
  loses them. Choosing a non-default entry seeds world state from that point:
  who is alive, what has already happened, where standings sit.

  AU / CROSSOVER - a free-text premise that forks the seed. "Everyone survives",
  "modern high-school", "Demon Slayer characters in the JJK world". The cards
  stay; the situation changes. This is player-declared NON-canon, which is
  both the honest label and the stronger transformative posture legally.

Storage and selection here are deterministic and free. Generating a NEW arc
list for an unknown world is a bootstrap concern and goes through the existing
world-bootstrap path, which is already budgeted - this module never calls a
model on its own.
"""
from __future__ import annotations

import json
import uuid

from . import db, rt

MAX_PREMISE = 600


class ArcError(ValueError):
    pass


# --------------------------------------------------------------------------
# The timeline
# --------------------------------------------------------------------------

def set_timeline(world_id: str, arcs: list) -> list:
    """Replace a world's arc list. `arcs` is [{name, summary, seeds{}}], in
    story order - the order IS the timeline."""
    db.run("DELETE FROM world_arcs WHERE world_id=?", (world_id,))
    out = []
    for i, arc in enumerate(arcs or []):
        name = (arc.get("name") or "").strip()
        if not name:
            continue
        arc_id = arc.get("id") or f"{world_id}:{i}:{uuid.uuid4().hex[:6]}"
        db.run(
            "INSERT INTO world_arcs (id,world_id,ord,name,summary,seeds,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (arc_id, world_id, i, name, (arc.get("summary") or "").strip(),
             json.dumps(arc.get("seeds") or {}), db.now()),
        )
        out.append(arc_id)
    return timeline(world_id)


def timeline(world_id: str) -> list:
    rows = db.rows("SELECT * FROM world_arcs WHERE world_id=? ORDER BY ord", (world_id,))
    return [{**r, "seeds": db.jload(r["seeds"], {})} for r in rows]


def arc(arc_id: str):
    row = db.row("SELECT * FROM world_arcs WHERE id=?", (arc_id,))
    return {**row, "seeds": db.jload(row["seeds"], {})} if row else None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def choose_entry(pt_id: str, arc_id: str) -> dict:
    """Start the story at a chosen point on the timeline.

    Consensus-gated at the caller (same gate as a character card) - this
    function does the seeding once that gate has passed."""
    target = arc(arc_id)
    if not target:
        raise ArcError("no such arc")

    db.run("UPDATE playthroughs SET arc_id=? WHERE id=?", (arc_id, pt_id))
    seeds = target["seeds"] or {}
    applied = _apply_seeds(pt_id, seeds)
    rt.invalidate_playthrough(pt_id)
    return {"arc_id": arc_id, "name": target["name"], "ord": target["ord"],
            "applied": applied}


def _apply_seeds(pt_id, seeds) -> dict:
    """A seed block says what is already true at this point in the story.

    Supported keys, all optional and all deterministic:
      dead[]        - characters who have already died
      location      - where the party begins
      flags{}       - world flags already set
      standing{}    - faction reputation already earned
    """
    applied = {"dead": 0, "flags": 0, "standing": 0, "location": ""}

    for npc_id in seeds.get("dead") or []:
        db.run("UPDATE npc_state SET alive=0 WHERE playthrough_id=? AND npc_id=?",
               (pt_id, npc_id))
        applied["dead"] += 1

    for key, value in (seeds.get("flags") or {}).items():
        from . import worldstate
        worldstate.set_flag(pt_id, key, value)
        applied["flags"] += 1

    for faction, score in (seeds.get("standing") or {}).items():
        db.run(
            "INSERT INTO faction_rep (playthrough_id,faction_id,player_id,standing)"
            " VALUES (?,?,?,?) ON CONFLICT(playthrough_id,faction_id,player_id)"
            " DO UPDATE SET standing=excluded.standing",
            (pt_id, faction, "user", float(score)))
        applied["standing"] += 1

    if seeds.get("location"):
        db.run("UPDATE playthroughs SET current_location=? WHERE id=?",
               (seeds["location"], pt_id))
        applied["location"] = seeds["location"]

    return applied


def position(pt_id: str, world_id: str) -> dict:
    """Where on the timeline this story currently sits."""
    row = db.row("SELECT arc_id, au_premise FROM playthroughs WHERE id=?", (pt_id,))
    line = timeline(world_id)
    if not row or not row["arc_id"]:
        return {"arc": line[0] if line else None, "index": 0, "total": len(line),
                "au": (row or {}).get("au_premise", "")}
    current = next((a for a in line if a["id"] == row["arc_id"]), None)
    return {"arc": current, "index": current["ord"] if current else 0,
            "total": len(line), "au": row["au_premise"]}


# --------------------------------------------------------------------------
# AU / crossover
# --------------------------------------------------------------------------

def set_au(pt_id: str, premise: str) -> dict:
    """Declare an alternate universe.

    Deliberately free text: the whole value is that a player can write any
    premise at all. It is stored, surfaced in the World Master's context, and
    labelled NON-canon everywhere it appears."""
    premise = (premise or "").strip()[:MAX_PREMISE]
    db.run("UPDATE playthroughs SET au_premise=? WHERE id=?", (premise, pt_id))
    rt.invalidate_playthrough(pt_id)
    return {"au_premise": premise, "canon": not bool(premise)}


def au_directive(pt_id: str) -> str:
    """Injected into the World Master and narrator context when an AU is set.

    A crossover is just an AU whose premise names more than one world, so the
    two are handled here together rather than as separate features the caller
    has to know to combine."""
    row = db.row("SELECT au_premise FROM playthroughs WHERE id=?", (pt_id,))
    premise = (row or {}).get("au_premise") or ""
    if not premise:
        return ""
    cross = crossover_directive(detect_worlds(premise))
    if cross:
        return cross + "\n" + _au_block(premise)
    return _au_block(premise)


def detect_worlds(premise: str) -> list:
    """Which named worlds a crossover premise fuses.

    Deliberately simple: split on the words people actually use to join two
    settings. A wrong guess costs nothing - the premise text is passed through
    verbatim either way, and this only decides whether the extra
    conflict-resolution instruction is worth adding."""
    import re as _re
    parts = _re.split(r"\s+(?:x|X|vs\.?|versus|crossed with|meets|\+|/)\s+|\s+in the\s+",
                      (premise or "").strip())
    names = [p.strip(" .,'\"") for p in parts if len(p.strip()) > 2]
    return names if len(names) > 1 else []


def _au_block(premise: str) -> str:
    return (
        "ALTERNATE UNIVERSE (player-declared, explicitly NOT canon):\n"
        f"  {premise}\n"
        "  The characters keep their voices, values and relationships. The "
        "SITUATION is what changed. Where this premise conflicts with canon, "
        "the premise wins - resolve the conflict consistently and keep "
        "resolving it the same way for the rest of the story."
    )


def crossover_directive(world_names: list) -> str:
    """Two or more named worlds fused. The rule-conflict resolution order is
    the players' to set, not the model's to guess."""
    names = [n for n in (world_names or []) if n]
    if len(names) < 2:
        return ""
    return (
        "CROSSOVER: " + " + ".join(names) + ".\n"
        "  Where two systems collide (magic vs technique, power scaling, "
        "cosmology), resolve in the listed priority order above - the first "
        "world named wins ties. Apply the same resolution every time it "
        "recurs; inconsistency between scenes is the failure mode here."
    )

def ensure_timeline(world) -> list:
    """Every world gets a timeline, including the built-in starters.

    Only forged worlds pass through worldforge.save(), so deriving arcs there
    alone left the starter worlds with no timeline and an empty entry-point
    picker. This is idempotent and free: if arcs already exist it does
    nothing, and if they do not it builds them from the fate the world
    already carries."""
    existing = timeline(world.id)
    if existing:
        return existing
    fate = list(getattr(world, "fated_events", []) or [])
    if not fate:
        return []
    built, dead = [], []
    for i, f in enumerate(fate):
        built.append({
            "id": f"{world.id}:arc{i}",
            "name": f.get("title") or f"Arc {i + 1}",
            "summary": (f.get("desc") or "")[:280],
            "seeds": {
                "dead": list(dead),
                "location": f.get("location") or world.get("start_location"),
                "flags": {f"fate:{p['id']}": True for p in fate[:i] if p.get("id")},
            },
        })
        if f.get("kills"):
            dead.append(f["kills"])
    return set_timeline(world.id, built)
