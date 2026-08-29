"""Layer 2 - the Living Atlas. Deterministic, $0 LLM.

Places are nodes with a state the world writes to, not a list the player reads:

    hidden      never seen; drawn fogged, name withheld
    rumoured    heard of but not visited; drawn faint
    discovered  been there; drawn lit
    ruined      consequence ripple landed; stays ruined forever

A node's `condition` is the ripple: burn the village and it is burned for the
rest of the story, in the atlas, in the Narrator's context, and in what NPCs
who lived there will say to you.

Layout is computed once per world by a deterministic force-free placement, so
the map looks the same every session and the client can draw it with no layout
library.
"""
from __future__ import annotations

import math

from . import db, llm

STATUS_ORDER = {"hidden": 0, "rumoured": 1, "discovered": 2, "ruined": 3}
CONDITIONS = ("intact", "damaged", "ruined", "abandoned", "fortified", "flooded", "burned")

# A ripple is a condition change plus the story reason it happened.
RIPPLE_BLOCKS_TRAVEL = {"ruined", "flooded", "burned"}


def seed(pt_id, world, start_place):
    for loc in world.locations:
        db.run(
            "INSERT OR IGNORE INTO atlas_places (playthrough_id, place_id, status) VALUES (?,?,?)",
            (pt_id, loc["id"], "hidden"))
    discover(pt_id, start_place, 0, reason="You are standing in it.")
    # Anywhere you can walk to from the start is at least heard of.
    for neighbour in world.connects(start_place):
        rumour(pt_id, neighbour, 0)


def _row(pt_id, place_id):
    return db.row("SELECT * FROM atlas_places WHERE playthrough_id=? AND place_id=?",
                  (pt_id, place_id))


def discover(pt_id, place_id, turn, reason=""):
    row = _row(pt_id, place_id)
    if not row:
        db.run("INSERT OR IGNORE INTO atlas_places (playthrough_id, place_id) VALUES (?,?)",
               (pt_id, place_id))
        row = _row(pt_id, place_id)
    first = row["status"] not in ("discovered", "ruined")
    db.run(
        "UPDATE atlas_places SET status=CASE WHEN status='ruined' THEN 'ruined' ELSE 'discovered' END,"
        " discovered_turn=CASE WHEN discovered_turn < 0 THEN ? ELSE discovered_turn END,"
        " visits=visits+1, last_seen_turn=?, note=CASE WHEN ?='' THEN note ELSE ? END"
        " WHERE playthrough_id=? AND place_id=?",
        (turn, turn, reason, reason, pt_id, place_id))
    return first


def rumour(pt_id, place_id, turn, note=""):
    row = _row(pt_id, place_id)
    if not row:
        db.run("INSERT OR IGNORE INTO atlas_places (playthrough_id, place_id) VALUES (?,?)",
               (pt_id, place_id))
        row = _row(pt_id, place_id)
    if row["status"] != "hidden":
        return False
    db.run("UPDATE atlas_places SET status='rumoured', note=? WHERE playthrough_id=? AND place_id=?",
           (note or row["note"], pt_id, place_id))
    return True


def ripple(pt_id, place_id, condition, turn, text, magnitude=3):
    """A consequence lands on a place and stays landed."""
    if condition not in CONDITIONS:
        condition = "damaged"
    status = "ruined" if condition in RIPPLE_BLOCKS_TRAVEL else None
    if status:
        db.run("UPDATE atlas_places SET condition=?, status=? WHERE playthrough_id=? AND place_id=?",
               (condition, status, pt_id, place_id))
    else:
        db.run("UPDATE atlas_places SET condition=? WHERE playthrough_id=? AND place_id=?",
               (condition, pt_id, place_id))
    echo(pt_id, turn, text, place_id=place_id, kind="ripple", magnitude=magnitude)
    return condition


def echo(pt_id, turn, text, *, place_id="", kind="change", magnitude=2):
    """The world-memory ribbon: how this world is different from how it started."""
    return db.run(
        "INSERT INTO world_echoes (playthrough_id,turn,place_id,kind,text,magnitude,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (pt_id, turn, place_id, kind, text, int(magnitude), db.now()))


def echoes(pt_id, limit=60):
    return db.rows(
        "SELECT * FROM world_echoes WHERE playthrough_id=? ORDER BY id DESC LIMIT ?",
        (pt_id, limit))[::-1]


def states(pt_id):
    return {r["place_id"]: r for r in
            db.rows("SELECT * FROM atlas_places WHERE playthrough_id=?", (pt_id,))}


def blocked(pt_id, place_id) -> str:
    row = _row(pt_id, place_id)
    if row and row["condition"] in RIPPLE_BLOCKS_TRAVEL:
        return row["condition"]
    return ""


# ---------------------------------------------------------------------------
# Deterministic layout - same map every time, no layout library on the client
# ---------------------------------------------------------------------------

def layout(world) -> dict[str, tuple[float, float]]:
    """Places on a unit square. Seeded ring placement refined by a few rounds of
    spring relaxation over the real adjacency graph - enough to make connected
    places sit near each other without importing anything."""
    ids = [l["id"] for l in world.locations]
    n = len(ids)
    if n == 0:
        return {}
    rng = llm.rng(world.id, "layout")
    pos = {}
    for i, pid in enumerate(ids):
        angle = (i / n) * math.tau + rng.random() * 0.35
        radius = 0.34 + rng.random() * 0.12
        pos[pid] = [0.5 + radius * math.cos(angle), 0.5 + radius * math.sin(angle)]

    edges = [(a["id"], b) for a in world.locations for b in a.get("connects", [])
             if b in pos and a["id"] < b]
    for _ in range(90):
        force = {pid: [0.0, 0.0] for pid in pos}
        for a in ids:                                   # repel everything
            for b in ids:
                if a >= b:
                    continue
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d2 = max(0.0016, dx * dx + dy * dy)
                f = 0.0016 / d2
                force[a][0] += dx * f; force[a][1] += dy * f
                force[b][0] -= dx * f; force[b][1] -= dy * f
        for a, b in edges:                              # pull the connected
            dx = pos[b][0] - pos[a][0]
            dy = pos[b][1] - pos[a][1]
            f = 0.045
            force[a][0] += dx * f; force[a][1] += dy * f
            force[b][0] -= dx * f; force[b][1] -= dy * f
        for pid in ids:
            pos[pid][0] = min(0.94, max(0.06, pos[pid][0] + force[pid][0]))
            pos[pid][1] = min(0.94, max(0.06, pos[pid][1] + force[pid][1]))
    return {pid: (round(p[0], 4), round(p[1], 4)) for pid, p in pos.items()}


def view(pt_id, world, *, here=None, turn=0, travellers=None):
    """Everything the map panel needs. Hidden places are returned WITHOUT their
    name - the client cannot leak what the player has not found."""
    st = states(pt_id)
    lay = layout(world)
    nodes = []
    for loc in world.locations:
        row = st.get(loc["id"]) or {"status": "hidden", "condition": "intact",
                                    "discovered_turn": -1, "visits": 0, "note": ""}
        known = row["status"] != "hidden"
        x, y = lay.get(loc["id"], (0.5, 0.5))
        nodes.append({
            "id": loc["id"],
            "name": loc["name"] if known else "?",
            "kind": loc["kind"] if known else "unknown",
            "desc": loc["desc"] if row["status"] in ("discovered", "ruined") else "",
            "status": row["status"],
            "condition": row["condition"],
            "visits": row["visits"],
            "discovered_turn": row["discovered_turn"],
            "note": row["note"] if known else "",
            "here": loc["id"] == here,
            "x": x, "y": y,
        })
    known_ids = {n["id"] for n in nodes if n["status"] != "hidden"}
    edges = []
    seen = set()
    for loc in world.locations:
        for other in loc.get("connects", []):
            key = tuple(sorted((loc["id"], other)))
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "a": key[0], "b": key[1],
                "known": key[0] in known_ids and key[1] in known_ids,
            })
    return {
        "nodes": nodes, "edges": edges, "here": here,
        "discovered": sum(1 for n in nodes if n["status"] in ("discovered", "ruined")),
        "total": len(nodes),
        "travellers": travellers or [],
        "echoes": echoes(pt_id, 24),
    }
