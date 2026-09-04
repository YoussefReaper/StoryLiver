"""NPC simulation - the Stanford Generative Agents loop, per character.

  observe   -> memory stream (append-only, importance-scored)
  reflect   -> synthesise opinions from retrieved memories, every N turns
  plan      -> a short intention list the NPC works through
  act       -> occasionally initiate, unprompted, and it gets labelled in the UI

In the full version every stage is keyed by (npc, player): a character builds a
separate history of each person it has met, so it can trust one player and
distrust another in the same room. Retrieval scoring is unchanged.
"""
from __future__ import annotations

import json

from . import db, llm, memory

REFLECT_SYSTEM = """You are the private inner voice of one character in a story. You are not talking to anyone.

From the memories given, write 2 short conclusions this character has drawn - opinions, suspicions, resolutions. First person. Specific to named people and events. No hedging, no therapy language, no self-help. A conclusion, not a summary.

Return ONLY JSON: {"reflections": ["...", "..."], "plan": ["short intention", "short intention"]}"""

ACT_SYSTEM = """You are one character in a story, deciding whether to do something right now without being asked.

Return ONLY JSON:
{"act": true|false, "action": "one concrete sentence in third person, present tense, describing what you do or say to the protagonist", "importance": 1-5, "toward": "affinity|trust|fear|obligation", "delta": -15..15}

Say act:false unless you have a real reason arising from your goals, your memories, or how you feel about this particular person right now. Most of the time, act:false. Never break your own constraints or taboos. Never speak if your voice line says you do not speak.

WHEN YOU DO ACT, PUT SOMETHING TO THEM. A character who only makes statements is furniture. Ask a question, make a request, offer a trade, set a condition, or refuse something they wanted - anything that leaves the protagonist with a decision they did not have a moment ago. "She tells you the well is poisoned" is furniture. "She asks what you were doing near the well last night" is a character.

You are NOT here to be agreeable. If your goals cut across theirs, say so. If they have not earned what they are asking for, do not give it. Wanting different things is what makes you a person."""

WHISPER_SYSTEM = """You are one character in a story. Someone has just spoken to you privately, where nobody else can hear.

Answer in character, in 40-90 words. Obey your VOICE, CONSTRAINTS and TABOOS absolutely - a character who does not speak does not speak here either; describe what they do instead. Privacy changes what you are willing to say, not who you are. No narrator voice, no scene-setting: this is your reply.

Return ONLY JSON:
{"reply": "what you say or do", "trust": -15..15, "affinity": -15..15, "fear": -15..15, "obligation": -15..15, "remember": "one sentence you will remember about this person"}"""


def _rel_words(r):
    if not r:
        return "no opinion"
    bits = []
    for k, warm, cold in [("affinity", "likes", "dislikes"), ("trust", "trusts", "distrusts"),
                          ("fear", "fears", "is easy around"), ("obligation", "owes", "owes nothing to")]:
        v = r[k]
        if abs(v) >= 15:
            bits.append(f"{warm if v > 0 else cold} them ({v:+.0f})")
    return ", ".join(bits) or "neutral"


# --------------------------------------------------------------------------

def observe_turn(pt_id, turn, present, action_text, consequence, importance=3,
                 player=memory.SOLO, actor_name="the traveller"):
    """Everyone in the room remembers what they saw - about this player
    specifically. Cheap, no model call."""
    for npc_id in present:
        memory.npc_observe(pt_id, npc_id, turn,
                           f"I saw {actor_name}: {action_text}. Result: {consequence}",
                           importance=importance, player=player)


def observe_fate(pt_id, turn, fated):
    for st in memory.all_npc_states(pt_id):
        if st["alive"]:
            memory.npc_observe(pt_id, st["npc_id"], turn, fated["desc"],
                               importance=5, kind="fate", player=memory.SHARED)


# --------------------------------------------------------------------------
# D10 - conversational continuity. An NPC's goal ("find out where Coal came
# from") was a CONSTANT anchor with no memory of ever having asked, so the
# dialogue path re-surfaced the exact same question turn after turn even
# though the player answered it three turns ago. Deterministic - the same
# keyword-matching philosophy as persona.detect_beat and modes.check_action -
# because a live judgement of "was this answered" would be a per-turn model
# call for something arithmetic can decide well enough.
# --------------------------------------------------------------------------

_QUESTION_CUES = (
    "find out", "ask ", "asks ", "asking ", "learn where", "learn who",
    "learn what", "learn why", "discover where", "discover who",
    "know where", "know who", "know what happened", "wants to know",
    "curious about", "figure out", "get answers", "get the truth",
)


def _is_question_goal(text: str) -> bool:
    """A goal that reads as something the character is trying to LEARN from
    the player, as opposed to something they intend to DO. Only these are
    tracked - a goal like "protect the herbary" has no question to stop
    repeating."""
    t = (text or "").lower()
    return any(cue in t for cue in _QUESTION_CUES)


def question_state(pt_id, npc_id, player) -> dict:
    ps = memory.npc_player_state(pt_id, npc_id, player)
    return db.jload(ps["question_state"], {})


def answered_goals(pt_id, npc_id, player) -> set:
    """The set of this NPC's own goal strings the player has already
    addressed - what the anchor block and the autonomous planner must both
    stop re-surfacing as an open pursuit."""
    state = question_state(pt_id, npc_id, player)
    return {g for g, v in state.items() if v.get("state") == "answered"}


def active_goals(goals, pt_id, npc_id, player) -> list:
    """This NPC's goal list with already-answered questions removed. Used
    everywhere a goal list feeds a prompt, so a resolved question cannot
    leak back in through a different code path than the anchor block."""
    dropped = answered_goals(pt_id, npc_id, player)
    return [g for g in (goals or []) if g not in dropped]


def track_questions(pt_id, npc_id, player, goals, turn):
    """Called once per turn for every NPC present. Two transitions, in order:

    1. A question asked on an EARLIER turn, with this NPC still present now,
       is the exchange actually happening - mark it answered, and record it
       as a memory so npc_recall can surface "they already told me" the same
       way it surfaces anything else the character was told.
    2. A question never tracked before, with this NPC present to ask it, is
       marked pending - the state now reflects that this NPC has this open
       ask on the table, the same way `pending` on a card means "waiting on
       the table", not "this happened."

    An already-answered goal is left alone permanently: the fix for
    "the NPC forgot" is not "the NPC asks again in five turns."
    """
    state = question_state(pt_id, npc_id, player)
    changed = False
    for goal in goals or []:
        if not _is_question_goal(goal):
            continue
        entry = state.get(goal)
        if entry and entry.get("state") == "pending" and entry.get("turn", turn) < turn:
            state[goal] = {"state": "answered", "turn": turn}
            memory.npc_observe(
                pt_id, npc_id, turn,
                f"They already answered this for me: {goal}",
                importance=4, kind="reflection", player=player)
            changed = True
        elif not entry:
            state[goal] = {"state": "pending", "turn": turn}
            changed = True
    if changed:
        memory.set_npc_player_state(pt_id, npc_id, player,
                                    question_state=json.dumps(state))
    return state


def reflect(pt, world, npc_id, *, user_id, player=memory.SOLO):
    turn = pt["current_turn"]
    st = memory.npc_state(pt["id"], npc_id)
    if not st or not st["alive"]:
        return None
    ps = memory.npc_player_state(pt["id"], npc_id, player)
    if turn - ps["last_reflect_turn"] < 6:
        return None
    npc = world.by_id[npc_id]
    # D10/D15: a goal the player already answered/addressed drops out of the
    # recall query and the plan - otherwise a reflection keeps circling back
    # to "find out where Coal came from" long after Coal already told them.
    goals = active_goals(npc["anchors"]["goals"], pt["id"], npc_id, player) \
        or npc["anchors"]["goals"]
    mems = memory.npc_recall(pt["id"], npc_id, turn, " ".join(goals),
                             k=8, player=player)
    if len(mems) < 3:
        return None
    rel = memory.rel_to(pt["id"], npc_id, player)
    prompt = (
        f"{memory.anchor_block(world, npc_id, answered_goals=answered_goals(pt['id'], npc_id, player))}\n\n"
        f"HOW YOU FEEL ABOUT THIS PERSON: {_rel_words(rel)}\n\n"
        "YOUR MEMORIES:\n" + "\n".join(f"  - {m['text']}" for m in mems) +
        f"\n\nIt is day {world.day_for(turn)}, {world.phase_for(turn)}. Reflect. JSON only."
    )

    def stub():
        r = llm.rng(turn, npc_id, player, "reflect")
        g = goals[0].rstrip(".") if goals else "what happens next"
        return {"reflections": [f"I keep coming back to one thing: {g.lower()}.",
                                r.choice(["This one is not what they say they are.",
                                          "Nobody here is going to do this for me.",
                                          "I have less time than I have been telling myself."])],
                "plan": [g, r.choice(["watch this one closely", "get ahead of it",
                                      "settle the debt", "keep the door shut"])]}

    try:
        out = llm.complete("npc", REFLECT_SYSTEM, prompt, user_id=user_id,
                           playthrough_id=pt["id"], json_mode=True, max_tokens=260,
                           temperature=0.85, stub=stub)
    except llm.LLMError:
        out = stub()

    refl = [str(x) for x in (out.get("reflections") or [])][:3]
    plan = [str(x) for x in (out.get("plan") or [])][:3]
    for text in refl:
        memory.npc_observe(pt["id"], npc_id, turn, text, importance=5,
                           kind="reflection", player=player)
    prior = db.jload(ps["reflections"], [])
    memory.set_npc_player_state(
        pt["id"], npc_id, player,
        reflections=json.dumps((prior + [{"turn": turn, "text": t} for t in refl])[-12:]),
        plan=json.dumps(plan), last_reflect_turn=turn)
    return {"npc": npc_id, "player": player, "reflections": refl, "plan": plan}


def maybe_act(pt, world, npc_id, *, user_id, player=memory.SOLO, actor_name="the traveller"):
    turn = pt["current_turn"]
    st = memory.npc_state(pt["id"], npc_id)
    if not st or not st["alive"]:
        return None
    ps = memory.npc_player_state(pt["id"], npc_id, player)
    if turn - ps["last_act_turn"] < 3:
        return None
    npc = world.by_id[npc_id]
    rel = memory.rel_to(pt["id"], npc_id, player)
    # D10/D15: goals already answered drop out of both the recall QUERY and
    # any stale entry in the STORED plan - a plan written by reflect() before
    # the answer landed can otherwise carry the literal question text for up
    # to reflect()'s own 6-turn cooldown after it was resolved.
    dropped = answered_goals(pt["id"], npc_id, player)
    goals = [g for g in npc["anchors"]["goals"] if g not in dropped] or npc["anchors"]["goals"]
    mems = memory.npc_recall(pt["id"], npc_id, turn,
                             f"{actor_name} " + " ".join(goals), k=5, player=player)
    plan = [p for p in db.jload(ps["plan"], [])
           if not any(g.lower() in p.lower() for g in dropped)]

    prompt = (
        f"{memory.anchor_block(world, npc_id, answered_goals=dropped)}\n\n"
        f"WHERE YOU ARE: {world.loc_name(st['location'])}, day {world.day_for(turn)}, {world.phase_for(turn)}\n"
        f"{actor_name} IS HERE TOO.\n"
        f"HOW YOU FEEL ABOUT THEM: {_rel_words(rel)}\n"
        f"YOUR CURRENT INTENTIONS: {'; '.join(plan) or 'none set'}\n\n"
        "WHAT YOU REMEMBER ABOUT THEM:\n" + "\n".join(f"  - {m['text']}" for m in mems) +
        "\n\nDo you act? JSON only."
    )

    def stub():
        r = llm.rng(turn, npc_id, player, "act")
        if r.random() > 0.35:
            return {"act": False, "action": "", "importance": 1, "toward": "affinity", "delta": 0}
        goal = goals[0].rstrip(".").lower()
        return {"act": True,
                "action": f"{npc['name']} crosses to {actor_name} without being asked, because of one thing: to {goal}.",
                "importance": 3, "toward": r.choice(["affinity", "trust", "obligation"]),
                "delta": r.choice([-8, -4, 4, 6, 8])}

    try:
        out = llm.complete("npc", ACT_SYSTEM, prompt, user_id=user_id,
                           playthrough_id=pt["id"], json_mode=True, max_tokens=200,
                           temperature=0.9, stub=stub)
    except llm.LLMError:
        out = stub()

    if not out.get("act") or not (out.get("action") or "").strip():
        return None
    toward = out.get("toward") if out.get("toward") in memory.REL_KEYS else "affinity"
    try:
        delta = max(-15, min(15, float(out.get("delta", 0) or 0)))
    except (TypeError, ValueError):
        delta = 0.0
    memory.set_npc_player_state(pt["id"], npc_id, player, last_act_turn=turn)
    db.run("UPDATE npc_state SET last_act_turn=? WHERE playthrough_id=? AND npc_id=?",
           (turn, pt["id"], npc_id))
    return {"npc": npc_id, "name": npc["name"], "action": out["action"].strip(),
            "importance": int(out.get("importance", 3) or 3),
            "delta": {"npc": npc_id, "src": player, toward: delta, "note": "acted on their own"}}


def whisper(pt, world, npc_id, text, *, user_id, player=memory.SOLO, speaker="someone"):
    """A private word with one character. Never broadcast; remembered by that
    character about that player only."""
    turn = pt["current_turn"]
    npc = world.by_id[npc_id]
    rel = memory.rel_to(pt["id"], npc_id, player)
    mems = memory.npc_recall(pt["id"], npc_id, turn, text, k=5, player=player)

    prompt = (
        f"{memory.anchor_block(world, npc_id)}\n\n"
        f"WHERE: {world.loc_name(memory.npc_state(pt['id'], npc_id)['location'])}, "
        f"day {world.day_for(turn)}, {world.phase_for(turn)}. Nobody else can hear this.\n"
        f"HOW YOU FEEL ABOUT {speaker}: {_rel_words(rel)}\n\n"
        "WHAT YOU REMEMBER ABOUT THEM:\n" + ("\n".join(f"  - {m['text']}" for m in mems) or "  (nothing yet)") +
        f"\n\n{speaker} says to you, privately: \"{text}\"\n\nReply. JSON only."
    )

    def stub():
        r = llm.rng(turn, npc_id, player, text)
        return {"reply": f"{npc['name']} takes that in, and gives it back changed: "
                         f"\"{r.choice(['Not here.', 'Say the rest of it.', 'And what do you want for that?'])}\"",
                "trust": r.choice([-4, 0, 3, 6]), "affinity": r.choice([-3, 0, 2, 4]),
                "fear": 0, "obligation": 0,
                "remember": f"They came to me privately about: {text[:80]}"}

    try:
        out = llm.complete("npc", WHISPER_SYSTEM, prompt, user_id=user_id,
                           playthrough_id=pt["id"], json_mode=True, max_tokens=320,
                           temperature=0.9, stub=stub)
    except llm.LLMError:
        out = stub()

    reply = (out.get("reply") or "").strip() or stub()["reply"]
    memory.npc_observe(pt["id"], npc_id, turn,
                       out.get("remember") or f"{speaker} spoke to me privately: {text[:100]}",
                       importance=4, player=player)
    delta = {"npc": npc_id, "src": player, "note": "spoke privately"}
    for k in memory.REL_KEYS:
        try:
            delta[k] = max(-15, min(15, float(out.get(k, 0) or 0)))
        except (TypeError, ValueError):
            delta[k] = 0.0
    memory.apply_deltas(pt["id"], [delta], turn, player)
    return {"reply": reply, "delta": delta}


def pick_actor(pt, state, player=memory.SOLO):
    """Who is most likely to make a move on this player? Strongest feeling in
    the room wins, with a cooldown so one NPC cannot monopolise the story."""
    turn = state["turn"]
    best, best_score = None, 0.0
    for npc_id in state["present"]:
        st = memory.npc_state(pt["id"], npc_id)
        if not st or not st["alive"]:
            continue
        ps = memory.npc_player_state(pt["id"], npc_id, player)
        if turn - ps["last_act_turn"] < 3:
            continue
        r = memory.rel_to(pt["id"], npc_id, player)
        intensity = (abs(r["affinity"]) + abs(r["trust"]) + r["fear"] * 1.4 + r["obligation"]) / 400.0 if r else 0.0
        patience = min(1.0, (turn - ps["last_act_turn"]) / 10.0)
        score = intensity * 0.6 + patience * 0.4
        if score > best_score:
            best, best_score = npc_id, score
    # Below the bar the room stays quiet; NPCs acting every turn is noise.
    return best if best_score >= 0.28 else None
