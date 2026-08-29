"""Layer 5 - world awareness. Entirely deterministic, $0 LLM.

The rule the whole layer exists to enforce: **nobody knows what they did not
witness or were not told.** No global event bus, no omniscient guards.

  witness   an event is known only to entities present with line of sight,
            filtered by the light/noise of the actual world vector
  rumour    a witness who travels carries the fact, arriving after a real
            latency; the fact degrades in confidence and loses detail
  faction   reputation moves for the factions that KNOW, not globally
  hunt      a faction that knows enough issues an order; the hunter is a
            marker on the map with an arrival WINDOW, not a teleport
  stealth   detection is arithmetic over light, noise, cover and senses

The one model call in this layer is narrating a rumour when it lands, and it is
optional.
"""
from __future__ import annotations

import hashlib
import json

from . import atlas, db, llm, worldstate

# Travel cost between adjacent places, in turns. Weather taxes it.
BASE_LEG_TURNS = 2
WEATHER_TRAVEL_TAX = {"storm": 2, "snow": 2, "rain": 1, "fog": 1, "ashfall": 1}

SEVERITY_WORDS = {1: "trivial", 2: "minor", 3: "serious", 4: "grave", 5: "capital"}


def fact_key(kind: str, actor: str, turn: int, place: str) -> str:
    raw = f"{kind}|{actor}|{turn}|{place}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Knowledge store
# ---------------------------------------------------------------------------

def learn(pt_id, holder_kind, holder_id, *, key, summary, detail="", subject="",
          place_id="", turn=0, confidence=1.0, source="witnessed", severity=2):
    """Idempotent: hearing the same fact twice raises confidence, never duplicates."""
    existing = db.row(
        "SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind=? AND holder_id=? AND fact_key=?",
        (pt_id, holder_kind, holder_id, key))
    if existing:
        if confidence > existing["confidence"]:
            db.run("UPDATE knowledge SET confidence=?, detail=CASE WHEN ?='' THEN detail ELSE ? END,"
                   " source=? WHERE id=?",
                   (confidence, detail, detail, source, existing["id"]))
        return existing["id"]
    return db.run(
        "INSERT INTO knowledge (playthrough_id,holder_kind,holder_id,fact_key,summary,detail,subject,"
        "place_id,turn_learned,confidence,source,severity,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pt_id, holder_kind, holder_id, key, summary[:240], detail[:400], subject, place_id,
         turn, float(confidence), source, int(severity), db.now()))


def knows(pt_id, holder_kind, holder_id, key) -> bool:
    return bool(db.row(
        "SELECT 1 FROM knowledge WHERE playthrough_id=? AND holder_kind=? AND holder_id=? AND fact_key=?",
        (pt_id, holder_kind, holder_id, key)))


def known_by(pt_id, holder_kind, holder_id, limit=80):
    return db.rows(
        "SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind=? AND holder_id=?"
        " ORDER BY id DESC LIMIT ?", (pt_id, holder_kind, holder_id, limit))


def who_knows(pt_id, key):
    return db.rows(
        "SELECT holder_kind, holder_id, confidence, source, turn_learned FROM knowledge"
        " WHERE playthrough_id=? AND fact_key=?", (pt_id, key))


# ---------------------------------------------------------------------------
# Witnessing
# ---------------------------------------------------------------------------

def perception(local_world, *, cover=0, distance=0) -> float:
    """How well a normal person can perceive here, 0..1. Light helps sight,
    noise hurts hearing, cover and distance hurt both."""
    light = local_world["light"] / 5.0
    quiet = 1.0 - local_world["noise"] / 6.0
    base = 0.55 * light + 0.45 * quiet
    base -= 0.18 * cover + 0.12 * distance
    return round(max(0.05, min(1.0, base)), 3)


def witness(pt_id, world, *, actor, kind, summary, detail="", place_id, turn,
            severity=3, present, stealth=0.0, subject=""):
    """Record an event and give it ONLY to those who could actually perceive it.
    Returns the fact key and the list of witnesses."""
    key = fact_key(kind, actor, turn, place_id)
    lw = worldstate.local(pt_id, world, place_id)
    clarity = perception(lw)
    seen_by = []
    for npc_id in present:
        # A quiet act in a dark room is missed; a killing in daylight is not.
        threshold = stealth - (severity - 2) * 0.12
        roll = llm.rng(pt_id, turn, npc_id, key).random()
        if roll < max(0.0, threshold) * (1.2 - clarity):
            continue
        seen_by.append(npc_id)
        learn(pt_id, "npc", npc_id, key=key, summary=summary, detail=detail,
              subject=subject or actor, place_id=place_id, turn=turn,
              confidence=round(clarity, 2), source="witnessed", severity=severity)
    # The actor always knows what they did.
    learn(pt_id, "player", actor, key=key, summary=summary, detail=detail,
          subject=subject or actor, place_id=place_id, turn=turn,
          confidence=1.0, source="did-it", severity=severity)
    return {"key": key, "witnesses": seen_by, "clarity": clarity, "summary": summary,
            "detail": detail, "actor": actor, "kind": kind,
            "unseen": not seen_by, "severity": severity, "place_id": place_id}


# ---------------------------------------------------------------------------
# Rumour relay
# ---------------------------------------------------------------------------

def travel_turns(pt_id, world, a, b) -> int:
    """Shortest path in turns, taxed by weather. Deterministic BFS."""
    if a == b:
        return 0
    seen = {a}
    frontier = [a]
    hops = 0
    while frontier and hops < 12:
        hops += 1
        nxt = []
        for node in frontier:
            for other in world.connects(node):
                if other == b:
                    st = worldstate.get(pt_id)
                    tax = WEATHER_TRAVEL_TAX.get(st["weather"], 0)
                    return hops * BASE_LEG_TURNS + tax
                if other not in seen:
                    seen.add(other)
                    nxt.append(other)
        frontier = nxt
    return 99


def relay(pt_id, world, *, key, carrier, from_place, to_place, turn, distortion=0.15):
    """A witness sets out carrying what they saw. It arrives when they arrive."""
    legs = travel_turns(pt_id, world, from_place, to_place)
    if legs >= 99:
        return None
    arrive = turn + max(1, legs)
    return db.run(
        "INSERT INTO rumors (playthrough_id,fact_key,carrier,from_place,to_place,depart_turn,"
        "arrive_turn,distortion,delivered) VALUES (?,?,?,?,?,?,?,?,0)",
        (pt_id, key, carrier, from_place, to_place, turn, arrive, float(distortion)))


def spread(pt_id, world, fact, *, turn, witnesses, severity=3):
    """Witnesses who care enough carry it to the places they are connected to.
    Which witnesses care is arithmetic, not a model call."""
    sent = []
    for npc_id in witnesses:
        npc = world.by_id.get(npc_id)
        if not npc:
            continue
        # Somebody who lives here tells the place they work; a gossip tells everyone.
        targets = {npc["schedule"][p] for p in ("morning", "midday", "evening", "night")}
        targets.discard(fact["place_id"])
        talkative = 0.35 + 0.15 * severity
        for target in sorted(targets):
            if llm.rng(pt_id, turn, npc_id, target, "relay").random() > talkative:
                continue
            rid = relay(pt_id, world, key=fact["key"], carrier=npc_id,
                        from_place=fact["place_id"], to_place=target, turn=turn)
            if rid:
                sent.append({"carrier": npc_id, "to": target, "id": rid})
    return sent


def arrivals(pt_id, world, turn):
    """Rumours that land this turn. Everyone whose routine touches the
    destination learns it, at reduced confidence and without the detail."""
    due = db.rows(
        "SELECT * FROM rumors WHERE playthrough_id=? AND delivered=0 AND arrive_turn<=?",
        (pt_id, turn))
    landed = []
    for r in due:
        origin = db.row(
            "SELECT * FROM knowledge WHERE playthrough_id=? AND fact_key=? ORDER BY confidence DESC LIMIT 1",
            (pt_id, r["fact_key"]))
        db.run("UPDATE rumors SET delivered=1 WHERE id=?", (r["id"],))
        if not origin:
            continue
        conf = max(0.15, origin["confidence"] * (1.0 - r["distortion"]))
        told = []
        for npc in world.npcs:
            if r["to_place"] not in npc["schedule"].values():
                continue
            if npc["id"] == r["carrier"]:
                continue
            learn(pt_id, "npc", npc["id"], key=r["fact_key"], summary=origin["summary"],
                  detail="", subject=origin["subject"], place_id=origin["place_id"],
                  turn=turn, confidence=round(conf, 2), source="heard", severity=origin["severity"])
            told.append(npc["id"])
        if told:
            landed.append({"key": r["fact_key"], "summary": origin["summary"],
                           "place": r["to_place"], "carrier": r["carrier"],
                           "told": told, "confidence": round(conf, 2),
                           "severity": origin["severity"],
                           "lag": turn - r["depart_turn"]})
    return landed


# ---------------------------------------------------------------------------
# Factions and reputation - moves only for factions that KNOW
# ---------------------------------------------------------------------------

def factions(world):
    declared = world.get("factions") or []
    if declared:
        return declared
    # A world with no declared factions still has one: the settlement itself.
    return [{"id": "locals", "name": f"The people of {world.name}",
             "seat": world.get("start_location"), "members": [n["id"] for n in world.npcs],
             "law": 2}]


def faction_of(world, npc_id):
    for f in factions(world):
        if npc_id in (f.get("members") or []):
            return f
    return None


def rep(pt_id, faction_id, player):
    row = db.row(
        "SELECT * FROM faction_rep WHERE playthrough_id=? AND faction_id=? AND player_id=?",
        (pt_id, faction_id, player))
    if row:
        return row
    db.run("INSERT OR IGNORE INTO faction_rep (playthrough_id,faction_id,player_id) VALUES (?,?,?)",
           (pt_id, faction_id, player))
    return db.row(
        "SELECT * FROM faction_rep WHERE playthrough_id=? AND faction_id=? AND player_id=?",
        (pt_id, faction_id, player))


def adjust_rep(pt_id, world, *, player, fact, turn, valence=-1):
    """Indirect reciprocity: a faction's standing toward you moves because its
    members know, weighted by how sure they are. A faction that heard nothing
    does not move."""
    moved = []
    for f in factions(world):
        members = set(f.get("members") or [])
        knowers = [k for k in who_knows(pt_id, fact["key"])
                   if k["holder_kind"] == "npc" and k["holder_id"] in members]
        if not knowers:
            continue
        confidence = max(k["confidence"] for k in knowers)
        reach = len(knowers) / max(1, len(members))
        magnitude = fact["severity"] * 6 * confidence * (0.4 + 0.6 * reach)
        row = rep(pt_id, f["id"], player)
        standing = max(-100.0, min(100.0, row["standing"] + valence * magnitude))
        fear = max(0.0, min(100.0, row["fear"] + (magnitude * 0.6 if valence < 0 else -magnitude * 0.2)))
        db.run("UPDATE faction_rep SET standing=?, fear=?, known_events=known_events+1, last_turn=?"
               " WHERE playthrough_id=? AND faction_id=? AND player_id=?",
               (standing, fear, turn, pt_id, f["id"], player))
        moved.append({"faction": f["id"], "name": f["name"], "standing": round(standing, 1),
                      "fear": round(fear, 1), "knowers": len(knowers),
                      "known_events": row["known_events"] + 1,
                      "confidence": round(confidence, 2), "delta": round(valence * magnitude, 1)})
    return moved


# ---------------------------------------------------------------------------
# Hunts - a marker with an arrival window, never a teleport
# ---------------------------------------------------------------------------

HUNT_THRESHOLD = -45.0
PATTERN_EVENTS = 4          # a repeated pattern is its own trigger
PATTERN_THRESHOLD = -25.0


def _hunt_warranted(entry, fact) -> bool:
    """Either one grave act against a faction that already hates you, or a
    pattern. Nine assaults with no response is not a simulation, it is a bug."""
    if fact["severity"] < 3:
        return False
    if entry["standing"] <= HUNT_THRESHOLD:
        return True
    row_events = entry.get("known_events", 0)
    return row_events >= PATTERN_EVENTS and entry["standing"] <= PATTERN_THRESHOLD


def maybe_order_hunt(pt_id, world, *, player, fact, turn, reps):
    """A faction that knows something grave enough, and already dislikes you,
    sends someone. The window is honest: travel time plus weather, plus slack."""
    orders = []
    for entry in reps:
        if not _hunt_warranted(entry, fact):
            continue
        f = next((x for x in factions(world) if x["id"] == entry["faction"]), None)
        if not f:
            continue
        if db.row("SELECT 1 FROM hunts WHERE playthrough_id=? AND faction_id=? AND target_id=?"
                  " AND status='enroute'", (pt_id, f["id"], player)):
            continue
        seat = f.get("seat") or world.get("start_location")
        target_place = fact["place_id"]
        legs = travel_turns(pt_id, world, seat, target_place)
        if legs >= 99:
            continue
        slack = max(1, legs // 2)
        hunter = next((m for m in (f.get("members") or []) if m in world.by_id), "a party")
        hid = db.run(
            "INSERT INTO hunts (playthrough_id,faction_id,hunter,target_kind,target_id,reason,"
            "ordered_turn,from_place,to_place,eta_min,eta_max,status,severity)"
            " VALUES (?,?,?,'player',?,?,?,?,?,?,?,'enroute',?)",
            (pt_id, f["id"], hunter, player, fact["summary"][:180], turn, seat, target_place,
             turn + legs, turn + legs + slack, fact["severity"]))
        orders.append({"id": hid, "faction": f["id"], "faction_name": f["name"],
                       "hunter": hunter, "from": seat, "to": target_place,
                       "eta_min": turn + legs, "eta_max": turn + legs + slack})
    return orders


def hunt_tick(pt_id, world, turn, *, player_places):
    """Advance every hunt. Arriving is a range, not a tick - the hunter may show
    up early or late inside their window, decided once and deterministically."""
    events = []
    for h in db.rows("SELECT * FROM hunts WHERE playthrough_id=? AND status='enroute'", (pt_id,)):
        rng = llm.rng(pt_id, h["id"], "arrive")
        actual = h["eta_min"] + int(rng.random() * max(1, h["eta_max"] - h["eta_min"] + 1))
        if turn < actual:
            continue
        where = player_places.get(h["target_id"])
        if where == h["to_place"]:
            db.run("UPDATE hunts SET status='arrived' WHERE id=?", (h["id"],))
            events.append({"kind": "arrived", **dict(h), "at": h["to_place"]})
        else:
            # They arrive, find nothing, and re-task toward where you were last seen.
            new_target = where or h["to_place"]
            legs = travel_turns(pt_id, world, h["to_place"], new_target)
            if new_target == h["to_place"] or legs >= 99:
                db.run("UPDATE hunts SET status='cold' WHERE id=?", (h["id"],))
                events.append({"kind": "cold", **dict(h)})
            else:
                db.run("UPDATE hunts SET from_place=?, to_place=?, eta_min=?, eta_max=? WHERE id=?",
                       (h["to_place"], new_target, turn + legs, turn + legs + max(1, legs // 2), h["id"]))
                events.append({"kind": "retasked", **dict(h), "now_to": new_target})
    return events


def hunt_panel(pt_id, world, player, turn):
    """What the player may see: only hunts they KNOW about, with the window."""
    out = []
    for h in db.rows("SELECT * FROM hunts WHERE playthrough_id=? AND target_id=?", (pt_id, player)):
        key = f"hunt:{h['id']}"
        aware = knows(pt_id, "player", player, key)
        if not aware:
            continue
        f = next((x for x in factions(world) if x["id"] == h["faction_id"]), {})
        out.append({
            "id": h["id"], "faction": f.get("name", h["faction_id"]),
            "hunter": world.npc_name(h["hunter"]) if h["hunter"] in world.by_id else h["hunter"],
            "reason": h["reason"], "status": h["status"],
            "from": world.loc_name(h["from_place"]), "to": world.loc_name(h["to_place"]),
            "eta_min": h["eta_min"], "eta_max": h["eta_max"],
            "turns_out": max(0, h["eta_min"] - turn),
            "window": (f"{max(0, h['eta_min'] - turn)}-{max(0, h['eta_max'] - turn)} turns"
                       if h["status"] == "enroute" else h["status"]),
            "severity": SEVERITY_WORDS.get(h["severity"], "serious"),
        })
    return out


def reveal_hunt_to_player(pt_id, hunt_id, player, turn, summary):
    """The player learns they are hunted the same way they learn anything else -
    somebody told them."""
    learn(pt_id, "player", player, key=f"hunt:{hunt_id}", summary=summary,
          turn=turn, confidence=0.7, source="heard", severity=4)


# ---------------------------------------------------------------------------
# Stealth - pure arithmetic over the environment
# ---------------------------------------------------------------------------

COVER = {"tavern": 0.35, "work": 0.3, "sacred": 0.2, "civic": 0.15,
         "open": 0.0, "threshold": 0.1, "place": 0.15}


def stealth_check(pt_id, world, *, place_id, turn, actor, watchers, intent_noise=1,
                  disguised=False):
    """Could this actor be present and unsensed? Light, noise, cover, and how
    loud the thing they are doing is. No dice against a wall - the environment
    decides, and the same conditions always give the same answer."""
    lw = worldstate.local(pt_id, world, place_id)
    kind = (world.loc_by_id.get(place_id) or {}).get("kind", "place")
    cover = COVER.get(kind, 0.15)

    concealment = (
        (1.0 - lw["light"] / 5.0) * 0.45      # dark hides
        + (lw["noise"] / 5.0) * 0.25          # loud rooms mask
        + cover * 0.8
        + (0.15 if disguised else 0.0)
    )
    exposure = intent_noise * 0.18 + len(watchers) * 0.07
    margin = round(concealment - exposure, 3)

    spotted_by = []
    for w in watchers:
        keen = llm.rng(pt_id, turn, w, place_id, "sense").random() * 0.3
        if margin - keen < 0:
            spotted_by.append(w)
    return {
        "hidden": not spotted_by,
        "margin": margin,
        "concealment": round(concealment, 3),
        "exposure": round(exposure, 3),
        "spotted_by": spotted_by,
        "light": lw["light"], "noise": lw["noise"], "cover": round(cover, 2),
        "reason": ("dark and covered" if margin > 0.25 else
                   "just barely out of sight" if margin > 0 else
                   "too lit and too quiet to move unseen"),
    }


def public_state(pt_id, world, player, turn):
    """The knowledge panel: only what this player actually knows."""
    facts = known_by(pt_id, "player", player, 60)
    reps = []
    for f in factions(world):
        r = rep(pt_id, f["id"], player)
        if r["known_events"] == 0 and abs(r["standing"]) < 1:
            continue
        reps.append({"id": f["id"], "name": f["name"], "standing": round(r["standing"], 1),
                     "fear": round(r["fear"], 1), "events": r["known_events"]})
    return {
        "facts": [{"key": f["fact_key"], "summary": f["summary"], "detail": f["detail"],
                   "turn": f["turn_learned"], "confidence": f["confidence"],
                   "source": f["source"], "place": world.loc_name(f["place_id"]) if f["place_id"] else "",
                   "severity": SEVERITY_WORDS.get(f["severity"], "minor")} for f in facts],
        "factions": reps,
        "hunts": hunt_panel(pt_id, world, player, turn),
    }
