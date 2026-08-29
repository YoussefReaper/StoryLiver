"""Layer 6 - combat. All math deterministic, $0 LLM; one capped call narrates.

WEGO: everyone declares into a pre-commit round, nothing resolves until the
last order lands, then the whole round happens at once. Nobody peeks at turn
order because there is no turn order.

Positioning is zone-based rather than gridded - three zones per side plus a
contested centre. That is enough for flanking to be real arithmetic without
asking a text player to think in squares.

Flanking cuts BOTH ways, which is the design point: four players who spread out
to flank five enemies are themselves flanked by the extra body. Advantage is
computed per combatant from who is adjacent to them, so the 4v5 case falls out
of the same formula as the 1v1 case.

Bosses are not stat walls. A boss carries `phases` and `weak_points`, and a
`only_hurt_by` condition the party has to create - so the answer to a boss is a
plan, not more hitting.
"""
from __future__ import annotations

import json
import uuid

from . import db, llm, precommit, worldstate

ZONES = ("back_a", "front_a", "centre", "front_b", "back_b")
ADJACENT = {
    "back_a": ("front_a",),
    "front_a": ("back_a", "centre"),
    "centre": ("front_a", "front_b"),
    "front_b": ("centre", "back_b"),
    "back_b": ("front_b",),
}
SIDE_ZONES = {"a": ("back_a", "front_a"), "b": ("front_b", "back_b")}

MOVES = {
    "strike":   dict(power=1.0, noise=3, mana=0, desc="A committed attack."),
    "guard":    dict(power=0.0, noise=1, mana=0, desc="Give ground, raise your guard."),
    "press":    dict(power=1.35, noise=4, mana=1, desc="All-in. Hits harder, leaves you open."),
    "feint":    dict(power=0.6, noise=2, mana=1, desc="Sets up an ally's next blow."),
    "shove":    dict(power=0.4, noise=3, mana=0, desc="Move a body out of a zone."),
    "reposition": dict(power=0.0, noise=2, mana=0, desc="Change zone. Breaks a flank."),
    "aid":      dict(power=0.0, noise=1, mana=0, desc="Steady an ally: they shrug off one hit."),
    "hide":     dict(power=0.0, noise=0, mana=0, desc="Break line of sight if the room allows."),
    "exploit":  dict(power=1.6, noise=3, mana=2, desc="Strike a weak point, if one is open."),
}


def _cid():
    return "c" + uuid.uuid4().hex[:10]


def start(pt_id, world, *, place_id, sides, session_id="", boss=None, surprise=""):
    """sides: {"a": [entity...], "b": [entity...]}; entity = dict(id,name,kind,hp,power,guard,zone)."""
    combat_id = _cid()
    db.run(
        "INSERT INTO combats (id,playthrough_id,session_id,place_id,round,status,surprise,boss,log,"
        "created_at,updated_at) VALUES (?,?,?,?,0,'declare',?,?,'[]',?,?)",
        (combat_id, pt_id, session_id, place_id, surprise, json.dumps(boss or {}),
         db.now(), db.now()))
    for side, members in sides.items():
        default_zone = SIDE_ZONES[side][1] if side in SIDE_ZONES else "centre"
        for e in members:
            hp = int(e.get("hp", 20))
            db.run(
                "INSERT OR REPLACE INTO combatants (combat_id,entity_id,kind,side,name,hp,max_hp,"
                "guard,power,zone,status,hidden,down) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (combat_id, e["id"], e.get("kind", "npc"), side, e.get("name", e["id"]),
                 hp, hp, int(e.get("guard", 10)), int(e.get("power", 4)),
                 e.get("zone", default_zone), json.dumps(e.get("status", [])),
                 1 if e.get("hidden") else 0))
    return get(combat_id)


def get(combat_id):
    c = db.row("SELECT * FROM combats WHERE id=?", (combat_id,))
    if not c:
        return None
    out = dict(c)
    out["boss"] = db.jload(c["boss"], {}) or {}
    out["log"] = db.jload(c["log"], []) or []
    out["combatants"] = [
        {**dict(r), "status": db.jload(r["status"], []) or [], "hidden": bool(r["hidden"]),
         "down": bool(r["down"])}
        for r in db.rows("SELECT * FROM combatants WHERE combat_id=? ORDER BY side, entity_id",
                         (combat_id,))]
    return out


def active_for(pt_id):
    row = db.row("SELECT id FROM combats WHERE playthrough_id=? AND status!='over' ORDER BY rowid DESC LIMIT 1",
                 (pt_id,))
    return get(row["id"]) if row else None


# ---------------------------------------------------------------------------
# The math
# ---------------------------------------------------------------------------

def _engaged(c, e):
    """Who is close enough to threaten this combatant."""
    zones = {e["zone"], *ADJACENT.get(e["zone"], ())}
    return [o for o in c["combatants"]
            if not o["down"] and o["side"] != e["side"] and o["zone"] in zones and not o["hidden"]]


def _allies_engaged(c, e):
    zones = {e["zone"], *ADJACENT.get(e["zone"], ())}
    return [o for o in c["combatants"]
            if not o["down"] and o["side"] == e["side"] and o["entity_id"] != e["entity_id"]
            and o["zone"] in zones]


def advantage(c, e):
    """Positive = you are flanking. Negative = you are flanked. This is the same
    formula for everyone, which is why a 4v5 puts one of the four in trouble."""
    threats = len(_engaged(c, e))
    support = len(_allies_engaged(c, e))
    net = support - threats + 1
    return max(-3, min(3, net))


def _boss_gate(c, attacker, target, move):
    """Bosses are answered with a plan. If the boss declares `only_hurt_by`,
    ordinary damage does nothing until the party creates that condition."""
    boss = c["boss"]
    if not boss or target["entity_id"] != boss.get("entity_id"):
        return 1.0, ""
    phase = current_phase(c)
    need = (phase or {}).get("only_hurt_by") or boss.get("only_hurt_by")
    if need and need not in target["status"]:
        return 0.0, f"{target['name']} is untouchable until {need.replace('_', ' ')}"
    weak = (phase or {}).get("weak_point") or boss.get("weak_point")
    if weak and move == "exploit":
        return 2.0, f"the {weak.replace('_', ' ')} is open"
    return 1.0, ""


def current_phase(c):
    boss = c["boss"]
    if not boss:
        return None
    entity = next((x for x in c["combatants"] if x["entity_id"] == boss.get("entity_id")), None)
    if not entity:
        return None
    pct = entity["hp"] / max(1, entity["max_hp"])
    for phase in boss.get("phases", []):
        if pct > phase.get("above", 0):
            return phase
    return (boss.get("phases") or [None])[-1]


def declare_key(combat_id, rnd):
    return precommit.round_key("combat", rnd, combat_id)


def declare(pt_id, combat_id, player_id, move, target=None, zone=None, session_id=""):
    c = get(combat_id)
    if not c or c["status"] == "over":
        return {"ok": False, "reason": "this fight is over"}
    if move not in MOVES:
        return {"ok": False, "reason": f"unknown move {move}"}
    return precommit.declare(
        pt_id, round_key=declare_key(combat_id, c["round"]), turn=c["round"],
        player_id=player_id, intent=move, kind="combat", visibility="hidden",
        payload={"move": move, "target": target, "zone": zone}, session_id=session_id)


def _npc_orders(c, pt_id):
    """Enemy orders are chosen deterministically from position, not by a model:
    press when flanking, reposition when flanked, guard when badly hurt."""
    orders = []
    for e in c["combatants"]:
        if e["down"] or e["kind"] == "player":
            continue
        adv = advantage(c, e)
        hurt = e["hp"] / max(1, e["max_hp"])
        foes = _engaged(c, e)
        if hurt < 0.3 and adv < 0:
            move, zone = "guard", None
        elif adv <= -2:
            move, zone = "reposition", _retreat_zone(e)
        elif adv >= 1 and foes:
            move, zone = "press", None
        elif foes:
            move, zone = "strike", None
        else:
            move, zone = "reposition", _advance_zone(e)
        target = min(foes, key=lambda o: o["hp"])["entity_id"] if foes else None
        orders.append({"player_id": e["entity_id"], "payload":
                       {"move": move, "target": target, "zone": zone}})
    return orders


def _retreat_zone(e):
    return SIDE_ZONES[e["side"]][0] if e["side"] in SIDE_ZONES else e["zone"]


def _advance_zone(e):
    return "centre"


def resolve(pt_id, combat_id, *, world=None):
    """The whole round happens at once. Movement first (so a break-flank works),
    then damage, then deaths. Pure arithmetic; returns a structured log the UI
    animates and the narrator can be handed."""
    c = get(combat_id)
    if not c:
        return None
    key = declare_key(combat_id, c["round"])
    player_orders = precommit.reveal(pt_id, key)
    orders = [{"player_id": o["player_id"], "payload": o["payload"]} for o in player_orders]
    orders += _npc_orders(c, pt_id)

    by_id = {e["entity_id"]: e for e in c["combatants"]}
    steps = []

    # 1. movement resolves first, simultaneously
    for o in orders:
        e = by_id.get(o["player_id"])
        payload = o["payload"] or {}
        if not e or e["down"]:
            continue
        if payload.get("move") in ("reposition", "shove") and payload.get("zone") in ZONES:
            frm = e["zone"]
            e["zone"] = payload["zone"]
            db.run("UPDATE combatants SET zone=? WHERE combat_id=? AND entity_id=?",
                   (e["zone"], combat_id, e["entity_id"]))
            steps.append({"kind": "move", "who": e["name"], "from": frm, "to": e["zone"]})

    c["combatants"] = list(by_id.values())

    # 2. support effects
    aided, feinted = set(), set()
    for o in orders:
        e = by_id.get(o["player_id"])
        payload = o["payload"] or {}
        if not e or e["down"]:
            continue
        if payload.get("move") == "aid" and payload.get("target") in by_id:
            aided.add(payload["target"])
            steps.append({"kind": "aid", "who": e["name"], "target": by_id[payload["target"]]["name"]})
        if payload.get("move") == "feint" and payload.get("target") in by_id:
            feinted.add(payload["target"])
            steps.append({"kind": "feint", "who": e["name"], "target": by_id[payload["target"]]["name"]})

    # 3. damage, all at once
    for o in orders:
        e = by_id.get(o["player_id"])
        payload = o["payload"] or {}
        move = payload.get("move")
        if not e or e["down"] or move not in MOVES or MOVES[move]["power"] <= 0:
            continue
        target = by_id.get(payload.get("target") or "")
        if not target:
            foes = _engaged(c, e)
            target = min(foes, key=lambda o2: o2["hp"]) if foes else None
        if not target or target["down"]:
            continue

        adv = advantage(c, e)
        gate, gate_reason = _boss_gate(c, e, target, move)
        rng = llm.rng(pt_id, combat_id, c["round"], e["entity_id"]).random()

        from_hiding = bool(e["hidden"])
        raw = e["power"] * MOVES[move]["power"]
        raw *= 1.0 + 0.18 * adv                         # flanking, both ways
        raw *= 1.25 if target["entity_id"] in feinted else 1.0
        raw *= 0.85 + 0.3 * rng
        if from_hiding:
            raw *= 1.8                                   # striking from unseen
        defence = target["guard"] * (1.3 if _order_of(orders, target["entity_id"]) == "guard" else 1.0)
        dmg = max(0, round(raw * gate - defence * 0.35))
        if target["entity_id"] in aided:
            dmg = max(0, dmg - 3)

        if e["hidden"]:
            e["hidden"] = False
            db.run("UPDATE combatants SET hidden=0 WHERE combat_id=? AND entity_id=?",
                   (combat_id, e["entity_id"]))

        target["hp"] = max(0, target["hp"] - dmg)
        db.run("UPDATE combatants SET hp=? WHERE combat_id=? AND entity_id=?",
               (target["hp"], combat_id, target["entity_id"]))
        steps.append({
            "kind": "hit" if dmg else "blocked", "who": e["name"], "target": target["name"],
            "move": move, "damage": dmg, "advantage": adv,
            "flanked": adv < 0, "flanking": adv > 0,
            "note": gate_reason or ("from hiding" if from_hiding else ""),
        })
        if target["hp"] == 0 and not target["down"]:
            target["down"] = True
            db.run("UPDATE combatants SET down=1 WHERE combat_id=? AND entity_id=?",
                   (combat_id, target["entity_id"]))
            steps.append({"kind": "down", "who": target["name"]})

    precommit.clear(pt_id, key)
    rnd = c["round"] + 1
    alive = {"a": [x for x in by_id.values() if x["side"] == "a" and not x["down"]],
             "b": [x for x in by_id.values() if x["side"] == "b" and not x["down"]]}
    status = "declare"
    outcome = None
    if not alive["a"] or not alive["b"]:
        status = "over"
        outcome = "a" if alive["a"] else "b"

    log = c["log"] + [{"round": c["round"], "steps": steps}]
    db.run("UPDATE combats SET round=?, status=?, log=?, updated_at=? WHERE id=?",
           (rnd, status, json.dumps(log[-24:]), db.now(), combat_id))

    return {"combat_id": combat_id, "round": c["round"], "steps": steps,
            "status": status, "winner": outcome, "state": get(combat_id),
            "phase": current_phase(get(combat_id))}


def _order_of(orders, entity_id):
    for o in orders:
        if o["player_id"] == entity_id:
            return (o["payload"] or {}).get("move")
    return None


def apply_condition(pt_id, combat_id, entity_id, condition):
    """How a party creates the boss's `only_hurt_by` state - deterministic,
    triggered by whatever the world says creates it."""
    row = db.row("SELECT status FROM combatants WHERE combat_id=? AND entity_id=?",
                 (combat_id, entity_id))
    if not row:
        return False
    status = db.jload(row["status"], []) or []
    if condition not in status:
        status.append(condition)
    db.run("UPDATE combatants SET status=? WHERE combat_id=? AND entity_id=?",
           (json.dumps(status), combat_id, entity_id))
    return True


def view(pt_id, combat_id, viewer):
    """Board state for one player. Hidden combatants on the other side are not
    sent to the client at all."""
    c = get(combat_id)
    if not c:
        return None
    me = next((x for x in c["combatants"] if x["entity_id"] == viewer), None)
    my_side = me["side"] if me else "a"
    board = []
    for e in c["combatants"]:
        if e["hidden"] and e["side"] != my_side:
            continue
        board.append({
            "id": e["entity_id"], "name": e["name"], "side": e["side"], "kind": e["kind"],
            "hp": e["hp"], "max_hp": e["max_hp"], "zone": e["zone"], "down": e["down"],
            "hidden": e["hidden"], "status": e["status"],
            "advantage": advantage(c, e), "mine": e["entity_id"] == viewer,
            "ally": e["side"] == my_side,
        })
    key = declare_key(combat_id, c["round"])
    declared = {r["player_id"] for r in precommit.submitted(pt_id, key)}
    return {
        "id": combat_id, "round": c["round"], "status": c["status"], "place": c["place_id"],
        "zones": list(ZONES), "board": board, "log": c["log"][-6:],
        "moves": {k: {**v} for k, v in MOVES.items()},
        "boss": {**c["boss"], "phase": current_phase(c)} if c["boss"] else None,
        "declared": sorted(declared), "you_declared": viewer in declared,
        "surprise": c["surprise"],
    }


def build_boss(world, spec):
    """World Forge boss editor output -> a runnable boss."""
    return {
        "entity_id": spec.get("entity_id") or "boss",
        "name": spec.get("name") or "The thing in the dark",
        "only_hurt_by": spec.get("only_hurt_by") or "",
        "weak_point": spec.get("weak_point") or "",
        "phases": spec.get("phases") or [
            {"above": 0.66, "name": "Testing you", "weak_point": ""},
            {"above": 0.33, "name": "In earnest", "weak_point": "off_hand"},
            {"above": 0.0, "name": "Cornered", "only_hurt_by": "grounded", "weak_point": "throat"},
        ],
    }
