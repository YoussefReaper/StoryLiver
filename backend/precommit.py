"""Layers 3/5/6 - simultaneous declare-and-reveal. Deterministic, $0 LLM.

Diplomacy's core mechanic: everyone writes their order, nobody sees anyone
else's, and when the last order lands they all reveal and resolve together. No
turn-order peeking, so an alliance is worth exactly what the people in it are
worth.

The same primitive serves three layers:
  * betrayal  - a departure is a private commitment revealed later
  * awareness - pre-commit review lets allies cover a blind spot, or exploit it
  * combat    - WEGO rounds are just a commit round with a resolver

`visibility` is the whole design:
  open    everyone sees the intent while declaring (coordination)
  vague   others see the announced version, not the real one (deception)
  hidden  others see only that you have committed (the traitor's move)
"""
from __future__ import annotations

import json

from . import db

VISIBILITY = ("open", "vague", "hidden")


def round_key(kind: str, turn: int, suffix: str = "") -> str:
    return f"{kind}:{turn}" + (f":{suffix}" if suffix else "")


def declare(pt_id, *, round_key, turn, player_id, intent, kind="action",
            visibility="open", announced="", payload=None, session_id=""):
    """Submit or replace your order. Replacing is allowed until the round locks -
    which is exactly the tension: you may change your mind, but not after
    someone else's move is visible, because it never is."""
    if visibility not in VISIBILITY:
        visibility = "open"
    body = dict(payload or {})
    if announced:
        body["announced"] = announced
    existing = db.row(
        "SELECT * FROM commits WHERE playthrough_id=? AND round_key=? AND player_id=?",
        (pt_id, round_key, player_id))
    if existing and existing["locked"]:
        return {"ok": False, "reason": "this round is already resolving"}
    if existing:
        db.run("UPDATE commits SET intent=?, kind=?, visibility=?, payload=? WHERE id=?",
               (intent[:600], kind, visibility, json.dumps(body), existing["id"]))
        cid = existing["id"]
    else:
        cid = db.run(
            "INSERT INTO commits (playthrough_id,session_id,round_key,turn,player_id,kind,intent,"
            "visibility,payload,locked,revealed,created_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,?)",
            (pt_id, session_id, round_key, turn, player_id, kind, intent[:600],
             visibility, json.dumps(body), db.now()))
    return {"ok": True, "id": cid}


def withdraw(pt_id, round_key, player_id):
    row = db.row("SELECT * FROM commits WHERE playthrough_id=? AND round_key=? AND player_id=?",
                 (pt_id, round_key, player_id))
    if not row or row["locked"]:
        return False
    db.run("DELETE FROM commits WHERE id=?", (row["id"],))
    return True


def submitted(pt_id, round_key):
    return db.rows("SELECT * FROM commits WHERE playthrough_id=? AND round_key=? ORDER BY id",
                   (pt_id, round_key))


def board(pt_id, round_key, viewer, expected):
    """The pre-commit review board, from ONE player's side. This is what makes
    coordination possible without making deception impossible."""
    rows = submitted(pt_id, round_key)
    have = {r["player_id"] for r in rows}
    cards = []
    for r in rows:
        payload = db.jload(r["payload"], {}) or {}
        mine = r["player_id"] == viewer
        if mine or r["visibility"] == "open":
            shown, truth = r["intent"], True
        elif r["visibility"] == "vague":
            shown, truth = (payload.get("announced") or "Something they are keeping vague."), False
        else:
            shown, truth = ("Committed. They are not saying to what."), False
        cards.append({
            "player_id": r["player_id"], "mine": mine, "kind": r["kind"],
            "visibility": r["visibility"], "shown": shown, "is_truth": truth,
            "locked": bool(r["locked"]), "revealed": bool(r["revealed"]),
        })
    waiting = [p for p in expected if p not in have]
    return {
        "round_key": round_key, "cards": cards, "waiting": waiting,
        "ready": not waiting and bool(rows),
        "count": len(rows), "expected": len(expected),
    }


def lock(pt_id, round_key):
    db.run("UPDATE commits SET locked=1 WHERE playthrough_id=? AND round_key=?",
           (pt_id, round_key))


def reveal(pt_id, round_key):
    """The simultaneous reveal. Everything true, all at once, in submit order."""
    lock(pt_id, round_key)
    db.run("UPDATE commits SET revealed=1 WHERE playthrough_id=? AND round_key=?",
           (pt_id, round_key))
    out = []
    for r in submitted(pt_id, round_key):
        payload = db.jload(r["payload"], {}) or {}
        announced = payload.get("announced") or ""
        out.append({
            "player_id": r["player_id"], "kind": r["kind"], "intent": r["intent"],
            "visibility": r["visibility"], "announced": announced,
            "deceptive": bool(announced and announced.strip().lower() != r["intent"].strip().lower()),
            "payload": payload,
        })
    return out


def clear(pt_id, round_key):
    db.run("DELETE FROM commits WHERE playthrough_id=? AND round_key=?", (pt_id, round_key))
