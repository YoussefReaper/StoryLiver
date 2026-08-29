"""Party lifecycle - invite, kick, and making a departure consistent.

Two consensus gates, deliberately different shapes:

  INVITE needs every CURRENT player to approve. Bringing a stranger to a table
  where secrets are being kept is a decision that belongs to everyone already
  sitting at it.

  KICK needs every OTHER player to approve. The target does not get a vote -
  otherwise nobody could ever be removed - but a single dissenter among the
  rest blocks it, so a majority cannot gang up on one person.

The hard part is not the vote, it's the world afterwards. When a player leaves,
what happens to everything the NPCs knew about them? Deleting nothing leaves a
ghost: characters who grieve someone the story no longer contains. Deleting
everything rewrites history other players actually lived through.

So there are two cease modes:

  STRICT - every per-user memory, relationship and private fact tied to them is
  purged from every NPC and from world state. The world recomputes as if they
  were never there. EXCEPT anything they made public: announced, betrayed, or
  rumoured facts stay, because other players witnessed those and their own
  memories must remain true.

  SOFT - the character leaves, the memories stay. The world simply remembers
  someone who is no longer at the table.

Either way the state is ARCHIVED, never destroyed, so a re-invite restores it.
Deterministic, $0.
"""
from __future__ import annotations

import json
import uuid

from . import db, rt

KINDS = ("invite", "kick")
CEASE = ("strict", "soft")


class MotionError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Motions
# --------------------------------------------------------------------------

def _active_players(session_id):
    """Everyone with a seat and a stake. The host counts - they are a player
    who also holds the wallet, not a separate class of person. Spectators do
    NOT: they have no character to lose and no memories to cease, so giving
    them a vote on someone's removal would be a stranger's veto."""
    return [r["player_id"] for r in db.rows(
        "SELECT player_id FROM session_players WHERE session_id=?"
        " AND role IN ('player','host') AND left_at=''", (session_id,))]


def propose(session_id, *, kind, proposed_by, target_id="", target_name="") -> dict:
    if kind not in KINDS:
        raise MotionError(f"unknown motion {kind!r}")

    players = _active_players(session_id)
    if proposed_by not in players:
        raise MotionError("only a seated player may propose")

    if kind == "kick":
        if target_id not in players:
            raise MotionError("that player is not at this table")
        needed = [p for p in players if p != target_id]
        if len(needed) < 2:
            raise MotionError("a table of two has no one to arbitrate a kick")
    else:
        needed = list(players)

    open_same = db.row(
        "SELECT id FROM party_motions WHERE session_id=? AND kind=? AND target_id=?"
        " AND status='open'", (session_id, kind, target_id))
    if open_same:
        raise MotionError("that motion is already open")

    motion_id = uuid.uuid4().hex[:12]
    # The proposer's own vote is implied - proposing IS approving.
    approvals = {proposed_by: True} if proposed_by in needed else {}
    db.run(
        "INSERT INTO party_motions (id,session_id,kind,target_id,target_name,proposed_by,"
        "needed,approvals,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (motion_id, session_id, kind, target_id, target_name, proposed_by,
         json.dumps(needed), json.dumps(approvals), "open", db.now()),
    )
    rt.cache_drop(f"sl:session:{session_id}:motions")
    return motion(motion_id)


def motion(motion_id):
    row = db.row("SELECT * FROM party_motions WHERE id=?", (motion_id,))
    if not row:
        return None
    needed = db.jload(row["needed"], [])
    approvals = db.jload(row["approvals"], {})
    return {**row, "needed": needed, "approvals": approvals,
            "outstanding": [p for p in needed if p not in approvals],
            "unanimous": all(approvals.get(p) for p in needed) if needed else False}


def open_motions(session_id):
    return [motion(r["id"]) for r in db.rows(
        "SELECT id FROM party_motions WHERE session_id=? AND status='open'"
        " ORDER BY created_at", (session_id,))]


def vote(motion_id, player_id, approve=True) -> dict:
    m = motion(motion_id)
    if not m:
        raise MotionError("no such motion")
    if m["status"] != "open":
        raise MotionError("that motion is already settled")
    if player_id not in m["needed"]:
        raise MotionError("you have no vote on this motion")

    if not approve:
        # One dissenter is enough. Consensus means consensus.
        db.run("UPDATE party_motions SET status='rejected', approvals=?, resolved_at=?"
               " WHERE id=?",
               (json.dumps({**m["approvals"], player_id: False}), db.now(), motion_id))
        rt.cache_drop(f"sl:session:{m['session_id']}:motions")
        return motion(motion_id)

    approvals = {**m["approvals"], player_id: True}
    passed = all(approvals.get(p) for p in m["needed"])
    db.run("UPDATE party_motions SET approvals=?, status=?, resolved_at=? WHERE id=?",
           (json.dumps(approvals), "passed" if passed else "open",
            db.now() if passed else "", motion_id))
    rt.cache_drop(f"sl:session:{m['session_id']}:motions")
    return motion(motion_id)


# --------------------------------------------------------------------------
# Carrying out a passed kick
# --------------------------------------------------------------------------

def remove(session_id, player_id, *, playthrough_id, cease="strict") -> dict:
    """Archive, then purge. Order matters - the archive is taken from live
    state, so it must be written before anything is deleted."""
    if cease not in CEASE:
        raise MotionError(f"unknown cease mode {cease!r}")

    archived = _archive(session_id, playthrough_id, player_id, cease)

    purged = {"npc_memories": 0, "relationships": 0, "whispers": 0, "knowledge": 0}
    if cease == "strict":
        purged = _purge(playthrough_id, session_id, player_id)

    db.run("UPDATE session_players SET left_at=? WHERE session_id=? AND player_id=?",
           (db.now(), session_id, player_id))
    rt.invalidate_playthrough(playthrough_id)
    return {"removed": player_id, "cease": cease, "purged": purged,
            "archived_id": archived, "retained": "public history other players witnessed"}


def _archive(session_id, pt_id, player_id, cease):
    payload = {
        "player": db.row("SELECT * FROM session_players WHERE session_id=? AND player_id=?",
                         (session_id, player_id)),
        "npc_memories": db.rows("SELECT * FROM npc_memories WHERE playthrough_id=?"
                                " AND player_id=?", (pt_id, player_id)),
        "relationships": db.rows("SELECT * FROM relationships WHERE playthrough_id=? AND src=?",
                                 (pt_id, player_id)),
        "npc_player": db.rows("SELECT * FROM npc_player WHERE playthrough_id=? AND player_id=?",
                              (pt_id, player_id)),
        "knowledge": db.rows("SELECT * FROM knowledge WHERE playthrough_id=? AND holder_kind='player'"
                             " AND holder_id=?", (pt_id, player_id)),
        "cards": db.rows("SELECT * FROM cards WHERE playthrough_id=? AND player_id=?",
                         (pt_id, player_id)),
    }
    return db.run(
        "INSERT INTO party_archive (session_id,playthrough_id,player_id,cease,payload,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (session_id, pt_id, player_id, cease, json.dumps(payload, default=str), db.now()),
    )


def _purge(pt_id, session_id, player_id) -> dict:
    """'Their memories in every NPC head cease from existence.'

    Scoped precisely to what is THEIRS. Per-user NPC memory, their private
    relationship vectors, their private knowledge, their whispers. What is NOT
    touched: the shared timeline, and anything another player witnessed - those
    are other people's memories and deleting them would corrupt their story."""
    counts = {}
    counts["npc_memories"] = _delete(
        "DELETE FROM npc_memories WHERE playthrough_id=? AND player_id=?", (pt_id, player_id))
    counts["relationships"] = _delete(
        "DELETE FROM relationships WHERE playthrough_id=? AND src=?", (pt_id, player_id))
    counts["npc_player"] = _delete(
        "DELETE FROM npc_player WHERE playthrough_id=? AND player_id=?", (pt_id, player_id))
    counts["knowledge"] = _delete(
        "DELETE FROM knowledge WHERE playthrough_id=? AND holder_kind='player'"
        " AND holder_id=?", (pt_id, player_id))
    counts["whispers"] = _delete(
        "DELETE FROM whispers WHERE session_id=? AND (from_player=? OR to_id=?)",
        (session_id, player_id, player_id))
    counts["commits"] = _delete(
        "DELETE FROM commits WHERE playthrough_id=? AND player_id=?", (pt_id, player_id))
    rt.cache_drop(f"sl:pt:{pt_id}:snapshot")
    return counts


def _delete(sql, args):
    with db.conn() as c:
        return c.execute(sql, args).rowcount


def restore(archive_id) -> dict:
    """Re-invite. Puts back exactly what the purge took."""
    row = db.row("SELECT * FROM party_archive WHERE id=?", (archive_id,))
    if not row:
        raise MotionError("no such archive")
    payload = db.jload(row["payload"], {})
    restored = {}

    for table, records in (("npc_memories", payload.get("npc_memories")),
                           ("relationships", payload.get("relationships")),
                           ("npc_player", payload.get("npc_player")),
                           ("knowledge", payload.get("knowledge"))):
        n = 0
        for rec in records or []:
            cols = ",".join(rec.keys())
            marks = ",".join("?" for _ in rec)
            try:
                db.run(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})",
                       tuple(rec.values()))
                n += 1
            except Exception:
                continue
        restored[table] = n

    db.run("UPDATE session_players SET left_at='' WHERE session_id=? AND player_id=?",
           (row["session_id"], row["player_id"]))
    rt.cache_drop(f"sl:pt:{row['playthrough_id']}:snapshot")
    return {"restored": restored, "player_id": row["player_id"]}


def archives(session_id):
    return db.rows("SELECT id,player_id,cease,created_at FROM party_archive"
                   " WHERE session_id=? ORDER BY created_at DESC", (session_id,))
