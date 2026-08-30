"""Out-of-character channel, and the cheapest World Master in the product.

Two different things happen at a table. There is the story, and there is the
table talking ABOUT the story - "wait, is the gate still barred?", "does my
technique work underwater?", "hang on, who has the key?". Forcing that second
conversation through the narrator is what makes an AI RPG exhausting: every
housekeeping question becomes a paragraph of prose, costs a full turn, and
drags the story sideways.

So OOC is a separate channel with a separate contract:

  PLAYER TO PLAYER   free, instant, never touches the model, never enters the
                     story timeline. The room chats.

  PLAYER TO THE WM   a rules question, answered in one clause. The blueprint
                     caps this at 30-60 tokens and calls it the only recurring
                     model cost worth paying, and that cap is the whole point:
                     an answer that runs long has become narration, which is
                     what this channel exists to avoid.

The World Master answering here is DELIBERATELY not the narrator. It has no
prose duty and no persona - it reads world rules and current state and returns
a ruling. "Yes, still barred." "No - you never learned that." "She is at the
forge until evening."

Nothing said here is canon. OOC never enters the timeline, never moves a
relationship, and is never witnessed - so a player can ask a question without
the world reacting to them having asked it.
"""
from __future__ import annotations

from . import budget, db, llm, world_master

# The blueprint's cap. Enforced as a hard max_tokens, not a request in the
# prompt - a model asked politely to be brief will still narrate.
MAX_ANSWER_TOKENS = 60

SYSTEM = """You are the World Master, answering a rules question at the table. You are NOT narrating.

Answer in ONE clause. A ruling, not a scene. No prose, no description, no second person, no "you feel".

If the state you were given answers it, say so plainly. If it does not, say you do not know rather than inventing. If the answer is no, say no - you are the reason the rules hold.

Never longer than one short sentence."""


def _shape(row) -> dict:
    """One shape for every line on this channel. Posting, asking and reading
    back used to return three different objects - `to_wm` as a bool from one
    and an int from another, `reply` as None from one and "" from another -
    which pushes the normalising into every caller that touches it."""
    return {"id": row["id"], "player_id": row["player_id"],
            "name": row["name"] or "someone", "text": row["text"],
            "to_wm": bool(row["to_wm"]), "reply": row["reply"] or "",
            "created_at": row["created_at"]}


def _get(msg_id):
    return db.row("SELECT * FROM ooc_messages WHERE id=?", (msg_id,))


def post(session_id, pt_id, *, player, name, text, to_wm=False) -> dict:
    """Record an OOC line. Free, and never part of the story."""
    text = (text or "").strip()[:600]
    if not text:
        raise ValueError("say something")
    row_id = db.run(
        "INSERT INTO ooc_messages (session_id,playthrough_id,player_id,name,text,to_wm,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (session_id or "", pt_id, player, name[:40] or "someone", text,
         1 if to_wm else 0, db.now()))
    return _shape(_get(row_id))


def history(session_id, pt_id, limit=60) -> list:
    rows = db.rows(
        "SELECT * FROM ooc_messages WHERE (session_id=? AND session_id!='')"
        " OR playthrough_id=? ORDER BY id DESC LIMIT ?",
        (session_id or "", pt_id, limit))
    return [_shape(r) for r in rows[::-1]]


def ask_world_master(pt, world, question, *, user_id, player) -> str:
    """One short ruling. The only recurring model cost on this channel.

    Reuses the World Master's own state builder, so a ruling here and a ruling
    inside a turn are answering from exactly the same picture of the world -
    an OOC answer that disagreed with the engine would be worse than none."""
    state = world_master.build_state(pt, world, player=player)
    rules = "\n".join(f"- {r['text']}" for r in world.rules[:10])
    present = ", ".join(world.npc_name(n) for n in state.get("present", [])) or "nobody"
    prompt = (
        f"WORLD RULES:\n{rules}\n"
        f"WHERE THEY ARE: {world.loc_name(state['location'])}, day {state['day']}, "
        f"{state['phase']}\n"
        f"WHO IS HERE: {present}\n"
        f"TURN: {state['turn']}\n\n"
        f"QUESTION: {question[:300]}\n\nAnswer in one clause."
    )
    return llm.complete(
        "world_master", SYSTEM, prompt, user_id=user_id,
        max_tokens=MAX_ANSWER_TOKENS, temperature=0.2,
        stub=lambda: _stub(question, state, world)).strip()


def _stub(question, state, world) -> str:
    """Offline ruling. Deterministic and honest about being a stub, so the
    channel is fully exercisable with no key."""
    q = (question or "").lower()
    if "where" in q:
        return f"You are at {world.loc_name(state['location'])}."
    if "who" in q:
        present = [world.npc_name(n) for n in state.get("present", [])]
        return (", ".join(present) + " is here.") if present else "Nobody is here."
    if "when" in q or "time" in q:
        return f"Day {state['day']}, {state['phase']}."
    return "Nothing in the world state settles that."


def answer_and_record(msg_id, reply) -> dict:
    db.run("UPDATE ooc_messages SET reply=? WHERE id=?", (reply[:600], msg_id))
    row = _get(msg_id)
    return _shape(row) if row else {}
