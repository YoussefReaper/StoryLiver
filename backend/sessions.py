"""Rooms: create, join by code, presence, turn queue, modes.

Entry is a six-character room code and nothing else - no account, no email, no
setup. A host creates a room; anyone with the code is playing inside a minute.
"""
from __future__ import annotations

import json
import secrets
import uuid

from . import config, db, engine, memory, modetree, rt

MODES = ("coop", "chaos", "solo")
ROLES = ("host", "player", "spectator")
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no I/O/0/1


def new_code() -> str:
    for _ in range(40):
        code = "".join(secrets.choice(ALPHABET) for _ in range(6))
        if not db.row("SELECT id FROM sessions WHERE code=?", (code,)):
            return code
    raise RuntimeError("could not allocate a room code")


def create(user_id, *, world_id="emberfall", mode="coop", host_name="Host",
           protagonist=None, title=None, world_json=None, session_type="",
           day=""):
    """Open a room.

    `mode` is how the table treats each other (co-op / chaos / solo) and
    predates the mode tree. `session_type` is what KIND of game it is - one of
    the nineteen sub-modes - and it is the one that decides whether the world
    survives the session. An old client sending only `mode` still works: the
    tree maps it to the nearest sub-mode rather than refusing."""
    if mode not in MODES:
        raise ValueError("unknown mode")
    session_type = modetree.normalise(session_type, room_mode=mode)
    engine.ensure_user(user_id)
    session_id = uuid.uuid4().hex[:12]
    code = new_code()
    # Seeded from the mode: a Daily is the same world for everybody today, and
    # everything else is seeded from the session so a rematch is genuinely a
    # new world rather than the same ground with the score reset.
    seed = modetree.seed_for(session_type, day=day or db.today(), session_id=session_id)
    # Both handed on. The seed was computed here, stored on the session row and
    # passed to NOTHING - so a multiplayer Daily was not the same world for
    # everybody today, and a rematch was the same ground with the score reset,
    # which is the exact opposite of what the comment above promises. The mode
    # went the same way: the playthrough could not tell what game it was in.
    pt_id = engine.create_playthrough(user_id, world_id, protagonist, title,
                                      session_id=session_id, world_json=world_json,
                                      session_type=session_type, seed=seed)
    pt = db.row("SELECT world_id FROM playthroughs WHERE id=?", (pt_id,))
    seats = modetree.spec(session_type)["max_players"]
    db.run(
        "INSERT INTO sessions (id,code,host_user_id,playthrough_id,world_id,mode,status,max_players,"
        "settings,session_type,seed,created_at,updated_at) VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?)",
        (session_id, code, user_id, pt_id, pt["world_id"], mode,
         min(config.MAX_PLAYERS, seats), json.dumps({}), session_type, seed,
         db.now(), db.now()),
    )
    join(session_id, user_id, host_name, role="host")
    return get(session_id)


def type_of(session_id) -> str:
    """The sub-mode this room is playing. Empty on rooms created before the
    tree existed, which map to their room mode."""
    row = db.row("SELECT session_type, mode FROM sessions WHERE id=?", (session_id,))
    if not row:
        return modetree.DEFAULT_MODE
    return modetree.normalise(row["session_type"] or "", room_mode=row["mode"])


def seats(session_id) -> list:
    """Seat order for round-robin modes: the order people sat down."""
    return modetree.seat_order(players(session_id))


def may_act(session_id, player_id, turn) -> dict:
    """Whether it is this player's turn, under this mode's turn policy.

    Free and locked modes always say yes here - the rt.py lock still governs
    them. Only round-robin refuses, because "whoever clicked first" is not a
    fair initiative system when the outcome is a ranking."""
    return modetree.may_act(type_of(session_id), seats=seats(session_id),
                            player_id=player_id, turn=turn)


def settle(session_id, *, outcome, winner="") -> dict:
    """A disposable world has been decided. Close the room and mark the result.

    The world rows are LEFT IN PLACE deliberately. "Disposable" means nothing
    carries into the next session, not that the record of what happened is
    destroyed - a player who just lost a match is owed the ability to read it
    back. What makes it disposable is that the next session is a new seed and
    reads none of this."""
    row = db.row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not row:
        raise KeyError("no such session")
    mode_id = type_of(session_id)
    db.run("UPDATE sessions SET status='closed', outcome=?, ended_at=?, updated_at=?"
           " WHERE id=?",
           (json.dumps({"outcome": outcome, "winner": winner}), db.now(), db.now(),
            session_id))
    db.run("UPDATE playthroughs SET run_state='ended' WHERE id=?", (row["playthrough_id"],))
    rt.cache_drop(f"sl:session:{session_id}")

    # The result reaches every profile at the table. Without this a Duel could
    # be won and the ladder would never hear about it - the whole competitive
    # half of the spec would have been a table nobody wrote to.
    scored = []
    if modetree.family(mode_id) == "pvp":
        from . import ladder
        for p in players(session_id):
            if p["role"] == "spectator" or p.get("left_at"):
                continue
            won = bool(winner) and p["player_id"] == winner
            opponent = next((q["player_id"] for q in players(session_id)
                             if q["player_id"] != p["player_id"]
                             and q["role"] != "spectator"), "")
            try:
                scored.append(ladder.record_match(
                    account_id=p["user_id"], player_id=p["player_id"],
                    session_id=session_id, mode=mode_id,
                    outcome="win" if won else "loss",
                    opponent=opponent,
                    opponent_name=next((q["name"] for q in players(session_id)
                                        if q["player_id"] == opponent), "")))
            except ValueError:
                continue
    return {"settled": True, "session_id": session_id, "mode": mode_id,
            "disposable": modetree.is_disposable(mode_id),
            "outcome": outcome, "winner": winner, "scored": scored}


def by_code(code):
    return db.row("SELECT * FROM sessions WHERE code=?", ((code or "").strip().upper(),))


def get(session_id):
    s = db.row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not s:
        raise KeyError("no such session")
    return {**s, "players": players(session_id), "present": rt.presence_list(session_id)}


def players(session_id):
    return db.rows(
        "SELECT * FROM session_players WHERE session_id=? ORDER BY joined_at", (session_id,))


def player_row(session_id, player_id):
    return db.row("SELECT * FROM session_players WHERE session_id=? AND player_id=?",
                  (session_id, player_id))


def join(session_id, user_id, name, *, role="player", goal="", player_id=None):
    s = db.row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not s:
        raise KeyError("no such session")
    if s["status"] == "closed":
        raise ValueError("this room is closed")

    engine.ensure_user(user_id)
    existing = db.row(
        "SELECT * FROM session_players WHERE session_id=? AND user_id=?", (session_id, user_id))
    if existing:
        return existing

    seated = [p for p in players(session_id) if p["role"] != "spectator"]
    # The mode's own ceiling, not just the room's. A Duel seats two; letting a
    # third in as a player would make the turn order incoherent, so they watch.
    ceiling = min(s["max_players"],
                  modetree.spec(modetree.normalise(s.get("session_type") or "",
                                                   room_mode=s["mode"]))["max_players"])
    if role != "spectator" and len(seated) >= ceiling:
        role = "spectator"

    player_id = player_id or ("host" if role == "host" else f"p{secrets.token_hex(3)}")
    is_host = 1 if role == "host" else 0
    # The host's wallet funds the room; friends they bring pay nothing. Another
    # player only becomes a payer if they explicitly chip in.
    db.run(
        "INSERT INTO session_players (session_id,player_id,user_id,name,role,is_host,pays,goal,joined_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, player_id, user_id, name[:40] or "Traveller", role, is_host, is_host,
         goal[:200], db.now()),
    )
    db.run("UPDATE sessions SET updated_at=? WHERE id=?", (db.now(), session_id))

    if role != "spectator":
        pt = db.row("SELECT * FROM playthroughs WHERE id=?", (s["playthrough_id"],))
        memory.seed_player(pt["id"], engine.world_for(pt), player_id)
    return player_row(session_id, player_id)


def contribute(session_id, player_id, pays=True):
    """A second payer stacks their daily allowance into the room."""
    db.run("UPDATE session_players SET pays=? WHERE session_id=? AND player_id=?",
           (1 if pays else 0, session_id, player_id))
    return player_row(session_id, player_id)


def set_goal(session_id, player_id, goal):
    db.run("UPDATE session_players SET goal=? WHERE session_id=? AND player_id=?",
           (goal[:200], session_id, player_id))
    return player_row(session_id, player_id)


def leave(session_id, player_id):
    rt.presence_drop(session_id, player_id)
    return True


def close(session_id):
    db.run("UPDATE sessions SET status='closed', updated_at=? WHERE id=?", (db.now(), session_id))


def for_user(user_id):
    return db.rows(
        "SELECT s.*, p.current_turn, p.title FROM sessions s"
        " JOIN playthroughs p ON p.id = s.playthrough_id"
        " WHERE s.id IN (SELECT session_id FROM session_players WHERE user_id=?)"
        " ORDER BY s.updated_at DESC", (user_id,))


def whispers_for(session_id, player_id, limit=60):
    return db.rows(
        "SELECT * FROM whispers WHERE session_id=? AND (from_player=? OR (to_kind='player' AND to_id=?))"
        " ORDER BY id DESC LIMIT ?", (session_id, player_id, player_id, limit))[::-1]


def public(session):
    """What a client is allowed to see about a room."""
    mode_id = modetree.normalise(session.get("session_type") or "",
                                 room_mode=session.get("mode", "coop"))
    return {
        "id": session["id"], "code": session["code"], "mode": session["mode"],
        # What kind of game this is, and - the part that matters to a player
        # about to spend an evening in it - whether the world will be here
        # tomorrow.
        "session_type": mode_id,
        "mode_spec": modetree.public(mode_id),
        "seed": session.get("seed", 0),
        "outcome": db.jload(session.get("outcome") or "", {}) or {},
        "status": session["status"], "world_id": session["world_id"],
        "playthrough_id": session["playthrough_id"],
        "max_players": session["max_players"],
        "premium_allowed": bool(session.get("premium_allowed", 0)),
        "players": [{"player_id": p["player_id"], "name": p["name"], "role": p["role"],
                     "is_host": bool(p["is_host"]), "pays": bool(p["pays"]), "goal": p["goal"],
                     "avatar_url": p.get("avatar_url", ""),
                     "life_state": p.get("life_state", "alive"),
                     "resolution": p.get("resolution", "")}
                    for p in session.get("players", [])
                    if not p.get("left_at")],
        "present": [p["player_id"] for p in session.get("present", [])],
    }


def set_premium_allowed(session_id, user_id, allowed: bool) -> dict:
    """Deep Prose is a ROOM decision, and only the host may make it.

    The narrator is shared - one passage is written for the whole table - so a
    per-player premium toggle was never coherent, and worse, it let any player
    spend the host's Mana at 4x by ticking a box. Only the host's own user id
    can move this flag."""
    session = db.row("SELECT host_user_id FROM sessions WHERE id=?", (session_id,))
    if not session:
        raise ValueError("no such room")
    if session["host_user_id"] != user_id:
        raise PermissionError("only the host sets Deep Prose for the room")
    db.run("UPDATE sessions SET premium_allowed=?, updated_at=? WHERE id=?",
           (1 if allowed else 0, db.now(), session_id))
    rt.cache_drop(f"sl:session:{session_id}")
    return {"premium_allowed": bool(allowed)}


def premium_allowed(session_id) -> bool:
    if not session_id:
        return False
    row = db.row("SELECT premium_allowed FROM sessions WHERE id=?", (session_id,))
    return bool(row["premium_allowed"]) if row else False
