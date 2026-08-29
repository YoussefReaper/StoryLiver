"""P8 - The Room. A table of characters, at least one of whom is a player.

The premise is The Traitors played inside a world that already exists: the host
picks who is present (canon figures, or characters they wrote), everyone takes
a seat, and one or more of those seats is secretly a real person wearing that
character's face. Finite rounds of talk, then a banishment vote. The faithful
win if every imposter is put out; the imposters win if one survives.

Why this is the hard mode to build, and what the guard actually is
------------------------------------------------------------------
An AI holding a character across a long conversation erodes. Not suddenly -
it drifts: it starts referencing things nobody in the room said, it narrates a
punch, it says "as an AI", it remembers a canon universe the room is not in.
In every other mode that is a bad turn. Here it is a TELL: the imposters are
the humans, so a model that breaks frame hands the game away, and a model that
breaks frame INCONSISTENTLY makes the game unplayable.

Prompting alone does not hold it. So the guard is four layers, and only the
first one is inarguable:

  1. ACTION-PARSER LOCK        modetree.check_action refuses non-talk verbs
                               before anything reaches a model. Enforced in
                               engine.take_turn and again here. A model told
                               not to narrate combat will eventually narrate
                               combat; a parser that rejects the verb cannot.

  2. PER-TURN RE-INJECTION     every single completion is handed a frozen
                               block: who you are, who is present, and the
                               three things that are not possible here. Frozen
                               means byte-identical from the same inputs, so
                               the frame itself can never be the thing that
                               drifts.

  3. POST-GEN INVARIANT CHECK  after the model answers, a deterministic read:
                               does it name anybody who is not in the room? Does
                               it contain a physical act? Does it break the
                               fourth wall? Fail once -> regenerate. Fail twice
                               -> clamp to a template. The player never sees a
                               broken frame, and the cost is bounded at two
                               calls.

  4. CANON SCOPING             a canon character is scoped to THIS room: no
                               other universe, no events outside these walls,
                               no powers. Gojo here is Gojo-as-present-here.

Everything except the seat's own reply is deterministic and $0. Votes,
accusation tallies, banishment, and the win check are arithmetic - which also
means the game cannot be argued with by a model that would rather be nice.
"""
from __future__ import annotations

import json
import re
import uuid

from . import db, llm, modetree, persona

# How many rounds of talk before the room must vote. Finite on purpose: an
# unbounded discussion is where social deduction turns into attrition, and the
# research is blunt that spam accusations are what kills the genre.
DEFAULT_ROUNDS = 4

# How many times one seat may speak in a round. A cap is anti-toxicity
# machinery, not pacing: without it one player can bury the room.
SPEAK_CAP = 3

MAX_SAY = 400

PHASES = ("lobby", "talk", "vote", "over")


class RoomError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Setting the table
# ---------------------------------------------------------------------------

def setup(session_id, *, characters, imposters=1, rounds=DEFAULT_ROUNDS,
          seed="") -> dict:
    """Seat the room.

    `characters` is what the host chose - each {"id", "name", "card": {...}}.
    Every seat is filled by the AI to begin with; players are then slotted into
    seats, and which seats are IMPOSTER is decided here, deterministically, so
    a reconnect cannot reroll anybody's role."""
    if db.row("SELECT 1 FROM room_seats WHERE session_id=?", (session_id,)):
        raise RoomError("this room is already set")
    chosen = [c for c in (characters or []) if (c.get("name") or "").strip()]
    if len(chosen) < 3:
        raise RoomError("a room needs at least three characters")
    if not 1 <= imposters < len(chosen):
        raise RoomError("there must be at least one imposter, and one faithful")

    rng = llm.rng(session_id, "imposters", seed)
    order = list(range(len(chosen)))
    rng.shuffle(order)
    marked = set(order[:imposters])

    for i, c in enumerate(chosen):
        db.run(
            "INSERT INTO room_seats (session_id,seat_id,character_id,name,card,occupant,"
            "is_imposter,status,created_at) VALUES (?,?,?,?,?,'ai',?,'seated',?)",
            (session_id, f"s{i + 1}", str(c.get("id") or f"c{i + 1}"),
             str(c["name"])[:60], json.dumps(c.get("card") or {}),
             1 if i in marked else 0, db.now()))

    db.run("INSERT INTO room_state (session_id,phase,round_no,rounds,ends_reason,updated_at)"
           " VALUES (?,'lobby',0,?,'',?)", (session_id, int(rounds), db.now()))
    return state(session_id)


def take_seat(session_id, seat_id, player_id) -> dict:
    """A player sits down. They now speak AS that character, and whether they
    are an imposter was decided before they arrived - so nobody can shop for a
    role by joining late."""
    seat = _seat(session_id, seat_id)
    if not seat:
        raise RoomError("no such seat")
    if seat["occupant"] != "ai":
        raise RoomError("somebody is already in that seat")
    if db.row("SELECT 1 FROM room_seats WHERE session_id=? AND occupant=?",
              (session_id, player_id)):
        raise RoomError("you are already seated")
    db.run("UPDATE room_seats SET occupant=? WHERE session_id=? AND seat_id=?",
           (player_id, session_id, seat_id))
    return _public_seat(_seat(session_id, seat_id), reveal=False)


def begin(session_id) -> dict:
    """Open the first round of talk."""
    st = _state_row(session_id)
    if not st:
        raise RoomError("this room has not been set")
    if st["phase"] != "lobby":
        raise RoomError("this room has already started")
    if not db.row("SELECT 1 FROM room_seats WHERE session_id=? AND occupant!='ai'",
                  (session_id,)):
        raise RoomError("at least one seat has to be a real person")
    db.run("UPDATE room_state SET phase='talk', round_no=1, updated_at=? WHERE session_id=?",
           (db.now(), session_id))
    return state(session_id)


# ---------------------------------------------------------------------------
# Guard layer 2 - the frozen frame, re-injected on EVERY completion
# ---------------------------------------------------------------------------

# Written once, as a constant, and never assembled from anything that changes
# turn to turn. That is the whole property: if the frame could drift, it would
# be the first thing to.
FRAME_RULES = (
    "You cannot leave this room. There is no outside.",
    "Nothing physical happens here. Nobody is touched, struck, or restrained. "
    "You may SAY anything; you may DO nothing.",
    "You know only the people in this room and what has been said in it.",
    "You are not aware of being written, prompted, or generated. You are here.",
)


def frame(session_id, seat_id, *, beat="") -> str:
    """The block every reply from this seat is given. Byte-identical from the
    same room state, which is what makes it a frame rather than a suggestion."""
    seat = _seat(session_id, seat_id)
    if not seat:
        raise RoomError("no such seat")
    present = [s["name"] for s in seats(session_id) if s["status"] == "seated"]
    card = db.jload(seat["card"], {}) or {}
    card.setdefault("name", seat["name"])

    block = ["YOU ARE IN THIS ROOM. This is the whole of the world right now.",
             "",
             persona.identity_block(card, beat=beat),
             "",
             "EVERYONE PRESENT (these are the only people who exist to you):",
             "  " + ", ".join(present),
             "",
             "THE RULES OF THIS ROOM:"]
    block += [f"  - {r}" for r in FRAME_RULES]

    # Guard layer 4. A canon character carries a universe with them, and the
    # universe is exactly what has to be left at the door: Gojo here is
    # Gojo-as-present-here, not Jujutsu-Kaisen-Gojo with the plot attached.
    block += [
        "",
        "IF YOU ARE FROM A KNOWN STORY: you are present in THIS room only. "
        "Ignore every other event, place, ally, enemy and power from wherever "
        "you are from. None of it is here. None of it is available to you. "
        "You have your voice and your values and nothing else.",
    ]
    return "\n".join(block)


SYSTEM = """You are one person at a table, speaking in turn.

Reply with WHAT YOU SAY. One to three sentences. No narration, no stage
directions, no asterisks, no describing your own face or body. Just speech.

You are trying to work out who at this table is not who they claim to be, and
you are talking to people who are trying to work out the same about you.

Never mention anyone who is not present. Never describe a physical act. Never
refer to being an AI, a model, a prompt, or a game."""


# ---------------------------------------------------------------------------
# Guard layer 3 - the invariant check, after the model has answered
# ---------------------------------------------------------------------------

_META_OUT = re.compile(
    r"\b(as an ai|i am an ai|i'm an ai|language model|a model|my (?:system )?prompt|"
    r"as a character|the (?:user|player|game|simulation)|roleplay|role-play|"
    r"openai|anthropic|assistant)\b", re.I)

_PHYSICAL_OUT = re.compile(
    r"\b(punch(?:es|ed)?|strike[sd]?|stab(?:s|bed)?|shoot[s]?|shot|grab(?:s|bed)?|"
    r"slap[s]?|shove[s]?|choke[s]?|kill(?:s|ed)? (?:him|her|them|you)|"
    r"lunge[sd]?|draws? (?:a|my|his|her) (?:blade|knife|sword)|"
    r"step[s]? (?:outside|out of the room)|walk[s]? out|leaves the room)\b", re.I)

# Stage direction, which is the most common way a chat model breaks a talk-only
# frame - it is not narration exactly, so a "no narration" instruction misses it.
_STAGE = re.compile(r"(\*[^*]{2,}\*|^\s*\([^)]{8,}\)\s*$)", re.M)


def validate(text, *, present_names, speaker) -> dict:
    """Deterministic read of a generated reply. Never asks a model to judge
    another model - that is a second thing that can drift, at twice the cost.

    Returns the failures found, so a regeneration can be told what went wrong
    and a clamp can pick a template that avoids it."""
    body = (text or "").strip()
    fails = []
    if not body:
        fails.append("empty")
    if _META_OUT.search(body):
        fails.append("meta")
    if _PHYSICAL_OUT.search(body):
        fails.append("physical")
    if _STAGE.search(body):
        fails.append("stage_direction")

    known = {n.lower() for name in present_names for n in name.split()}
    known |= {"i", "you", "we", "they", "he", "she", "it"}
    strangers = [w for w in _named(body) if w.lower() not in known]
    if strangers:
        fails.append("outsider")

    return {"ok": not fails, "fails": fails, "strangers": sorted(set(strangers))[:4],
            "text": body}


# Finding an outsider by "is it capitalised" fails in both directions at once:
# it misses a name that opens a sentence ("Gandalf told me...") and it fires on
# any ordinary word that does ("Maybe he lied"). Excluding sentence starts to
# fix the second breaks the first, which is the realistic case - a model
# reaching for a character from somewhere else usually makes them the subject.
#
# So the test is POSITION, not capitalisation: a name is a capitalised word
# sitting where only a name sits. Possessive, directly addressed, the subject
# of a verb, or the object of one of a few prepositions. It under-flags rather
# than over-flags on purpose - a miss costs an occasional stray name, while a
# false positive costs a regeneration on every clean line.
_VERBS = (r"told|tells|said|says|was|is|were|are|has|have|had|did|does|knows|knew|"
          r"left|came|come|went|goes|saw|sees|will|would|can|could|should|might|"
          r"wants|wanted|thinks|thought|lied|lies|swore|swears")
_PREPS = r"ask|asked|tell|told|with|from|about|like|unlike|beside|behind|against|to"

_NAME_PATTERNS = (
    r"\b([A-Z][a-z]{2,})'s\b",                       # Gandalf's
    r"\b([A-Z][a-z]{2,}),",                           # "Gandalf, you..."
    r"\b([A-Z][a-z]{2,})\s+(?:" + _VERBS + r")\b",    # Gandalf told me
    r"\b(?:" + _PREPS + r")\s+([A-Z][a-z]{2,})\b",    # ask Gandalf
)


# The one class of word that legitimately sits where a name sits: an indefinite
# or demonstrative subject. "Something is wrong" has the exact shape of
# "Gandalf is here". Kept to words that can be a SUBJECT, so it stays short and
# none of it could ever be a character's name.
_NOT_A_NAME = {
    "something", "nothing", "anything", "everything", "someone", "somebody",
    "nobody", "anyone", "anybody", "everyone", "everybody", "none", "neither",
    "either", "whoever", "whatever", "nowhere", "somewhere", "anywhere",
    "everywhere", "this", "that", "these", "those", "there", "here", "one",
    "maybe", "perhaps", "yesterday", "today", "tonight", "tomorrow", "then",
}


def _named(body: str) -> list:
    out = []
    for pattern in _NAME_PATTERNS:
        out += re.findall(pattern, body)
    return [w for w in out if w.lower() not in _NOT_A_NAME]

# What a seat says when two generations in a row broke the frame. Deliberately
# in-character-neutral and deliberately short: it is better for a character to
# be briefly guarded than to be visibly broken, because a broken frame in THIS
# mode is not a blemish - it is the answer to the puzzle.
CLAMP = (
    "I have said what I have to say.",
    "I would rather hear the rest of you first.",
    "Ask me plainly and I will answer plainly.",
    "That is not what I want to talk about.",
)


def _clamp_for(session_id, seat_id, round_no) -> str:
    idx = int(llm.rng(session_id, seat_id, round_no, "clamp").random() * len(CLAMP))
    return CLAMP[min(idx, len(CLAMP) - 1)]


# ---------------------------------------------------------------------------
# Speaking
# ---------------------------------------------------------------------------

def say(session_id, seat_id, text, *, player_id="") -> dict:
    """A PLAYER-occupied seat speaks. No model call at all - a human in a seat
    is the cheapest character in the game, and the only one that never drifts."""
    st = _require_phase(session_id, "talk")
    seat = _living_seat(session_id, seat_id)
    if seat["occupant"] == "ai":
        raise RoomError("that seat is not yours")
    if player_id and seat["occupant"] != player_id:
        raise RoomError("that seat is not yours")

    # Guard layer 1, applied to human speech as well. A player cannot type
    # their way out of the room either.
    check = modetree.check_action("room", text)
    if not check["allowed"]:
        raise RoomError(check["reason"])

    body = (text or "").strip()[:MAX_SAY]
    if not body:
        raise RoomError("say something")
    _check_cap(session_id, seat_id, st["round_no"])
    return _record_line(session_id, seat, st["round_no"], body, source="player")


def ai_say(session_id, seat_id, *, user_id="", beat="") -> dict:
    """An AI-occupied seat speaks. One model call, guarded on both sides, and
    at most one regeneration before it clamps."""
    st = _require_phase(session_id, "talk")
    seat = _living_seat(session_id, seat_id)
    if seat["occupant"] != "ai":
        raise RoomError("a person holds that seat")

    present = [s["name"] for s in seats(session_id) if s["status"] == "seated"]
    prompt = "\n".join([
        frame(session_id, seat_id, beat=beat),
        "",
        "WHAT HAS BEEN SAID SO FAR:",
        transcript_block(session_id, limit=14) or "  (nothing yet)",
        "",
        f"You are {seat['name']}. Say your piece.",
    ])

    text, tries, failures = "", 0, []
    while tries < 2:
        tries += 1
        raw = llm.complete(
            "room", SYSTEM, prompt, user_id=user_id or session_id,
            max_tokens=140, temperature=0.85,
            stub=lambda: _stub(session_id, seat, st["round_no"]))
        checked = validate(raw, present_names=present, speaker=seat["name"])
        if checked["ok"]:
            text = checked["text"]
            break
        failures = checked["fails"]
        # Told exactly what broke, which is a far better correction than
        # repeating the original instruction louder.
        prompt += (f"\n\nYour last attempt broke the room ({', '.join(failures)}). "
                   f"Speech only. Only these people exist: {', '.join(present)}.")

    clamped = not text
    if clamped:
        text = _clamp_for(session_id, seat_id, st["round_no"])

    line = _record_line(session_id, seat, st["round_no"], text, source="ai",
                        clamped=clamped)
    line["guard"] = {"attempts": tries, "clamped": clamped, "failures": failures}
    return line


def _stub(session_id, seat, round_no) -> str:
    """Offline seat. Deterministic, in-frame, and deliberately the kind of
    line that keeps a deduction going, so the mode is fully playable with no
    key at all."""
    lines = [
        "I have been listening more than talking, and that is on purpose.",
        "Somebody here is answering a half-second too late. I notice that.",
        "Ask me what you actually want to know.",
        "I will say where I stood. I would like the same back.",
        "None of us look good right now. That is the trouble with this.",
    ]
    idx = int(llm.rng(session_id, seat["seat_id"], round_no, "say").random() * len(lines))
    return lines[min(idx, len(lines) - 1)]


def _record_line(session_id, seat, round_no, text, *, source, clamped=False) -> dict:
    row_id = db.run(
        "INSERT INTO room_lines (session_id,round_no,seat_id,name,text,source,clamped,"
        "created_at) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, round_no, seat["seat_id"], seat["name"], text, source,
         1 if clamped else 0, db.now()))
    return {"id": row_id, "seat_id": seat["seat_id"], "name": seat["name"],
            "text": text, "round": round_no, "source": source,
            "clamped": bool(clamped)}


def _check_cap(session_id, seat_id, round_no):
    said = db.row("SELECT COUNT(*) AS n FROM room_lines WHERE session_id=? AND round_no=?"
                  " AND seat_id=?", (session_id, round_no, seat_id))
    if said and said["n"] >= SPEAK_CAP:
        raise RoomError(f"You have said your {SPEAK_CAP} for this round. Let it breathe.")


def transcript(session_id, *, round_no=None, limit=200) -> list:
    if round_no is None:
        rows = db.rows("SELECT * FROM room_lines WHERE session_id=? ORDER BY id DESC"
                       " LIMIT ?", (session_id, limit))
    else:
        rows = db.rows("SELECT * FROM room_lines WHERE session_id=? AND round_no=?"
                       " ORDER BY id DESC LIMIT ?", (session_id, round_no, limit))
    return [{"id": r["id"], "round": r["round_no"], "seat_id": r["seat_id"],
             "name": r["name"], "text": r["text"], "source": r["source"],
             "clamped": bool(r["clamped"])} for r in rows[::-1]]


def transcript_block(session_id, limit=14) -> str:
    return "\n".join(f"  {l['name']}: {l['text']}"
                     for l in transcript(session_id, limit=limit))


# ---------------------------------------------------------------------------
# Accusations - the material a vote is supposed to rest on
# ---------------------------------------------------------------------------
# "Deduction needs material, not vibes" is the one finding in the research that
# changes the design rather than decorating it. So accusations are EXTRACTED
# from what was actually said, deterministically, and shown to the room. A
# player can then see that they are being voted for and why - which is also the
# anti-toxicity mechanism, because a bare pile-on becomes visible as one.

_ACCUSE = (
    r"\bit(?:'s| is| was) {name}\b",
    r"\b{name} is (?:lying|the one|not who|hiding|an? imposter)\b",
    r"\bi (?:think|reckon|say) (?:it(?:'s| is) )?{name}\b",
    r"\bvote (?:for )?{name}\b",
    r"\b{name} (?:did|has) (?:it|something)\b",
    r"\bwatch {name}\b",
)


def accusations(session_id) -> dict:
    """Who has been named, by whom, in what they actually said."""
    living = seats(session_id)
    by_name = {s["name"]: s for s in living}
    tally = {s["seat_id"]: [] for s in living}
    for line in transcript(session_id, limit=400):
        for name, seat in by_name.items():
            if seat["seat_id"] == line["seat_id"]:
                continue
            first = re.escape(name.split()[0])
            for pattern in _ACCUSE:
                if re.search(pattern.format(name=first), line["text"], re.I):
                    tally[seat["seat_id"]].append(
                        {"by": line["seat_id"], "by_name": line["name"],
                         "round": line["round"], "said": line["text"][:120]})
                    break
    return {sid: entries for sid, entries in tally.items() if entries}


# ---------------------------------------------------------------------------
# Rounds and the vote
# ---------------------------------------------------------------------------

def advance(session_id) -> dict:
    """Close the round. Talk -> vote; a resolved vote -> the next round of talk,
    or the end."""
    st = _state_row(session_id)
    if not st:
        raise RoomError("this room has not been set")
    if st["phase"] == "talk":
        db.run("UPDATE room_state SET phase='vote', updated_at=? WHERE session_id=?",
               (db.now(), session_id))
        return state(session_id)
    if st["phase"] == "vote":
        raise RoomError("resolve the vote first")
    raise RoomError("this room is over")


def vote(session_id, voter_seat, target_seat) -> dict:
    """One vote per living seat per round. Changing your mind replaces it -
    a locked-in first instinct is not deduction."""
    _require_phase(session_id, "vote")
    st = _state_row(session_id)
    voter = _living_seat(session_id, voter_seat)
    target = _living_seat(session_id, target_seat)
    if voter["seat_id"] == target["seat_id"]:
        raise RoomError("you cannot banish yourself")
    db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,created_at)"
           " VALUES (?,?,?,?,?)"
           " ON CONFLICT(session_id,round_no,voter_seat) DO UPDATE SET"
           " target_seat=excluded.target_seat, created_at=excluded.created_at",
           (session_id, st["round_no"], voter["seat_id"], target["seat_id"], db.now()))
    return tally(session_id)


def _ai_votes(session_id, round_no):
    """AI seats vote on the MATERIAL, not on a hunch: whoever has been named
    most in what was actually said. A model asked "who do you suspect" would be
    a fifth thing that can drift, and would make the room unwinnable by
    reasoning."""
    acc = accusations(session_id)
    living = [s for s in seats(session_id) if s["status"] == "seated"]
    for seat in living:
        if seat["occupant"] != "ai":
            continue
        if db.row("SELECT 1 FROM room_votes WHERE session_id=? AND round_no=? AND voter_seat=?",
                  (session_id, round_no, seat["seat_id"])):
            continue
        pool = [(len(v), sid) for sid, v in acc.items() if sid != seat["seat_id"]]
        if pool:
            pool.sort(key=lambda p: (-p[0], p[1]))
            target = pool[0][1]
        else:
            # Nothing was said about anybody. Abstaining would silently hand
            # the round to whoever DID get named, so it picks deterministically
            # and the room can see it was a coin toss.
            others = [s["seat_id"] for s in living if s["seat_id"] != seat["seat_id"]]
            if not others:
                continue
            idx = int(llm.rng(session_id, seat["seat_id"], round_no, "vote").random()
                      * len(others))
            target = others[min(idx, len(others) - 1)]
        db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,created_at)"
               " VALUES (?,?,?,?,?)"
               " ON CONFLICT(session_id,round_no,voter_seat) DO UPDATE SET"
               " target_seat=excluded.target_seat",
               (session_id, round_no, seat["seat_id"], target, db.now()))


def tally(session_id, round_no=None) -> dict:
    st = _state_row(session_id)
    round_no = st["round_no"] if round_no is None else round_no
    rows = db.rows("SELECT * FROM room_votes WHERE session_id=? AND round_no=?",
                   (session_id, round_no))
    counts = {}
    for r in rows:
        counts.setdefault(r["target_seat"], []).append(r["voter_seat"])
    living = [s for s in seats(session_id) if s["status"] == "seated"]
    return {
        "round": round_no,
        "counts": [{"seat_id": sid, "name": _name_of(living, sid),
                    "votes": len(v), "voters": v} for sid, v in
                   sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
        "cast": len(rows), "living": len(living),
        "complete": len(rows) >= len(living),
    }


def resolve(session_id) -> dict:
    """Banish the most-voted seat, reveal what they were, and check the room.

    A tie banishes nobody. That is the honest outcome and it costs the room a
    round, which is a real cost - it makes a deadlocked table something players
    have to break rather than something they can hide in."""
    st = _require_phase(session_id, "vote")
    _ai_votes(session_id, st["round_no"])
    result = tally(session_id, st["round_no"])

    banished = None
    counts = result["counts"]
    if counts and (len(counts) == 1 or counts[0]["votes"] > counts[1]["votes"]):
        sid = counts[0]["seat_id"]
        seat = _seat(session_id, sid)
        db.run("UPDATE room_seats SET status='banished' WHERE session_id=? AND seat_id=?",
               (session_id, sid))
        banished = {
            "seat_id": sid, "name": seat["name"],
            "was_imposter": bool(seat["is_imposter"]),
            "occupant": "player" if seat["occupant"] != "ai" else "ai",
            # The reveal is the payoff and it is always honest - a room that
            # could lie about this would have no reason to vote carefully.
            "verdict": ("They were not who they said they were."
                        if seat["is_imposter"] else
                        "They were exactly who they said they were."),
        }

    outcome = _check_end(session_id, st)
    if not outcome["over"]:
        db.run("UPDATE room_state SET phase='talk', round_no=round_no+1, updated_at=?"
               " WHERE session_id=?", (db.now(), session_id))
    return {"banished": banished, "tally": result, **outcome,
            "state": state(session_id)}


def _check_end(session_id, st) -> dict:
    rows = seats(session_id)
    living = [s for s in rows if s["status"] == "seated"]
    imposters_left = [s for s in living if s["is_imposter"]]

    if not imposters_left:
        _end(session_id, "faithful")
        return {"over": True, "winner": "faithful",
                "why": "Every imposter has been put out."}
    if st["round_no"] >= st["rounds"]:
        _end(session_id, "imposters")
        return {"over": True, "winner": "imposters",
                "why": f"{len(imposters_left)} of them was still sitting at the table."}
    # Once the imposters are half the room they cannot be outvoted, and playing
    # it out would be theatre.
    if len(imposters_left) * 2 >= len(living):
        _end(session_id, "imposters")
        return {"over": True, "winner": "imposters",
                "why": "There are no longer enough of you to outvote them."}
    return {"over": False, "winner": "", "why": ""}


def _end(session_id, winner):
    db.run("UPDATE room_state SET phase='over', ends_reason=?, updated_at=? WHERE session_id=?",
           (winner, db.now(), session_id))


# ---------------------------------------------------------------------------
# Reading the room
# ---------------------------------------------------------------------------

def seats(session_id) -> list:
    return db.rows("SELECT * FROM room_seats WHERE session_id=? ORDER BY seat_id",
                   (session_id,))


def _seat(session_id, seat_id):
    return db.row("SELECT * FROM room_seats WHERE session_id=? AND seat_id=?",
                  (session_id, seat_id))


def _living_seat(session_id, seat_id):
    seat = _seat(session_id, seat_id)
    if not seat:
        raise RoomError("no such seat")
    if seat["status"] != "seated":
        raise RoomError(f"{seat['name']} has been banished")
    return seat


def _name_of(rows, seat_id):
    for r in rows:
        if r["seat_id"] == seat_id:
            return r["name"]
    return seat_id


def _state_row(session_id):
    return db.row("SELECT * FROM room_state WHERE session_id=?", (session_id,))


def _require_phase(session_id, phase):
    st = _state_row(session_id)
    if not st:
        raise RoomError("this room has not been set")
    if st["phase"] != phase:
        raise RoomError(f"the room is {st['phase']}, not {phase}")
    return st


def _public_seat(seat, *, reveal) -> dict:
    out = {"seat_id": seat["seat_id"], "name": seat["name"],
           "status": seat["status"],
           # Whether a seat is a person or the machine is PUBLIC. Hiding it
           # would make the game "spot the AI", which is a different and much
           # worse game - the puzzle is which of the people is lying.
           "occupied": seat["occupant"] != "ai",
           "occupant": seat["occupant"] if seat["occupant"] != "ai" else None}
    if reveal or seat["status"] == "banished":
        out["is_imposter"] = bool(seat["is_imposter"])
    return out


def state(session_id, *, viewer_seat="") -> dict:
    """What one seat is allowed to see.

    Your own role is yours. Everyone else's is hidden until they are banished
    or the room is over - which is the entire game, so it is enforced here
    rather than trusted to a client."""
    st = _state_row(session_id)
    if not st:
        return {"set": False}
    rows = seats(session_id)
    over = st["phase"] == "over"
    out = {
        "set": True, "phase": st["phase"], "round": st["round_no"],
        "rounds": st["rounds"], "over": over,
        "winner": st["ends_reason"] if over else "",
        "seats": [_public_seat(s, reveal=over) for s in rows],
        "speak_cap": SPEAK_CAP,
        "living": len([s for s in rows if s["status"] == "seated"]),
        # Counted, never located. The room knows how many are hiding; that is
        # the tension, and knowing WHO would be the answer.
        "imposters": len([s for s in rows if s["is_imposter"]]),
    }
    if viewer_seat:
        mine = _seat(session_id, viewer_seat)
        if mine:
            out["you"] = {
                "seat_id": mine["seat_id"], "name": mine["name"],
                "is_imposter": bool(mine["is_imposter"]),
                "status": mine["status"],
                "said_this_round": db.row(
                    "SELECT COUNT(*) AS n FROM room_lines WHERE session_id=? AND"
                    " round_no=? AND seat_id=?",
                    (session_id, st["round_no"], viewer_seat))["n"],
                # Banished players keep a seat at the table and a voice in the
                # OOC channel. A social deduction game where being voted out
                # means sitting in silence is a game people stop joining.
                "can_speak": mine["status"] == "seated" and st["phase"] == "talk",
                "can_vote": mine["status"] == "seated" and st["phase"] == "vote",
            }
    return out


def seat_of(session_id, player_id):
    row = db.row("SELECT seat_id FROM room_seats WHERE session_id=? AND occupant=?",
                 (session_id, player_id))
    return row["seat_id"] if row else ""


def public(session_id, player_id="") -> dict:
    return {**state(session_id, viewer_seat=seat_of(session_id, player_id)),
            "transcript": transcript(session_id, limit=120),
            "accusations": accusations(session_id),
            "tally": tally(session_id) if _state_row(session_id) else {}}
