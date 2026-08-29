"""Layer 7 - character cards, mutual approval, disconnect handling.

A card is not a form to fill in. Empty fields are filled by the cheapest model
(one call, capped) or rolled deterministically, and then EVERY player at the
table approves it before it enters play. An anomaly - an ability the world does
not have - becomes canon only if the others say yes, which is exactly how a
table handles it.

Disconnects: a grace period, then a persona-preserving stand-in that plays your
character conservatively, then a server-authoritative resync from Redis when
you come back. Your character never freezes and never runs off with your stuff.
"""
from __future__ import annotations

import json
import uuid

from . import canon, db, llm, rt, worldkit

CARD_SYSTEM = """You fill in a player's character card for a text RPG, in the voice of the world they are entering.

You are given the world, whatever the player already wrote, and which fields are blank. Fill ONLY the blanks. Never overwrite what they wrote. Keep everything ordinary and human unless they explicitly asked for otherwise - this is a person in a place, not a superhero.

Return ONLY JSON with exactly the requested keys:
{"concept":"6-12 words","voice":"how they speak, 12-25 words, specific",
 "background":"2 sentences, concrete, no destiny","drive":"what they want, one line",
 "flaw":"what will cost them, one line","gear":["item","item","item"]}"""

FIELDS = ("concept", "voice", "background", "drive", "flaw", "gear")

_ROLL = {
    "concept": ["a courier who stopped running", "a debt-collector's apprentice",
                "a surgeon struck off elsewhere", "a stonemason between towns",
                "a deserter with a good coat", "a cartographer of nowhere useful"],
    "voice": ["Short sentences. Answers questions with questions.",
              "Warm, fast, always three words ahead of themselves.",
              "Formal to the point of rudeness. Never contracts a word.",
              "Quiet, dry, funniest person in a room that is not laughing."],
    "drive": ["Get far enough away that the name stops following.",
              "Be owed something by someone who matters.",
              "Find out what happened to the one who left first.",
              "Put one thing right before the end."],
    "flaw": ["Cannot walk away from a bet.", "Lies first, thinks second.",
             "Trusts anyone who feeds them.", "Cannot let an insult go."],
    "background": ["You came in off the low road with what you could carry. "
                   "Nobody here has heard your name, and that was the point."],
    "gear": [["a good coat", "a folding knife", "someone else's letter"],
             ["a satchel of tools", "half a loaf", "a key to nothing here"],
             ["a walking stick", "three coins", "a bandage already used"]],
}


def _cid():
    return "card" + uuid.uuid4().hex[:10]


def blank(player_id, name=""):
    return {"player_id": player_id, "name": name, "concept": "", "aspects": {
        "voice": "", "background": "", "drive": "", "flaw": "", "gear": []}, "anomaly": ""}


def roll(world, player_id, name="", seed=""):
    """Deterministic fill - free, instant, and good enough that nobody has to
    use the model unless they want to."""
    rng = llm.rng(world.id, player_id, seed or "roll")
    return {
        "player_id": player_id,
        "name": name or "Traveller",
        "concept": rng.choice(_ROLL["concept"]),
        "aspects": {
            "voice": rng.choice(_ROLL["voice"]),
            "background": rng.choice(_ROLL["background"]),
            "drive": rng.choice(_ROLL["drive"]),
            "flaw": rng.choice(_ROLL["flaw"]),
            "gear": list(rng.choice(_ROLL["gear"])),
        },
        "anomaly": "",
    }


def autofill(world, draft, *, user_id, pt_id=None):
    """One capped call on the cheapest model, and only for the blanks."""
    aspects = dict(draft.get("aspects") or {})
    blanks = [f for f in FIELDS
              if not (draft.get(f) if f == "concept" else aspects.get(f))]
    if not blanks:
        return draft
    written = {f: (draft.get(f) if f == "concept" else aspects.get(f)) for f in FIELDS}
    prompt = (
        f"WORLD: {world.name} - {world.get('tagline', '')}\n"
        f"PREMISE: {world.get('premise', '')[:600]}\n\n"
        f"NAME: {draft.get('name') or '(unnamed)'}\n"
        f"ALREADY WRITTEN: {json.dumps({k: v for k, v in written.items() if v})}\n"
        f"BLANK FIELDS TO FILL: {', '.join(blanks)}\n\nJSON only."
    )

    def stub():
        rolled = roll(world, draft.get("player_id", "p"), draft.get("name", ""))
        return {"concept": rolled["concept"], **rolled["aspects"]}

    try:
        out = llm.complete("card", CARD_SYSTEM, prompt, user_id=user_id, playthrough_id=pt_id,
                           json_mode=True, max_tokens=420, temperature=0.9, stub=stub)
    except Exception:
        out = stub()

    filled = dict(draft)
    filled["aspects"] = aspects
    for f in blanks:
        value = out.get(f)
        if not value:
            continue
        if f == "concept":
            filled["concept"] = str(value)[:120]
        elif f == "gear":
            aspects["gear"] = [str(x)[:60] for x in (value if isinstance(value, list) else [value])][:5]
        else:
            aspects[f] = str(value)[:300]
    return filled


# ---------------------------------------------------------------------------
# Storage + the approval gate
# ---------------------------------------------------------------------------

def save(pt_id, draft, *, session_id="", card_id=None):
    now = db.now()
    aspects = json.dumps(draft.get("aspects") or {})
    if card_id:
        db.run("UPDATE cards SET name=?, concept=?, aspects=?, anomaly=?, updated_at=?"
               " WHERE id=? AND playthrough_id=?",
               (draft.get("name", "")[:60], draft.get("concept", "")[:160], aspects,
                (draft.get("anomaly") or "")[:300], now, card_id, pt_id))
        return get(card_id)
    card_id = _cid()
    db.run(
        "INSERT INTO cards (id,playthrough_id,session_id,player_id,name,concept,aspects,anomaly,"
        "status,approvals,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'draft','{}',?,?)",
        (card_id, pt_id, session_id, draft.get("player_id", ""), draft.get("name", "")[:60],
         draft.get("concept", "")[:160], aspects, (draft.get("anomaly") or "")[:300], now, now))
    return get(card_id)


def get(card_id):
    row = db.row("SELECT * FROM cards WHERE id=?", (card_id,))
    if not row:
        return None
    return {**dict(row), "aspects": db.jload(row["aspects"], {}) or {},
            "approvals": db.jload(row["approvals"], {}) or {},
            # The identity block is stored as JSON; hand it back parsed so the
            # client never has to double-decode a field it renders directly.
            "identity": db.jload(row["identity"], {}) or {}}


def for_playthrough(pt_id):
    return [get(r["id"]) for r in
            db.rows("SELECT id FROM cards WHERE playthrough_id=? ORDER BY created_at", (pt_id,))]


def submit(pt_id, card_id):
    db.run("UPDATE cards SET status='pending', approvals='{}', updated_at=? WHERE id=? AND playthrough_id=?",
           (db.now(), card_id, pt_id))
    return get(card_id)


def approve(pt_id, card_id, voter, *, ok=True, note="", table=None):
    """Every seated player votes. The card's owner is counted as approving
    their own; one objection is enough to send it back."""
    card = get(card_id)
    if not card:
        return None
    approvals = card["approvals"]
    approvals[voter] = {"ok": bool(ok), "note": note[:200]}
    seats = [p for p in (table or []) if p != card["player_id"]]
    votes = {k: v for k, v in approvals.items() if k in seats}
    status = card["status"]
    if any(not v["ok"] for v in votes.values()):
        status = "changes_requested"
    elif seats and len(votes) >= len(seats):
        status = "approved"
    elif not seats:
        status = "approved"                     # solo: you are the whole table
    db.run("UPDATE cards SET approvals=?, status=?, updated_at=? WHERE id=?",
           (json.dumps(approvals), status, db.now(), card_id))
    return get(card_id)


def anomaly_to_canon(pt_id, world_json, card):
    """An approved anomaly becomes a world rule, so the World Master will
    enforce it from then on instead of the player having to argue for it."""
    if card["status"] != "approved" or not card["anomaly"]:
        return None
    rules = list(world_json.get("rules") or [])
    rid = worldkit.slug(f"A_{card['name']}_{card['anomaly'][:20]}", "anomaly")
    if any(r.get("id") == rid for r in rules):
        return None
    rules.append({"id": rid,
                  "text": f"{card['name']} can do this, and the world accepts it: {card['anomaly']}. "
                          f"Nobody else can.",
                  "check": None})
    world_json["rules"] = rules
    return rid


# ---------------------------------------------------------------------------
# Disconnect: grace -> stand-in -> resync
# ---------------------------------------------------------------------------

GRACE_TURNS = 1


def standin_order(pt_id, world, card, state):
    """What your character does while you are gone: nothing irreversible. It
    holds position, keeps its mouth shut, and does not spend your things."""
    place = world.loc_name(state.get("location", "")) if state else ""
    drive = ((card or {}).get("aspects") or {}).get("drive", "")
    return {
        "kind": "standin",
        "intent": "You hold where you are, say little, and give nothing away.",
        "detail": f"Standing by in {place}." if place else "Standing by.",
        "note": "An absent player's character never spends, promises, or betrays.",
        "drive": drive,
        "safe": True,
    }


def snapshot_for_resync(pt_id, session_id, player_id, payload):
    rt.cache_set(f"sl:resync:{session_id}:{player_id}", payload, ttl=86400)


def resync(session_id, player_id):
    """Server-authoritative: the client throws away whatever it thought was
    true and takes this."""
    return rt.cache_get(f"sl:resync:{session_id}:{player_id}")


def presence_state(session_id, player_id, turn):
    seen = [p for p in rt.presence_list(session_id) if p["player_id"] == player_id]
    if seen:
        return {"state": "here"}
    return {"state": "away", "grace_turns": GRACE_TURNS, "since_turn": turn}
