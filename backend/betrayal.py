"""Layer 3 - the betrayal / hidden-information engine. Deterministic, $0 LLM.

Three mechanics, all borrowed from tables that already prove they work:

  Diplomacy   a departure is N private turns and then a SIMULTANEOUS reveal.
              Nobody sees what you did while you were away until everyone does.
  D&D         Insight versus Deception. Whether a lie holds is arithmetic over
              the relationship you have with the person you are lying to, not a
              model's opinion.
  Among Us    an optional hidden role. Knowing a traitor MIGHT exist is what
              makes an honest party interesting; certainty kills it.

Private knowledge reuses the per-player knowledge store from Layer 5, so a
secret you learn while split off is genuinely yours: it is not in the shared
feed, and the other players' clients never receive it.
"""
from __future__ import annotations

import json

from . import awareness, db, llm, precommit, relationships

DEFAULT_PRIVATE_TURNS = 3


# ---------------------------------------------------------------------------
# Splitting the party
# ---------------------------------------------------------------------------

def depart(pt_id, *, player_id, announced, truth, destination, turn,
           private_turns=DEFAULT_PRIVATE_TURNS, session_id=""):
    """Leave the group. What you TELL them and what you are DOING may differ;
    the gap between the two is the deception the reveal will expose."""
    open_row = active(pt_id, player_id)
    if open_row:
        return {"ok": False, "reason": "you are already away"}
    deception = 1 if (truth or "").strip().lower() != (announced or "").strip().lower() else 0
    sid = db.run(
        "INSERT INTO splits (playthrough_id,session_id,player_id,announced,truth,destination,"
        "private_turns,turns_taken,started_turn,status,deception,created_at)"
        " VALUES (?,?,?,?,?,?,?,0,?,'away',?,?)",
        (pt_id, session_id, player_id, announced[:400], (truth or announced)[:400],
         destination, int(private_turns), turn, deception, db.now()))
    return {"ok": True, "id": sid, "private_turns": private_turns, "deception": bool(deception)}


def active(pt_id, player_id=None):
    if player_id:
        return db.row(
            "SELECT * FROM splits WHERE playthrough_id=? AND player_id=? AND status='away'",
            (pt_id, player_id))
    return db.rows("SELECT * FROM splits WHERE playthrough_id=? AND status='away'", (pt_id,))


def note_private_turn(pt_id, player_id):
    row = active(pt_id, player_id)
    if not row:
        return None
    taken = row["turns_taken"] + 1
    db.run("UPDATE splits SET turns_taken=? WHERE id=?", (taken, row["id"]))
    return {"taken": taken, "of": row["private_turns"],
            "due": taken >= row["private_turns"]}


def record_private(pt_id, *, player_id, turn, summary, detail="", place_id=""):
    """Something learned or done off-screen. Held for the reveal, and readable
    only by this player in the meantime."""
    key = awareness.fact_key("private", player_id, turn, place_id or "away")
    awareness.learn(pt_id, "player", player_id, key=key, summary=summary, detail=detail,
                    subject=player_id, place_id=place_id, turn=turn,
                    confidence=1.0, source="private", severity=2)
    return key


def private_log(pt_id, player_id, since_turn):
    return [k for k in awareness.known_by(pt_id, "player", player_id, 120)
            if k["source"] == "private" and k["turn_learned"] >= since_turn]


# ---------------------------------------------------------------------------
# Insight vs deception - who believes the story you tell
# ---------------------------------------------------------------------------

def insight(pt_id, world, *, liar, target_kind, target_id, turn, deception_quality=0,
            player=None):
    """Does the target see through it? Deterministic over the relationship.

    Someone who trusts you believes you (that is what trust IS, and it is why
    betraying it costs so much). Someone burned before is very hard to fool."""
    if target_kind == "npc":
        rel = db.row("SELECT * FROM relationships WHERE playthrough_id=? AND src=? AND dst=?",
                     (pt_id, liar, target_id))
        trust = rel["trust"] if rel else 0.0
        betrayals = rel["betrayals"] if rel else 0
        respect = rel["respect"] if rel else 0.0
    else:
        trust, betrayals, respect = 0.0, 0, 0.0

    # Believing is the default; suspicion has to be earned by evidence.
    credulity = 0.5 + trust / 220.0 - betrayals * 0.22 + deception_quality * 0.05
    keen = llm.rng(pt_id, turn, liar, target_id, "insight").random() * 0.25
    believed = (credulity - keen) > 0.42
    return {
        "believed": believed,
        "credulity": round(credulity, 3),
        "trust": round(trust, 1),
        "prior_betrayals": betrayals,
        "reason": ("they have no reason to doubt you" if believed and betrayals == 0 else
                   "they want to believe you" if believed else
                   "you have burned them before" if betrayals else
                   "it does not quite hold together"),
    }


# ---------------------------------------------------------------------------
# The simultaneous reveal
# ---------------------------------------------------------------------------

def reveal_key(turn):
    return precommit.round_key("betrayal", turn)


def stage_reveal(pt_id, *, split_id, player_id, turn, account, truth, session_id=""):
    """Come back and write your account of where you were. It is sealed until
    everyone returning this turn has written theirs."""
    precommit.declare(
        pt_id, round_key=reveal_key(turn), turn=turn, player_id=player_id,
        intent=truth[:600], kind="return", visibility="hidden",
        announced=account[:400], payload={"split_id": split_id}, session_id=session_id)
    return {"staged": True, "round_key": reveal_key(turn)}


def resolve_reveal(pt_id, world, turn, *, present_npcs=None, party=None):
    """Everything comes out at once. Returns, per player: what they said, what
    they did, whether the gap was spotted, and what it cost them."""
    revealed = precommit.reveal(pt_id, reveal_key(turn))
    if not revealed:
        return {"revealed": [], "ruptures": []}

    out, ruptures = [], []
    for r in revealed:
        pid = r["player_id"]
        split = db.row("SELECT * FROM splits WHERE id=?", ((r["payload"] or {}).get("split_id"),))
        db.run("UPDATE splits SET status='returned' WHERE player_id=? AND playthrough_id=? AND status='away'",
               (pid, pt_id))

        lied = r["deceptive"]
        witnesses = []
        for npc_id in (present_npcs or []):
            verdict = insight(pt_id, world, liar=pid, target_kind="npc", target_id=npc_id,
                              turn=turn, deception_quality=1 if lied else 0)
            if lied and not verdict["believed"]:
                witnesses.append({"npc": npc_id, "name": world.npc_name(npc_id),
                                  "reason": verdict["reason"]})
        # Lying to people who saw through it costs the relationship, deterministically.
        for w in witnesses:
            relationships.apply_event(pt_id, w["npc"], pid, "broke_promise", turn=turn,
                                      note="caught in a story that did not hold")
        entry = {
            "player_id": pid,
            "announced": r["announced"] or (split["announced"] if split else ""),
            "actual": r["intent"],
            "deceptive": lied,
            "caught_by": witnesses,
            "held": lied and not witnesses,
            "private_turns": split["turns_taken"] if split else 0,
        }
        out.append(entry)
        if lied and witnesses:
            ruptures.append(entry)
    precommit.clear(pt_id, reveal_key(turn))
    return {"revealed": out, "ruptures": ruptures}


# ---------------------------------------------------------------------------
# Hidden roles - opt-in per room
# ---------------------------------------------------------------------------

def assign_roles(pt_id, session_id, players, *, traitors=1, seed_extra=""):
    """Deterministic per session so a reconnect cannot reroll your role."""
    rng = llm.rng(pt_id, session_id, "roles", seed_extra)
    pool = sorted(players)
    rng.shuffle(pool)
    chosen = set(pool[:max(0, min(traitors, len(pool) - 1))])
    roles = {p: ("traitor" if p in chosen else "faithful") for p in pool}
    _save_roles(pt_id, session_id, roles)
    return roles


def _save_roles(pt_id, session_id, roles):
    row = db.row("SELECT * FROM safety WHERE playthrough_id=?", (pt_id,))
    if not row:
        db.run("INSERT OR IGNORE INTO safety (playthrough_id, updated_at) VALUES (?,?)",
               (pt_id, db.now()))
        row = db.row("SELECT * FROM safety WHERE playthrough_id=?", (pt_id,))
    events = db.jload(row["events"], []) or []
    events = [e for e in events if e.get("kind") != "roles"]
    events.append({"kind": "roles", "session_id": session_id, "roles": roles})
    db.run("UPDATE safety SET events=?, updated_at=? WHERE playthrough_id=?",
           (json.dumps(events), db.now(), pt_id))


def my_role(pt_id, session_id, player_id):
    row = db.row("SELECT * FROM safety WHERE playthrough_id=?", (pt_id,))
    if not row:
        return None
    for e in db.jload(row["events"], []) or []:
        if e.get("kind") == "roles" and e.get("session_id") == session_id:
            return (e.get("roles") or {}).get(player_id)
    return None


def roles_enabled(pt_id, session_id) -> bool:
    return my_role(pt_id, session_id, "__any__") is not None or bool(
        db.row("SELECT 1 FROM safety WHERE playthrough_id=? AND events LIKE '%\"kind\": \"roles\"%'",
               (pt_id,)))


# ---------------------------------------------------------------------------
# The conspiracy board - what the panel becomes when someone is away
# ---------------------------------------------------------------------------

def board(pt_id, world, viewer, turn, party):
    away = active(pt_id)
    if not away:
        return {"active": False}
    entries = []
    for s in away:
        mine = s["player_id"] == viewer
        who = next((p for p in party if p["player_id"] == s["player_id"]), {})
        entries.append({
            "player_id": s["player_id"],
            "name": who.get("name", s["player_id"]),
            "mine": mine,
            "said": s["announced"],
            "truth": s["truth"] if mine else None,
            "destination": world.loc_name(s["destination"]) if mine and s["destination"] else None,
            "turns_taken": s["turns_taken"],
            "private_turns": s["private_turns"],
            "returns_in": max(0, s["private_turns"] - s["turns_taken"]),
            "deceptive": bool(s["deception"]) if mine else None,
        })
    return {
        "active": True, "entries": entries,
        "private_log": [{"turn": k["turn_learned"], "summary": k["summary"]}
                        for k in private_log(pt_id, viewer, turn - 12)],
        "note": "Nothing here reaches the other players until the reveal.",
    }
