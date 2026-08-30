"""Layer 0 - the narrative graph. Deterministic, $0 LLM.

"You are here", the trail behind you, and the forks the world is holding open.
Nodes are written by the engine as the story resolves; future forks are read
straight out of the fate schedule and any live pressure (a hunt en route, a
split party, a Director beat that has been set up but not landed).

WhatIF's finding, applied: a branch view is only useful if edges are coloured
by what the branch *is*, not merely that it exists.
"""
from __future__ import annotations

from . import db

KIND_TONE = {
    "arrival": "calm", "action": "calm", "beat": "tension", "fate": "fate",
    "contest": "tension", "betrayal": "tension", "reveal": "tension",
    "combat": "danger", "death": "danger", "discovery": "hope",
    "bond": "hope", "rupture": "danger", "twist": "tension", "future": "unknown",
}


def add(pt_id, turn, kind, label, *, detail="", place_id="", actor="", weight=2, after=None,
        fact_key=""):
    """`fact_key` is what gates the node. A node written during the player's own
    turn leaves it empty - the player was there, by construction. A node the
    WORLD wrote while nobody was watching carries the key of the fact it came
    from, and the Chronicle then shows it only to whoever learned that fact.
    Without the key there is no way to tell "you did this" from "this happened
    somewhere you have never been", and the graph leaks the whole world."""
    node_id = db.run(
        "INSERT INTO graph_nodes (playthrough_id,turn,kind,label,detail,place_id,actor,weight,"
        "fact_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pt_id, turn, kind, label[:120], detail[:400], place_id, actor, int(weight),
         fact_key, db.now()))
    prev = after if after is not None else last_id(pt_id, exclude=node_id)
    if prev:
        db.run("INSERT INTO graph_edges (playthrough_id,src,dst,kind) VALUES (?,?,?,?)",
               (pt_id, prev, node_id, "then"))
    return node_id


def last_id(pt_id, exclude=None):
    row = db.row(
        "SELECT id FROM graph_nodes WHERE playthrough_id=? AND id!=? ORDER BY id DESC LIMIT 1",
        (pt_id, exclude or -1))
    return row["id"] if row else None


def link(pt_id, src, dst, kind="causes"):
    db.run("INSERT INTO graph_edges (playthrough_id,src,dst,kind) VALUES (?,?,?,?)",
           (pt_id, src, dst, kind))


def _futures(pt_id, world, turn, extra):
    """Faint forks ahead: the next fated events, plus whatever is actually
    in flight. These are real commitments the engine will honour, not guesses."""
    out = []
    upcoming = [f for f in world.fated_events if f["turn"] > turn][:3]
    for i, f in enumerate(upcoming):
        # A mark on the thread, never its content. This panel was printing the
        # TITLE and DESCRIPTION of the next three fated events - the same leak
        # the Fate panel had, on a different screen. "The Warden falls" is not
        # something a player should be able to read twenty-six turns early,
        # and the id is a slug that carries the title too.
        out.append({
            "id": f"future_fate_{f['turn']}", "turn": f["turn"], "kind": "fate",
            "label": "Something lands here", "detail": "",
            # The place is a spoiler of its own when only one thing happens
            # there, so it does not travel either.
            "place_id": "",
            "certainty": "sealed", "distance": f["turn"] - turn, "lane": i,
        })
    for item in extra or []:
        out.append({**item, "certainty": item.get("certainty", "open")})
    return out


def view(pt_id, world, turn, *, extra_futures=None, limit=80):
    nodes = db.rows(
        "SELECT * FROM graph_nodes WHERE playthrough_id=? ORDER BY id DESC LIMIT ?",
        (pt_id, limit))[::-1]
    ids = {n["id"] for n in nodes}
    edges = [e for e in db.rows(
        "SELECT * FROM graph_edges WHERE playthrough_id=? ORDER BY id", (pt_id,))
        if e["src"] in ids and e["dst"] in ids]

    # Lay the trail out as a spine with side-lanes for anything that branched.
    spine = 0
    laid = []
    for n in nodes:
        lane = 0
        if n["kind"] in ("beat", "twist", "contest", "betrayal"):
            lane = 1
        elif n["kind"] in ("bond", "discovery"):
            lane = -1
        laid.append({
            "id": n["id"], "turn": n["turn"], "kind": n["kind"], "label": n["label"],
            "detail": n["detail"], "place_id": n["place_id"], "actor": n["actor"],
            "weight": n["weight"], "lane": lane, "seq": spine,
            "tone": KIND_TONE.get(n["kind"], "calm"),
        })
        spine += 1

    return {
        "nodes": laid,
        "edges": [{"src": e["src"], "dst": e["dst"], "kind": e["kind"]} for e in edges],
        "here": laid[-1]["id"] if laid else None,
        "futures": _futures(pt_id, world, turn, extra_futures),
        "turn": turn,
    }


def node(pt_id, node_id):
    return db.row("SELECT * FROM graph_nodes WHERE playthrough_id=? AND id=?", (pt_id, node_id))


def by_fact(pt_id, fact_key):
    if not fact_key:
        return None
    return db.row("SELECT * FROM graph_nodes WHERE playthrough_id=? AND fact_key=?"
                  " ORDER BY id DESC LIMIT 1", (pt_id, fact_key))


def tone_of(kind: str) -> str:
    return KIND_TONE.get(kind, "calm")
