"""The stable identity block - the answer to persona drift.

The single loudest complaint against Character.AI and AI Dungeon is that the
character stops being the character. StoryLiver's existing defence is that the
anchor block is a constant, never a summary that decays. This module widens
that constant from 5 fields to the full card the blueprint specifies, and adds
the two framing tricks the research is explicit about:

  1. BECOME, don't describe. "You are X" invites the model to play a helpful
     assistant wearing X as a hat; author-gives-voice framing ("become X; the
     author has left the room") is measurably more resistant to fourth-wall
     breaks. The directive is written that way on purpose.

  2. SITUATION-KEYED CANON LINES. A famous line is not decoration - it is the
     proof the character is real. Each is stored with the beat it belongs to,
     and only the lines whose beat matches THIS moment are surfaced. Dumping
     every catchphrase every turn is what makes bots feel like soundboards;
     surfacing the one that fits is what makes them feel canon.

Everything here is string assembly. $0, deterministic, no model call.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# The card. Every field is optional - a world authored before this module
# existed simply has none of them, and gets exactly the block it always got.
# --------------------------------------------------------------------------

TEXT_FIELDS = ("concept", "voice", "backstory", "power_profile", "speech_constraints")
LIST_FIELDS = ("catchphrases", "famous_lines", "mannerisms", "values", "flaws",
               "secrets", "relationships_to_canon", "goals", "taboos", "constraints")

# Beats a famous line can be keyed to. The World Master tags the current moment
# with one of these; lines keyed to it become eligible.
BEATS = ("battle_start", "battle_turn", "victory", "defeat", "farewell", "reunion",
         "betrayal", "grief", "resolve", "greeting", "threat", "mercy", "sacrifice",
         # The two a curator reaches for before any of the others, and the two
         # the detector had no cue for. A line filed under `meeting` was
         # authored, stored, carried onto the world card, and then permanently
         # unreachable: `eligible_lines` withholds every line whose beat is not
         # the detected one, and nothing could ever detect `meeting`. `setback`
         # is the same story one beat over. Adding them here without adding
         # cues below would be the same bug wearing a seatbelt, so both landed
         # together and `test_a_famous_line_is_keyed_to_a_beat_that_actually_
         # happens` now fails if they ever drift apart again.
         "meeting", "setback")


def _lines(value):
    """Accept both a plain list of strings and the situation-keyed form
    [{"beat": "...", "line": "..."}] so a hand-authored world stays valid."""
    out = []
    for item in value or []:
        if isinstance(item, dict):
            line = (item.get("line") or "").strip()
            if line:
                out.append({"beat": (item.get("beat") or "").strip(), "line": line})
        elif str(item).strip():
            out.append({"beat": "", "line": str(item).strip()})
    return out


def normalise(card: dict) -> dict:
    """One shape, whatever the world author wrote."""
    card = card or {}
    out = {f: str(card.get(f) or "").strip() for f in TEXT_FIELDS}
    for f in LIST_FIELDS:
        raw = card.get(f) or []
        if f == "famous_lines":
            out[f] = _lines(raw)
        elif isinstance(raw, str):
            out[f] = [s.strip() for s in raw.split(";") if s.strip()]
        else:
            out[f] = [str(s).strip() for s in raw if str(s).strip()]
    out["name"] = str(card.get("name") or "").strip()
    out["role"] = str(card.get("role") or "").strip()
    return out


def _bullets(label: str, items, limit=6) -> str:
    items = [i for i in (items or []) if i][:limit]
    return f"  {label}: {'; '.join(items)}\n" if items else ""


def identity_block(card: dict, *, beat: str = "") -> str:
    """The block re-injected on EVERY call for this character.

    Stable by construction: same card + same beat always produces the same
    bytes, so this cannot be a source of drift. Only the eligible famous
    lines vary, and only with the beat the World Master detected."""
    c = normalise(card)
    head = c["name"] or "Unnamed"
    if c["role"]:
        head = f"{head} - {c['role']}"

    out = f"{head}\n"
    if c["concept"]:
        out += f"  CONCEPT: {c['concept']}\n"
    if c["voice"]:
        out += f"  VOICE: {c['voice']}\n"
    # Many of these are CONDITIONAL - "her horns only come out when she turns
    # lethal, otherwise they are hidden in her hair" - and a narrator that
    # treats them as a standing description puts the horns on show in every
    # scene, which is the opposite of the detail being right. Honour the
    # condition or leave the detail alone.
    out += _bullets("MANNERISMS (only when their condition is met)",
                    c["mannerisms"], 5)
    out += _bullets("VALUES", c["values"], 5)
    out += _bullets("FLAWS", c["flaws"], 5)
    out += _bullets("CONSTRAINTS", c["constraints"])
    out += _bullets("WANTS", c["goals"])
    out += _bullets("TIES", c["relationships_to_canon"], 6)
    if c["power_profile"]:
        out += f"  CAN: {c['power_profile']}\n"
    if c["backstory"]:
        out += f"  BEHIND THEM: {c['backstory']}\n"
    out += _bullets("NEVER", c["taboos"])

    # Secrets are the character's, not the player's. They belong in the block
    # because the character acts on them; they must never be narrated openly.
    out += _bullets("KNOWS BUT WILL NOT SAY", c["secrets"], 5)

    out += _bullets("SIGNATURE", c["catchphrases"], 4)
    eligible = eligible_lines(c, beat)
    if eligible:
        out += f"  CANON LINES FOR THIS MOMENT: {' | '.join(eligible)}\n"
    if c["speech_constraints"]:
        out += f"  SPEECH: {c['speech_constraints']}\n"
    return out.rstrip("\n")


def eligible_lines(card: dict, beat: str, limit=3):
    """Lines keyed to this beat, plus the standing refrain in the right context.

    A line keyed to a DIFFERENT beat is deliberately withheld: that is the whole
    mechanism. A soundboard says everything; a character says the right thing.

    Priority is explicit and ordered, because "available" was too blunt:
      1. the line filed to THIS beat    - it was written for this moment
      2. the standing refrain ("any")   - ALWAYS, but it is a refrain: it does
                                          not displace a line that was written
                                          for the moment, and on a keyed beat it
                                          is carried as register rather than as
                                          the thing to say
      3. the line filed to THIS beat only

    The old shape added the refrain everywhere at full strength, so at a burial
    a character delivered their catchphrase - the exact soundboard behaviour this
    field exists to prevent. Now the commonest turn (no beat at all) still gets
    the refrain and nothing else, which is what makes an ordinary turn feel like
    the character; and a keyed turn leads with the line written for it.
    """
    c = card if "famous_lines" in card and isinstance(card.get("famous_lines"), list) \
        and all(isinstance(x, dict) for x in card["famous_lines"]) else normalise(card)
    beat = (beat or "").strip().lower()
    keyed, refrain = [], []
    for entry in c.get("famous_lines") or []:
        key = (entry.get("beat") or "").strip().lower()
        if key and key != "any":
            if beat and key == beat:
                keyed.append(entry["line"])
        else:
            refrain.append(entry["line"])
    if keyed:
        return (keyed + refrain)[:limit]
    return refrain[:limit]


# Deterministic beat detection. Keyword-matched on purpose: asking a model
# "what kind of moment is this?" would be a seventh LLM caller and a per-turn
# cost, for a judgement a word list makes correctly almost every time. Ordered
# most-specific first, since a farewell during a battle is still a farewell.
#
# A cue is a STEM, not a sentence. The list used to store whole contractions -
# "she's dead" - which meant the beat fired only when a player happened to
# write exactly that, and "she is dead", "he was dead", "my master is dead" all
# returned nothing. The same hazard ran the other way on the apostrophe itself:
# a phone keyboard produces U+2019 and the cue held U+0027, so a pasted line
# stopped matching. normalise_beat_text() folds both before matching, and the
# cues below are written as the fragments that survive the folding.
BEAT_CUES = (
    ("sacrifice", ("sacrifice", "give my life", "take my place", "in my stead", "die for")),
    ("betrayal", ("betray", "double-cross", "turn on", "sold us", "traitor", "backstab")),
    ("farewell", ("goodbye", "farewell", "leave you", "last time", "part ways", "so long")),
    ("reunion", ("alive", "at last", "found you", "come back")),
    # `grief` is about a LOSS, and players report a loss without ever using
    # the word "dead": they ask who has lost someone, they say someone is gone,
    # they mention a grave or a name they carry. The audit caught "I ask who
    # here has lost someone" reading as an ordinary turn because the cue list
    # only knew the explicit death vocabulary. Widened to the forms people
    # actually type - "lost", "gone", "passed" - each still specific enough
    # that it is about a death and not about misplacing a key.
    ("grief", ("mourn", "griev", "is dead", "are dead", "was dead", "were dead",
               "'s dead", "'re dead", "death of", "buried", "bury", "funeral", "grave",
               "lost someone", "lost him", "lost her", "lost them", "lost my",
               "has lost", "have lost",
               "she's gone", "he's gone",
               "they're gone", "is gone", "passed away", "no longer with us",
               "never coming back", "i miss", "missing him", "missing her",
               "and i am sorry", "and i'm sorry", "i am sorry for your")),
    ("mercy", ("spare", "mercy", "let them live", "let him live", "let her live",
               "let it live", "put down the", "stand down", "lower the blade",
               "lower my blade", "lower my weapon", "sheathe", "walk away from",
               "do not kill", "don't kill", "i will not kill", "i won't kill")),
    ("victory", ("we won", "it is over", "it's over", "defeated", "you lose",
                 "finished them")),
    ("defeat", ("i lose", "we lost", "can not win", "can't win", "cannot win",
                "overwhelmed", "on my knees")),
    ("threat", ("threaten", "or else", "i will kill", "i'll kill", "one more step",
                "back away")),
    ("battle_start", ("attack", "draw my", "charge", "strike", "fight", "unsheath", "raise my")),
    # `resolve` is a DECLARATION OF INTENT - the moment a character states what
    # they are going to do and that nothing will turn them from it. The list
    # only held the "give up" family, so "I tell them what I intend to do and
    # that I will not be stopped" - the most direct resolve a player can type -
    # detected as nothing at all. Both halves of the beat are covered now: the
    # not-giving-up family AND the will-not-be-stopped family.
    ("resolve", ("will not give up", "won't give up", "never give up",
                 "stand my ground", "i refuse", "not today", "keep going",
                 "will not stop", "won't stop", "will not be stopped",
                 "won't be stopped", "cannot be stopped", "can't be stopped",
                 "nothing will stop", "no one will stop", "nothing can stop",
                 "i will not", "i won't", "i intend to", "i am going to",
                 "i'm going to", "mark my words", "whatever it takes",
                 "no matter what", "i swear", "i vow", "nothing is going to stop",
                 "i will do this", "i'll do this", "i will end this")),
    ("greeting", ("hello", "greet", "introduce myself", "well met", "who are you")),
    # `meeting` is the frame rather than the moment - arriving in front of
    # someone, being handed to them, being asked to account for yourself.
    ("meeting", ("meet ", "shake hands", "we have arrived", "i am new here",
                 "first time", "so we meet")),
    # `setback` is when the thing you were trying stops working. It is filed
    # before `greeting` deliberately: "I ask for help and nobody answers" is a
    # setback, not a hello.
    ("setback", ("no one helps", "nobody helps", "refuse to help", "turned away",
                 "did not work", "backfired", "failed", "we lost the", "lost it")),
)

# Apostrophes and dashes arrive in more than one codepoint and the engine never
# normalises them on the way in, so a cue written with the ASCII form silently
# misses the same word typed with the typographic one. Folded here, once, so no
# cue author has to think about it.
_FOLDABLE = {
    "\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ",
}


def normalise_beat_text(text: str) -> str:
    """Fold the characters a keyboard produces that a cue list does not hold."""
    out = []
    for ch in text or "":
        out.append(_FOLDABLE.get(ch, ch))
    return "".join(out).lower()


def detect_beat(*texts) -> str:
    """Which canonical moment this turn is, or "" for an ordinary one.

    Ordinary is the common case and it returns empty deliberately: unkeyed
    lines stay available, keyed ones stay holstered."""
    blob = normalise_beat_text(" ".join(t for t in texts if t))
    # The cue fragments are written against the folded form too, so a cue that
    # a curator types with a curly apostrophe cannot become dead text.
    if not blob:
        return ""
    for beat, cues in BEAT_CUES:
        if any(normalise_beat_text(cue) in blob for cue in cues):
            return beat
    return ""


BECOME = (
    "Become this character. You are not describing them and you are not an "
    "assistant playing them - the author has left the room and you speak with "
    "their voice directly. Stay inside their knowledge, their vocabulary and "
    "their limits. If a canon line above fits this exact moment, deliver it "
    "verbatim; that is the character, not an imitation. Never explain yourself, "
    "never mention being a model, never offer the player a menu of options."
)


def become_directive(card: dict) -> str:
    """The framing sentence that precedes the block on an NPC call."""
    name = normalise(card)["name"] or "this character"
    return BECOME.replace("this character", name, 1)


def card_completeness(card: dict) -> dict:
    """Drives the card editor's 'how canon does this feel' meter. Pure count,
    no judgement - it tells an author which fields are still empty, which is
    the actual thing that makes a character feel generic."""
    c = normalise(card)
    filled, missing = [], []
    for f in ("voice", "catchphrases", "famous_lines", "mannerisms", "values",
              "flaws", "goals", "taboos", "secrets", "backstory"):
        (filled if c.get(f) else missing).append(f)
    total = len(filled) + len(missing)
    return {"filled": filled, "missing": missing,
            "score": round(len(filled) / total, 3) if total else 0.0}
