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
         "betrayal", "grief", "resolve", "greeting", "threat", "mercy", "sacrifice")


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
    """Lines keyed to this beat, plus unkeyed ones (always available).

    A line keyed to a DIFFERENT beat is deliberately withheld: that is the
    whole mechanism. A soundboard says everything; a character says the right
    thing."""
    c = card if "famous_lines" in card and isinstance(card.get("famous_lines"), list) \
        and all(isinstance(x, dict) for x in card["famous_lines"]) else normalise(card)
    picked = []
    for entry in c.get("famous_lines") or []:
        key = entry.get("beat") or ""
        if not key or (beat and key == beat):
            picked.append(entry["line"])
    return picked[:limit]


# Deterministic beat detection. Keyword-matched on purpose: asking a model
# "what kind of moment is this?" would be a seventh LLM caller and a per-turn
# cost, for a judgement a word list makes correctly almost every time. Ordered
# most-specific first, since a farewell during a battle is still a farewell.
BEAT_CUES = (
    ("sacrifice", ("sacrifice", "give my life", "take my place", "in my stead", "die for")),
    ("betrayal", ("betray", "double-cross", "turn on", "sold us", "traitor", "backstab")),
    ("farewell", ("goodbye", "farewell", "leave you", "last time", "part ways", "so long")),
    ("reunion", ("you're alive", "youre alive", "at last", "found you", "come back")),
    ("grief", ("mourn", "grieve", "she's dead", "he's dead", "they're dead", "buried", "funeral")),
    ("mercy", ("spare", "mercy", "let them live", "put down the", "stand down")),
    ("victory", ("we won", "it's over", "defeated", "you lose", "finished them")),
    ("defeat", ("i lose", "we lost", "can't win", "overwhelmed", "on my knees")),
    ("threat", ("threaten", "or else", "i'll kill", "one more step", "back away")),
    ("battle_start", ("attack", "draw my", "charge", "strike", "fight", "unsheath", "raise my")),
    ("resolve", ("i won't give up", "never give up", "stand my ground", "i refuse", "not today")),
    ("greeting", ("hello", "greet", "introduce myself", "well met", "who are you")),
)


def detect_beat(*texts) -> str:
    """Which canonical moment this turn is, or "" for an ordinary one.

    Ordinary is the common case and it returns empty deliberately: unkeyed
    lines stay available, keyed ones stay holstered."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return ""
    for beat, cues in BEAT_CUES:
        if any(cue in blob for cue in cues):
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
