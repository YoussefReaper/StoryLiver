"""Narrator - the only component that writes prose.

It sees persona anchors + compact retrieved state + the validated action. It
never sees the raw transcript, so there is no growing blob to drift inside.
Anti-repetition (Pain #4) is enforced two ways: a hard ban list of the
therapy-speak and purple-prose tics players complain about, and a rolling list
of the openings already used in this playthrough that must not recur.
"""
import re

from . import arcs, callbacks, canon_evidence, db, llm, memory, modes, npc_sim

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

THE LINE THAT LANDS
If anybody speaks, the most important thing said goes on its OWN line, exactly:
    @Name: what they say
Name as written under CHARACTERS PRESENT. Nothing else on that line - no quotes,
no "she says", no action. Example:

    He does not move out of the doorway, and the rain keeps coming off the eaves.
    @Yeva Marrow: Don't go up there tonight.
    Behind her the candles gutter, one after another.

One per passage, two at most; all other dialogue stays in the prose. This is not
optional: a passage where somebody speaks and no line is marked is wrong.

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
- Never use any of these dead phrases or anything like them: {BANNED}.
- No therapy-speak, no validation language, no motivational summary. Nobody in this world is a life coach.
- Do not open with the same construction you used before (see FORBIDDEN OPENINGS).
- ZERO mechanics in the prose. Never a number, a percentage, a rule name, a stat, or any line about how the story engine works. If it would not appear in a novel, it does not appear here.
- A character's WANTS lists only what they are STILL after. If something they would obviously be curious about is missing from it, that means the player already told them - do not have the character ask it again, even in different words.

Write only the prose. No headings, no quotes around the whole thing, no meta."""


# The `@` is optional. Given the rule, the model reliably puts the line on its
# own and writes "Giyu Tomioka: The courier notice is missing." - the shape
# exactly right, the sigil dropped - and requiring the sigil threw every one of
# those away. The real gate was never the punctuation: it is that the name
# before the colon belongs to somebody actually in the room, which a line of
# ordinary prose will not accidentally satisfy.
_SPEECH_LINE = re.compile(r"^\s*@?\s*([^:@\n]{1,60}?)\s*:\s*(.+?)\s*$")


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
    # Matched the way people actually name each other. Exact-only matching is
    # why plates almost never fired in live play: the cast is "Inosuke
    # Hashibira" and "Zenitsu Agatsuma", the narrator writes "@Inosuke:" and
    # "@Zenitsu:" because that is what everyone in the story calls them, and
    # every one of those lines was silently folded back into prose. Six
    # speaking moments across two turns produced zero plates.
    known = {}
    for n in (present or []):
        if not n:
            continue
        known[_fold(n)] = n
        # First name, and surname, when they are not already taken by somebody
        # else present - an ambiguous short name ("Kamado", with both Tanjiro
        # and Nezuko in the room) is left unmatched rather than credited to the
        # wrong character. Split on the ORIGINAL name: _fold removes spaces.
        for word in str(n).split():
            part = _fold(word)
            if len(part) < 3:
                continue
            if part in known and known[part] != n:
                known[part] = ""      # ambiguous: belongs to nobody
            else:
                known.setdefault(part, n)
    known = {k: v for k, v in known.items() if v}
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


def _spoken_before(pt_id, n=400):
    """Every line anybody has already said in this playthrough, folded.

    Unbounded by turn on purpose, unlike `_recent_speech`: an ordinary line can
    fairly be echoed later, but a character's ONE famous line is spent the
    moment it lands. Folding drops punctuation and case so a near-identical
    redelivery is still recognised as the same line.
    """
    rows = db.rows(
        "SELECT text FROM narrative WHERE playthrough_id=? AND kind='speech'"
        " ORDER BY id DESC LIMIT ?", (pt_id, n))
    return {_fold(r["text"]) for r in rows if (r["text"] or "").strip()}


def _recent_speech(pt_id, n=14, cap=10):
    """The lines characters have actually said lately, whole.

    Whole, not the first five words like `_openings`: a spoken line is short
    enough to repeat exactly, and it is the exact repeat that reads as broken.
    Trimmed to the most recent handful so an old line stops being forbidden
    once the scene has genuinely moved on - a catchphrase said twice an hour
    apart is characterisation; twice in two turns is a stuck record.
    """
    rows = db.rows(
        "SELECT text FROM narrative WHERE playthrough_id=? AND kind='speech'"
        " ORDER BY id DESC LIMIT ?", (pt_id, n))
    out = []
    for r in rows:
        line = " ".join((r["text"] or "").split())[:100]
        if line and line not in out:
            out.append(line)
    return out[:cap]


def _stub(world, pt, action, verdict, extra):
    """The offline narrator. No key, no spend - used by the whole test suite.

    It is not trying to be good prose, but it must not read as a TEMPLATE
    WITH A HOLE IN IT: an empty room produced "no one takes that in without
    hurrying", which is the stub showing the player its own placeholder. An
    empty room gets a sentence about the room instead.
    """
    r = llm.rng(pt["current_turn"], action)
    loc = world.loc_by_id[pt["current_location"]]
    who = verdict["state"]["present"]
    weather = r.choice(["Rain ticks on the shutters.", "The air tastes of iron.",
                        "Somewhere a dog will not stop.", "The light is going copper."])
    if who:
        reaction = (f"{world.npc_name(who[0])} takes that in without hurrying, "
                    "and gives you back exactly as much as it was worth.")
    else:
        reaction = r.choice([
            "Nobody is here to see it, which is its own kind of answer.",
            "The room keeps it. There is no one in it to carry it anywhere.",
            "It goes unwitnessed, and the quiet afterwards is longer than you expected.",
        ])
    return (f"{weather} {loc['name']} holds the sound of you the way a room holds a stranger. "
            f"{verdict.get('consequence') or action.strip()} "
            f"{reaction} {extra or ''}").strip()


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
    # A SIGNATURE LINE IS SPENT ONCE IT HAS BEEN SAID. The card offers it every
    # turn its beat is live, and the narrator is told in as many words to
    # deliver an offered canon line verbatim - so Gojo said "Throughout heaven
    # and earth, I alone am the honored one." three times in eight turns, which
    # is the soundboard the whole beat mechanism exists to prevent. Asking the
    # prompt nicely does not beat an explicit instruction; withholding the line
    # does. Cheap, deterministic, and it cannot be argued with.
    spent = _spoken_before(pt["id"])
    anchors = "\n".join(memory.anchor_block(
        world, n, beat=moment, spent=spent,
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

    # A source title identifies the work; it is not permission to improvise
    # facts from model memory. The saved entry contract is revision-linked and
    # quote-backed, and therefore the only canon authority a turn receives.
    source = (world.get("inspired_by") or world.get("source_prompt") or "").strip()
    setting_line = ""
    if source and world.get("mode") == "canon":
        contract = canon_evidence.brief(world.get("canon_entry") or {})
        setting_line = (
            f"THE SOURCE IDENTITY: this world continues {source}. The title identifies style "
            f"and vocabulary; it does NOT authorize facts from memory. Use only the saved "
            f"SOURCE-BACKED ENTRY CONTRACT, character anchors, and established play history "
            f"as factual canon. Never add a named person, place, power, relationship, death, "
            f"or past event merely because you recall it. When evidence is missing, keep the "
            f"scene local and uncertain rather than completing canon by guess.\n"
        )
        if contract:
            setting_line += "\n" + contract + "\n"
        else:
            setting_line += ("\nCANON EVIDENCE: unverified. Do not assert source-specific facts that are "
                             "not already present in the world state.\n")

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

    # WHERE IN THE SOURCE THIS STARTED. The cast list says who EXISTS; it says
    # nothing about who has MET whom, and the model fills that gap with the
    # settled version of the story every time. A build opened at the very
    # beginning of Demon Slayer - three correct people, right place - and had
    # all of them greet the player by name, know his business, and offer to
    # take the mountain apart with him, in a scene where nobody has met
    # anybody yet. At the beginning of a story, almost everyone is a stranger,
    # and the narrator has to be told so on every turn rather than once at
    # build time.
    early_line = ""
    if world.get("mode") == "canon" and str(world.get("entry_point") or "") == "start":
        early_line = (
            "\nTHIS IS THE VERY BEGINNING OF THE SOURCE. The people present have not met "
            "the player before, have not heard of him, and owe him nothing. Introduce them "
            "as strangers meeting a stranger: they size him up, they ask, they are wary or "
            "curious or dismissive. Do not have anyone greet him by name unless he gave it "
            "in this scene, do not let anyone already know what he wants or where he is "
            "from, and do not have a canon character treat him as an ally, a student, or a "
            "comrade they have not yet become. Anyone the source has not introduced by this "
            "point in its own story is not here yet - speak of them as rumour or not at all.\n"
        )

    # Past the last chapter of the source there is nothing written left to
    # follow, and the narrator should stop implying there is.
    from . import chapters as _chapters
    parts = [
        setting_line,
        friction_line,
        early_line,
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
        # "Dramatise, do not report" is load-bearing. Asked "I ask Zenitsu who
        # has been asking about me", a live turn answered:
        #
        #     "Zenitsu turns toward you and answers, telling you who has been
        #      asking about you in the quarter."
        #
        # - the consequence line rephrased, with the actual answer missing. The
        # single most important thing in the turn became a summary of itself.
        # If somebody answers a question, the passage contains what they SAID.
        parts.append(
            f"WHAT ACTUALLY RESULTS (narrate this, do not change it): "
            f"{verdict['consequence']}\n"
            f"Raw material, never text: play it out, do not restate it. If somebody "
            f"answers or explains, the passage contains what they SAY.")
    # The world moved on its own this turn. Given as fact, like every other
    # consequence: the narrator reports it and never decides it.
    if verdict.get("legacy_line"):
        parts.append(verdict["legacy_line"])
    if forbidden:
        parts.append("\nFORBIDDEN OPENINGS (do not begin with any of these constructions):\n" +
                     "\n".join("  - " + f for f in forbidden))
    # A LINE ALREADY SAID IS NOT AVAILABLE AGAIN. `_openings` reads narration,
    # beats and npc rows and takes the first five words of each - so it never
    # saw SPEECH at all, and a character could repeat a whole sentence verbatim
    # on consecutive turns with nothing to stop them. In a six-turn audit
    # Takuma Ino opened two turns running with "Let me put it another way." A
    # person who says the same sentence twice in two minutes is not a person.
    # SAYING IT AGAIN AND DOING IT AGAIN ARE ONE PROBLEM, so they are one
    # section - and a section that costs nothing on the turns where there is
    # nothing to repeat yet. `_openings` reads narration, beats and npc rows
    # and keeps five words of each, so it never saw SPEECH at all: in a
    # six-turn audit Takuma Ino opened two turns running with "Let me put it
    # another way." The same audit had one character hook his fingers under his
    # cap brim and count shoes in ten passages out of six turns, because
    # mannerisms arrive from the persona card on every single turn and the
    # narrator reaches for what it is handed. A card says what a person is
    # like; it does not say to perform it on a loop.
    said = _recent_speech(pt["id"])
    if said:
        parts.append(
            "\nALREADY USED (no one repeats a line or a paraphrase of one; at most "
            "ONE physical mannerism this passage, and not a recent one):\n"
            + "\n".join('  - "' + s + '"' for s in said))
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
