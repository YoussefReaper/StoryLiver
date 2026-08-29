"""Narrator - the only component that writes prose.

It sees persona anchors + compact retrieved state + the validated action. It
never sees the raw transcript, so there is no growing blob to drift inside.
Anti-repetition (Pain #4) is enforced two ways: a hard ban list of the
therapy-speak and purple-prose tics players complain about, and a rolling list
of the openings already used in this playthrough that must not recur.
"""
from . import arcs, db, llm, memory, modes

BANNED = (
    "I understand your frustration; I hear you; a mix of X and Y; a testament to; "
    "little did you know; the air was thick with; sends shivers; a shiver runs down; "
    "you can't help but; something shifts in the air; the weight of it all; "
    "in that moment; time seems to slow; a whirlwind of emotions; barely above a whisper; "
    "eyes glinting with mischief; a knowing smile; leaving you to wonder"
)

SYSTEM = f"""You are the Narrator of a literary text RPG. Second person, present tense, addressed to "you".

HARD RULES
- 90-150 words. Never longer. Stop on a live moment, never on a summary.
- Dialogue must obey each character's VOICE line exactly. A character's constraints and taboos are absolute.
- Only state facts given to you. Never invent an item, an ally, a name, or an event that is not in the state you were handed.
- Never narrate the player's feelings or decisions for them. Show the world; let them react.
- Never ask "what do you do?" and never offer a menu of options.
- Never use any of these dead phrases or anything like them: {BANNED}.
- No therapy-speak, no validation language, no motivational summary. Nobody in this world is a life coach.
- Do not open with the same construction you used before (see FORBIDDEN OPENINGS).

Write only the prose. No headings, no quotes around the whole thing, no meta."""


def _openings(pt_id, n=8):
    rows = db.rows(
        "SELECT text FROM narrative WHERE playthrough_id=? AND kind IN ('narration','beat','npc')"
        " ORDER BY id DESC LIMIT ?", (pt_id, n))
    outs = []
    for r in rows:
        words = r["text"].split()
        if words:
            outs.append(" ".join(words[:5]))
    return outs


def _stub(world, pt, action, verdict, extra):
    r = llm.rng(pt["current_turn"], action)
    loc = world.loc_by_id[pt["current_location"]]
    who = verdict["state"]["present"]
    name = world.npc_name(who[0]) if who else "no one"
    weather = r.choice(["Rain ticks on the shutters.", "The air tastes of iron.",
                        "Somewhere a dog will not stop.", "The light is going copper."])
    return (f"{weather} {loc['name']} holds the sound of you the way a room holds a stranger. "
            f"{verdict.get('consequence') or action.strip()} "
            f"{name} takes that in without hurrying, and gives you back exactly as much as it was worth. "
            f"{extra or ''}").strip()


def narrate(pt, world, action, verdict, *, user_id, premium=False, beat=None,
            npc_action=None, kind="narration", player=memory.SOLO, actor_name=None):
    state = verdict["state"]
    present = state["present"]
    # The beat the World Master detected keys which canon lines a character is
    # allowed to reach for this turn - a signature line lands because it fits
    # the moment, not because it fires every turn.
    moment = verdict.get("beat_key", "")
    anchors = "\n".join(memory.anchor_block(world, n, beat=moment)
                        for n in present) or "  (nobody else is present)"
    events = memory.retrieve_events(pt["id"], state["turn"], action + " " + (beat or ""), k=7)
    loc = world.loc_by_id[state["location"]]
    forbidden = _openings(pt["id"])

    fate_now = [f for f in world.fated_events if f["turn"] == state["turn"]]
    fate_line = f"\nHAPPENING RIGHT NOW, UNSTOPPABLE: {fate_now[0]['desc']}" if fate_now else ""

    parts = [
        f"PLACE: {loc['name']} - {loc['desc']}",
        f"TIME: day {state['day']}, {state['phase']}",
        f"\nCHARACTERS PRESENT (obey these exactly):\n{anchors}",
        f"\nWHAT THEY FEEL ABOUT THE PLAYER:\n{memory.relationship_block(pt['id'], world, present, player)}",
        f"\nESTABLISHED FACTS YOU MUST NOT CONTRADICT:\n{memory.compact_timeline(events)}",
        fate_line,
    ]
    if npc_action:
        parts.append(f"\nAN NPC ACTS FIRST, UNPROMPTED: {npc_action}\nNarrate this as something that happens TO the player, initiated by that character.")
    if beat:
        parts.append(f"\nTHE STORY TURNS: {beat}\nWeave this in as something the world does, not something the player chose.")
    if action:
        who = actor_name.upper() if actor_name else "THE PLAYER"
        parts.append(f"\n{who} ACTS: {action}")
        if actor_name:
            parts.append(f'Address the passage to {actor_name} as "you". Name the other players only where they act.')
    if verdict.get("consequence"):
        parts.append(f"WHAT ACTUALLY RESULTS (narrate this, do not change it): {verdict['consequence']}")
    if forbidden:
        parts.append("\nFORBIDDEN OPENINGS (do not begin with any of these constructions):\n" +
                     "\n".join("  - " + f for f in forbidden))
    parts.append("\nWrite the passage.")

    # Modes re-tune the narrator's register and the AU premise reframes the
    # whole situation. Both are appended to the SYSTEM constant rather than the
    # per-turn prompt, so they stay stable across the story - a tone that
    # drifted turn to turn would be worse than no tone at all. On a default
    # world both are empty and SYSTEM is byte-identical to what it always was.
    system = SYSTEM
    extra = "\n".join(x for x in (modes.tone_directive(pt["id"]),
                                  arcs.au_directive(pt["id"])) if x)
    if extra:
        system = SYSTEM + "\n\n" + extra

    model = "narrator_premium" if premium else "narrator"
    try:
        return llm.complete(model, system, "\n".join(parts), user_id=user_id,
                            playthrough_id=pt["id"], max_tokens=420, temperature=0.95,
                            stub=lambda: _stub(world, pt, action, verdict, beat or npc_action)).strip()
    except llm.LLMError:
        return _stub(world, pt, action, verdict, beat or npc_action)


def refusal(pt, world, action, verdict, *, user_id):
    """A rejected action still gets prose - the world pushes back in character
    rather than showing an error box."""
    state = verdict["state"]
    loc = world.loc_by_id[state["location"]]
    anchors = "\n".join(memory.anchor_block(world, n) for n in state["present"]) or "  (nobody else is present)"
    prompt = (
        f"PLACE: {loc['name']} - {loc['desc']}\n"
        f"CHARACTERS PRESENT:\n{anchors}\n\n"
        f"THE PLAYER TRIED: {action}\n"
        f"IT CANNOT HAPPEN, BECAUSE: {verdict['reason']}\n\n"
        "In 50-90 words, narrate the attempt failing for exactly that reason. The world refuses; "
        "it does not lecture. Do not repeat the reason verbatim. Do not apologise. Do not explain "
        "the rules of the game."
    )
    try:
        return llm.complete("narrator", SYSTEM, prompt, user_id=user_id,
                            playthrough_id=pt["id"], max_tokens=260, temperature=0.9,
                            stub=lambda: f"You try it, and the world does not move. {verdict['reason']}").strip()
    except llm.LLMError:
        return verdict["reason"]
