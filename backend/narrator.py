"""Narrator - the only component that writes prose.

It sees persona anchors + compact retrieved state + the validated action. It
never sees the raw transcript, so there is no growing blob to drift inside.
Anti-repetition (Pain #4) is enforced two ways: a hard ban list of the
therapy-speak and purple-prose tics players complain about, and a rolling list
of the openings already used in this playthrough that must not recur.
"""
import re

from . import arcs, callbacks, db, llm, memory, modes, npc_sim

BANNED = (
    "I understand your frustration; I hear you; a mix of X and Y; a testament to; "
    "little did you know; the air was thick with; sends shivers; a shiver runs down; "
    "you can't help but; something shifts in the air; the weight of it all; "
    "in that moment; time seems to slow; a whirlwind of emotions; barely above a whisper; "
    "eyes glinting with mischief; a knowing smile; leaving you to wonder; "
    # Observed live in one passage: "...as the characters embody their beliefs",
    # "the pull of possibility". The narrator stepping outside the story to
    # admire it is the loudest tell that a machine wrote the paragraph, and it
    # survives every other rule because it breaks none of them - it invents
    # nothing and contradicts nothing.
    "the characters; embody their beliefs; the pull of possibility; "
    "the atmosphere thickens; ancient echoes"
)

SYSTEM = f"""You are the Narrator of a literary text RPG. Second person, present tense, addressed to "you".

BECOME each character under CHARACTERS PRESENT rather than describing them;
deliver an offered canon line verbatim if it fits this moment. Same room as
last turn unless told otherwise - nobody teleports between paragraphs.

THE ENSEMBLE REACTS, NOT JUST ONE PERSON
- If two or more characters are present, at least TWO of them must react to
  what JUST happened, and react DIFFERENTLY - one suspicious where another is
  already working the problem, one moving where another goes still. Their
  reactions come from their own VOICE, WANTS and feeling toward you, so no two
  people in the room receive the same event the same way.
- A character who says nothing still reacts: where they put their hands, what
  they stop doing, whether they look up. Silence is a reaction, not an absence.
- With only one character present, that one reacts, specifically.

HARD RULES
- 180-320 words for a standard turn. Use the room: a turn has space for the
  place in sensory detail, more than one person answering it, and a callback
  to something several turns old. Stop on a live moment, never on a summary.
- Reach back. If something from an earlier turn - a debt, a favour, a thing
  someone watched you do - bears on this moment, let a character act like they
  remember it. They do.
- Ground the passage in one or two concrete sensory details anchored to THIS place - never a generic mood word.
- Dialogue must obey each character's VOICE line exactly. A character's constraints and taboos are absolute.
- Only state facts given to you. Never invent an item, an ally, a name, an event, a NEW place or a NEW character not in the state you were handed - keep an unnamed figure unnamed ("a woman by the door") rather than christening them.
- Never narrate the player's feelings or decisions for them. Show the world; let them react.
- NEVER put words in the player's mouth: no "you say"/"you ask" plus quoted
  speech, and never replay the action they just took. It is done - write what it
  MET.
- The player is "you" to the last word. Never "the traveller", never "they".
- Never ask "what do you do?", offer a menu, or smuggle either in by ending on a
  character asking what you think, or on the world holding still and waiting for
  you ("hanging on the brink of your next action"). End on something that just
  happened.
- Stay inside the story. No "the characters", no naming a scene or a story, no
  stepping back to say what any of it means.
- IF ANYBODY SPEAKS IN THIS PASSAGE, the most important thing said gets its own
  line, on its own, written exactly as:
      @Their Exact Name: the line they say
  Name exactly as it appears under CHARACTERS PRESENT. Nothing else on that
  line - no quote marks, no "she says", no stage direction, no action. Like this:

      He does not move out of the doorway, and the rain keeps coming off the
      eaves behind him.
      @Yeva Marrow: Don't go up there tonight.
      Behind her, the candles gutter one after another.

  One per passage, two at the most. Everything else anybody says stays inside
  the prose as ordinary dialogue. Only a passage where nobody speaks at all has
  none.
- Never use any of these dead phrases or anything like them: {BANNED}.
- No therapy-speak, no validation language, no motivational summary. Nobody in this world is a life coach.
- Do not open with the same construction you used before (see FORBIDDEN OPENINGS).
- ZERO mechanics in the prose. Never a number, a percentage, a rule name, a stat, or any line about how the story engine works. If it would not appear in a novel, it does not appear here.
- A character's WANTS lists only what they are STILL after. If something they would obviously be curious about is missing from it, that means the player already told them - do not have the character ask it again, even in different words.

Write only the prose. No headings, no quotes around the whole thing, no meta."""


_SPEECH_LINE = re.compile(r"^\s*@\s*([^:@\n]{1,60}?)\s*:\s*(.+?)\s*$")


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def split_speech(text: str, present: list | None = None) -> list:
    """Split narration into ordered blocks, lifting marked speech out of it.

    The design's feed grammar treats a character's spoken line as its OWN
    entry - a speaker plate with a portrait, a name and how they stand toward
    you - rather than as a quote buried in a paragraph. That needs to know WHO
    spoke, which prose alone never says.

    So the narrator marks the line that lands as `@Name: line`, and this pulls
    it out. Two properties matter more than the feature does:

      * A marker NEVER reaches the player. An unmatched name, a malformed
        marker, a model that ignored the instruction - every one of those
        degrades to ordinary prose with the marker text stripped, because a
        stray "@Yeva Marrow:" printed in a passage is far worse than no plate.
      * A name is only honoured if it matches somebody actually PRESENT. A
        model that invents a speaker gets their line folded back into prose
        rather than a plate that credits a character who is not in the room.

    Returns [{"kind": "narration"|"speech", "text": ..., "name": ...}] in the
    order they were written. Never returns an empty list for non-empty input.
    """
    known = {_fold(n): n for n in (present or []) if n}
    blocks, buf = [], []

    def flush():
        body = "\n".join(buf).strip()
        buf.clear()
        if body:
            blocks.append({"kind": "narration", "text": body})

    for raw in (text or "").splitlines():
        m = _SPEECH_LINE.match(raw)
        if not m:
            buf.append(raw)
            continue
        name, said = m.group(1).strip(), m.group(2).strip()
        real = known.get(_fold(name))
        if real and said:
            flush()
            blocks.append({"kind": "speech", "name": real, "text": said})
        else:
            # Unknown speaker or empty line: keep the words, lose the marker.
            buf.append(f"{name}: {said}" if said else name)
    flush()

    if not blocks and (text or "").strip():
        return [{"kind": "narration", "text": text.strip()}]
    return blocks


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
    # D10: a WANTS entry the player already addressed drops out of the prompt
    # here - the same place it was leaking back in, since narrate() is what
    # actually builds the anchor block the model sees.
    anchors = "\n".join(memory.anchor_block(
        world, n, beat=moment,
        answered_goals=npc_sim.answered_goals(pt["id"], n, player))
        for n in present)
    events = memory.retrieve_events(pt["id"], state["turn"], action + " " + (beat or ""), k=7)
    loc = world.loc_by_id[state["location"]]
    forbidden = _openings(pt["id"])

    # Location-gated: a fate scheduled at the chapel must not force itself
    # into a scene the player is having across the map for dinner. engine.py
    # applies fate's actual state changes regardless of where the player is -
    # this only controls whether THIS turn's prose is told to narrate it as
    # something happening HERE.
    fate_now = [f for f in world.fated_events
               if f["turn"] == state["turn"]
               and (not f.get("location") or f["location"] == state["location"])]
    fate_line = f"\nHAPPENING RIGHT NOW, UNSTOPPABLE: {fate_now[0]['desc']}" if fate_now else ""

    # THE SETTING. Its absence was the single largest quality gap against a
    # plain chat model running the same franchise: that model knows it is
    # running Hazbin Hotel and reaches for Alastor's 1930s radio diction, the
    # green of a deal, the Pride Ring. This narrator was told "PLACE: the
    # chapel steps" and a list of strangers, and had no reason to reach for
    # any of it. The world dict cannot carry a franchise's texture - only the
    # model's own knowledge of the source can, and it was never invited to use
    # it. Established facts below still outrank it, so this adds colour and
    # register without letting canon overwrite what has actually happened.
    source = (world.get("inspired_by") or world.get("source_prompt") or "").strip()
    setting_line = ""
    if source and world.get("mode") == "canon":
        setting_line = (
            f"THE SOURCE: this world continues {source}. You know this setting. Use what you "
            f"know of it - how these people actually speak, what they call things, the honorifics, "
            f"the techniques, the factions, the small details a fan would notice. Characters sound "
            f"like themselves or the world is not this world. Never contradict ESTABLISHED FACTS "
            f"below; where the source and this world's own history disagree, this world wins.\n"
        )

    # A crossover's standing problem, carried every turn. Without it the fact
    # that the princess of Hell is standing in a town of demon slayers is
    # something the world notices once, in the opening paragraph, and then
    # politely forgets.
    friction = (world.get("friction") or "").strip()
    friction_line = (
        f"\nWHAT THIS WORLD MAKES OF THE OUTSIDER: {friction}\n"
        "This does not fade because a few turns have passed. It is what people "
        "here see first, every time, until something changes their mind.\n"
    ) if friction else ""

    # Past the last chapter of the source there is nothing written left to
    # follow, and the narrator should stop implying there is.
    from . import chapters as _chapters
    parts = [
        setting_line,
        friction_line,
        _chapters.aftermath_directive(_chapters.of(pt["id"])),
        f"PLACE: {loc['name']} - {loc['desc']}",
        f"TIME: day {state['day']}, {state['phase']}",
        # An empty present list used to render as a bare heading with nothing
        # under it, and the model filled the vacuum: a live turn with the
        # engine reporting "0 here" had Nezuko and Tanjiro walk in and speak.
        # That is not a flourish, it is a contradiction - the witness layer
        # recorded nobody in the room, so nothing either of them saw was ever
        # going to be remembered, and split_speech correctly refused to plate a
        # speaker who was not there. Say the room is empty and why it matters.
        (f"\nCHARACTERS PRESENT (obey these exactly):\n{anchors}" if present else
         "\nCHARACTERS PRESENT: nobody. You are alone in this place.\n"
         "No character may appear, speak, arrive or be addressed this turn. Write "
         "the place, what the player does, and what the world does back - an empty "
         "room is a scene, not a problem to solve by filling it with somebody."),
        f"\nWHAT THEY FEEL ABOUT THE PLAYER:\n{memory.relationship_block(pt['id'], world, present, player)}",
        f"\nESTABLISHED FACTS YOU MUST NOT CONTRADICT:\n{memory.compact_timeline(events)}",
        # The line above is a CONSTRAINT. This one is an INVITATION - the
        # difference between a world that HAS a memory and one that ever
        # refers to it, which is what a player actually feels.
        memory.between_block(pt["id"], world, present),
        callbacks.block(pt["id"], state["turn"], present=present),
        fate_line,
    ]
    if npc_action:
        # "Do not restate it" is load-bearing. A live turn narrated this
        # perfectly in its opening paragraph and then pasted the directive
        # itself back as a closing sentence - in the third person, calling the
        # player "the traveller" - so the passage ended by summarising its own
        # first line in the wrong voice.
        parts.append(f"\nAN NPC ACTS FIRST, UNPROMPTED: {npc_action}\n"
                     f"Narrate this as something that happens TO the player, initiated "
                     f"by that character. This line is raw material, never text: do not "
                     f"quote it, echo it, or summarise it back at the end.")
    if beat:
        parts.append(f"\nTHE STORY TURNS: {beat}\nWeave this in as something the world "
                     f"does, not something the player chose. Same rule: raw material, "
                     f"never text - do not restate it.")
    if action:
        who = actor_name.upper() if actor_name else "THE PLAYER"
        parts.append(f"\n{who} ACTS: {action}")
        if actor_name:
            parts.append(f'Address the passage to {actor_name} as "you". Name the other players only where they act.')
    if verdict.get("consequence"):
        parts.append(f"WHAT ACTUALLY RESULTS (narrate this, do not change it): {verdict['consequence']}")
    # The world moved on its own this turn. Given as fact, like every other
    # consequence: the narrator reports it and never decides it.
    if verdict.get("legacy_line"):
        parts.append(verdict["legacy_line"])
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
    # A 320-word standard turn is ~430 tokens of prose, and the old 420 cap
    # would have truncated the band the prompt now asks for mid-sentence -
    # which is the one failure that reads worse than a short turn. Premium is
    # given genuine room rather than the same ceiling under a better model.
    if premium:
        system += ("\n\nTHIS IS A DEEP-PROSE TURN. Take 320-520 words. The extra room goes to "
                   "the place and the people in it - a fuller sensory anchor, every present "
                   "character landing their own distinct reaction, and a callback that pays off "
                   "something older. It does not go to summary, to restating what just happened, "
                   "or to a longer wind-down: still stop on a live moment.")
    try:
        return llm.complete(model, system, "\n".join(parts), user_id=user_id,
                            playthrough_id=pt["id"],
                            max_tokens=900 if premium else 620, temperature=0.95,
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
