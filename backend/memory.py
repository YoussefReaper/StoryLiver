"""The structured memory core - StoryLiver's anti-collapse engine.

Three orthogonal stores, and the raw transcript is never one of them:

  (a) timeline_events  - append-only record of what happened
  (b) relationships    - player<->npc and npc<->npc scalar vectors
  (c) persona anchors  - constant, injected verbatim every turn, immune to rot

Full-version changes, both of which leave the retrieval scoring untouched:
  * Redis-backed hot state (write-through; SQLite stays the system of record
    and the export format).
  * Per-player NPC memory: a character's memory stream is keyed by
    (npc, player), so it remembers a different history of each person it met.
"""
from __future__ import annotations

import json
import math
import re

from . import db, persona, rt

STOP = set("""a an the and or but if then than that this these those of in on at to for with from by as is are was were
be been being it its it's he she they them his her their you your i me my we us our do does did not no so such into over
about after before while when where which who whom what how all any some there here up down out off again very can will
just now""".split())

REL_KEYS = ("affinity", "trust", "fear", "obligation")
REL_MIN, REL_MAX = -100.0, 100.0

SOLO = "user"      # the solo player's id; keeps single-player rows unchanged
SHARED = "*"       # a memory the character holds regardless of who is asking
# One full day (four phases). The people who were in the room when the player
# arrived are still in the room for the rest of that day - see npcs_at.
OPENING_TURNS = 4


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", (text or "").lower()) if len(w) > 2 and w not in STOP}


# ---------------------------------------------------------------------------
# Saying what somebody DID, rather than quoting what the player typed
# ---------------------------------------------------------------------------
#
# A witness record is read back in three places a player actually looks at -
# the rumour that reaches the next town, the "what you know" card, and the
# Chronicle - and every one of them was printing the raw input with a name
# stuck on the front:
#
#     A traveller: I look around and take stock of the room.
#
# That is a log line, not a thing anybody saw. Nobody who watched you do
# something reports it in your words, in your tense, addressed to you. This
# recasts the sentence the way the room would tell it: third person, past
# tense, the player named.
#
# It is deliberately mechanical. A model call per witnessed act would be one
# of the most expensive things in the engine and this fires on EVERY action;
# the transformation below is free, runs offline, and is right for the shapes
# players actually type ("I ask her who lit them", "I wait in the dark").
# Anything it cannot parse it leaves alone rather than mangling.

_IRREGULAR_PAST = {
    "am": "was", "are": "were", "is": "was", "have": "had", "has": "had",
    "do": "did", "does": "did", "go": "went", "say": "said", "tell": "told",
    "take": "took", "make": "made", "get": "got", "give": "gave", "come": "came",
    "see": "saw", "sit": "sat", "stand": "stood", "run": "ran", "hold": "held",
    "keep": "kept", "leave": "left", "let": "let", "put": "put", "read": "read",
    "hit": "hit", "cut": "cut", "set": "set", "let's": "let", "buy": "bought",
    "bring": "brought", "think": "thought", "catch": "caught", "teach": "taught",
    "fight": "fought", "seek": "sought", "draw": "drew", "throw": "threw",
    "know": "knew", "grow": "grew", "blow": "blew", "fly": "flew", "speak": "spoke",
    "break": "broke", "wake": "woke", "steal": "stole", "choose": "chose",
    "freeze": "froze", "write": "wrote", "ride": "rode", "drive": "drove",
    "rise": "rose", "eat": "ate", "fall": "fell", "feel": "felt", "find": "found",
    "hear": "heard", "lead": "led", "lose": "lost", "meet": "met", "pay": "paid",
    "sell": "sold", "send": "sent", "shoot": "shot", "sleep": "slept",
    "spend": "spent", "swear": "swore", "sweep": "swept", "swim": "swam",
    "win": "won", "wear": "wore", "lay": "laid", "lie": "lay", "hide": "hid",
    "bind": "bound", "build": "built", "burn": "burnt", "bend": "bent",
    "shut": "shut", "spread": "spread", "beat": "beat", "become": "became",
    "begin": "began", "bite": "bit", "deal": "dealt", "dig": "dug", "drink": "drank",
    "forget": "forgot", "forgive": "forgave", "kneel": "knelt", "mean": "meant",
    "ring": "rang", "shake": "shook", "shine": "shone", "sing": "sang",
    "sink": "sank", "slide": "slid", "strike": "struck", "swing": "swung",
    "tear": "tore", "understand": "understood", "wind": "wound",
}

# Words that are not the verb: an adverb or a modal can sit between "I" and the
# action ("I quietly ask her", "I will not move"). A modal is already tenseless
# in the past for our purposes - "I will wait" reported later is "would wait".
_MODALS = {"will": "would", "can": "could", "may": "might", "shall": "should",
           "must": "had to", "would": "would", "could": "could",
           "might": "might", "should": "should"}

_PRONOUNS = [
    (r"\bmyself\b", "themselves"), (r"\bmy\b", "their"), (r"\bmine\b", "theirs"),
    (r"\bme\b", "them"), (r"\bI'm\b", "they were"), (r"\bI've\b", "they had"),
    (r"\bI'll\b", "they would"), (r"\bI\b", "they"),
]

# Words that cannot be the verb of a clause. Used to decide whether the word
# after an "and" is a second action to conjugate ("looked around and TOOK
# stock") or the start of a noun phrase ("the room and THE door"). Leaving a
# bare verb in the present was the one thing the recast still got wrong.
_NOT_A_VERB = set("""the a an his her their its my your our this that these those it he she they we you there then
into onto in on at to for from with by of out up down over under across through back away not no nor all some any
both each every much many more most other another such same so very just only even still yet than as if when while
because since until before after about against between during without within along around behind beneath beside
one two three four five six seven eight nine ten
who whom whose what which where why how whether""".split())

# Reported speech. "I say I am looking for work" swaps the second pronoun to
# "they" and leaves the verb agreeing with the first person: "said they am".
# The embedded clause has to shift tense with the reporting verb, which is
# exactly what these pairs do. Applied after the pronoun swap, so it also
# catches "they is" produced by a third-person subject earlier in the line.
_REPORTED = [
    (r"\bthey (?:am|are|is)\b", "they were"),
    (r"\bthey (?:have|has)\b", "they had"),
    (r"\bthey don't\b", "they did not"), (r"\bthey do\b", "they did"),
    (r"\bthey will\b", "they would"), (r"\bthey can\b", "they could"),
    (r"\bthey shall\b", "they should"), (r"\bthey may\b", "they might"),
]


# Multi-syllable verbs that double their final consonant anyway, because the
# stress falls on the last syllable (or because British spelling says so).
# Without these the rule below would give "prefered" and "traveled".
_DOUBLERS = {"prefer", "refer", "defer", "occur", "permit", "admit", "commit",
             "submit", "omit", "regret", "forget", "begin", "control",
             "patrol", "travel", "cancel", "signal", "label", "level",
             "marvel", "quarrel", "shovel", "unravel", "equip"}


def _syllables(word: str) -> int:
    """Vowel groups. A crude count, and all this needs it for is one/many."""
    return len(re.findall(r"[aeiouy]+", word)) or 1


def _past(verb: str) -> str:
    """Best-effort past tense of a bare present-tense verb."""
    low = verb.lower()
    if low in _IRREGULAR_PAST:
        return _IRREGULAR_PAST[low]
    if low.endswith("e"):
        return low + "d"
    if len(low) > 2 and low.endswith("y") and low[-2] not in "aeiou":
        return low[:-1] + "ied"
    # Consonant doubling needs the final syllable to be the STRESSED one, so
    # "stop" doubles and "open" does not. Syllable count is the cheap proxy:
    # one syllable is always stressed on itself.
    doubles = (len(low) > 2 and low[-1] not in "aeiouwxy"
               and low[-2] in "aeiou" and low[-3] not in "aeiou"
               and (_syllables(low) == 1 or low in _DOUBLERS))
    return low + low[-1] + "ed" if doubles else low + "ed"


def _and_clauses(words: list[str]) -> list[str]:
    """Put the SECOND verb of "I looked around and take stock" into the past too.

    Only where the word after "and"/"then" can actually be a verb: "the room
    and the door" must be left alone, and a determiner is the reliable tell.
    """
    out = list(words)
    for i, w in enumerate(out[:-1]):
        if w.lower().strip(",") not in ("and", "then"):
            continue
        nxt = out[i + 1]
        low = nxt.lower().strip(",.")
        if not low or low in _NOT_A_VERB or not low.isalpha():
            continue
        punct = nxt[len(nxt.rstrip(",.")):]
        out[i + 1] = _past(low) + punct
    return out


def retell(action: str, who: str = "") -> str:
    """Recast a player's first-person action as a witness would report it.

    "I ask her who lit them"  ->  "Ilsabet Marr asked her who lit them"
    "I wait in the dark"      ->  "Ilsabet Marr waited in the dark"

    Returns the original text unchanged if it is not first person - a player
    who already wrote in the third person, or a rumour that came from
    somewhere else, is left exactly as it is.
    """
    text = (action or "").strip()
    if not text:
        return ""
    name = (who or "").strip() or "A traveller"

    m = re.match(r"^\s*I\s+(.*)$", text)
    if not m:
        # Not first person. Still swap any stray "my"/"me" so a mixed sentence
        # does not address the reader, and hand it back.
        return text
    rest = m.group(1)

    # Skip any adverbs that sit between the pronoun and the verb, so
    # "I quietly ask" conjugates `ask` and not `quietly`.
    lead: list[str] = []
    words = rest.split()
    while words and (words[0].lower().endswith("ly") or words[0].lower() in ("just", "then")):
        lead.append(words.pop(0))
    if not words:
        return f"{name} {rest}".strip()

    first = words[0]
    low = first.lower().strip(",.")
    if low in ("do", "don't", "dont"):
        # "I don't move" -> "did not move"
        tail = " ".join(words[1:])
        neg = "did not" if low != "do" else "did"
        body = " ".join(lead + [neg, tail]).strip()
    elif low in _MODALS:
        body = " ".join(lead + [_MODALS[low]] + words[1:])
    else:
        punct = first[len(first.rstrip(",.")):]
        body = " ".join(lead + [_past(low) + punct] + _and_clauses(words[1:]))

    out = f"{name} {body}".strip()
    for pattern, repl in _PRONOUNS:
        out = re.sub(pattern, repl, out)
    for pattern, repl in _REPORTED:
        out = re.sub(pattern, repl, out)
    return out


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def clamp(v: float) -> float:
    return max(REL_MIN, min(REL_MAX, float(v)))


# --------------------------------------------------------------------------
# (c) persona anchors - constant blocks, never summarised, never trimmed
# --------------------------------------------------------------------------

def anchor_block(world, npc_id: str, *, beat: str = "", answered_goals=None,
                 spent=()) -> str:
    """The constant re-injected every call. Never a summary, never decayed.

    A character authored with only the original five fields produces exactly
    the bytes it always did - that path is untouched, which is what keeps the
    anti-collapse proof meaningful. A character carrying the wider identity
    card (catchphrases, famous lines, mannerisms, values, flaws, secrets)
    gets the full block instead, assembled by persona.py and equally stable.

    `answered_goals` (D10) drops a WANTS entry the player has already
    addressed - a goal that reads as "find out X" from a character who was
    already told X is not a want any more, it is a closed loop, and leaving
    it in the block is exactly what made the dialogue path re-ask it."""
    npc = world.by_id[npc_id]
    a = npc["anchors"]
    goals = [g for g in a["goals"] if g not in (answered_goals or ())]
    if any(a.get(f) for f in persona.LIST_FIELDS + persona.TEXT_FIELDS
           if f not in ("constraints", "goals", "taboos", "voice")):
        card = dict(a, goals=goals)
        return persona.identity_block(card, beat=beat, spent=spent)
    return (
        f"{a['name']} - {a['role']}\n"
        f"  VOICE: {a['voice']}\n"
        f"  CONSTRAINTS: {'; '.join(a['constraints'])}\n"
        f"  WANTS: {'; '.join(goals)}\n"
        f"  NEVER: {'; '.join(a['taboos'])}"
    )


# --------------------------------------------------------------------------
# (a) timeline events
# --------------------------------------------------------------------------

def add_event(pt_id, turn, actor, action, consequence, *, rule_ref=None,
              kind="action", importance=3, location=None):
    row_id = db.run(
        "INSERT INTO timeline_events (playthrough_id,turn,actor,action,consequence,rule_ref,kind,importance,location,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pt_id, turn, actor, action, consequence, rule_ref, kind, int(importance), location, db.now()),
    )
    rt.cache_drop(f"sl:pt:{pt_id}:events", f"sl:pt:{pt_id}:snapshot")
    return row_id


def timeline(pt_id, limit=500):
    return db.rows(
        "SELECT * FROM timeline_events WHERE playthrough_id=? ORDER BY id DESC LIMIT ?",
        (pt_id, limit),
    )[::-1]


def _event_pool(pt_id):
    """The retrieval candidate set. Cached hot; SQLite remains the truth."""
    key = f"sl:pt:{pt_id}:events"
    cached = rt.cache_get(key)
    if cached is not None:
        return cached
    pool = db.rows(
        "SELECT id,turn,actor,action,consequence,rule_ref,kind,importance,location"
        " FROM timeline_events WHERE playthrough_id=? ORDER BY id DESC LIMIT 400",
        (pt_id,),
    )
    rt.cache_set(key, pool)
    return pool


def retrieve_events(pt_id, current_turn, query, k=8):
    """Generative-Agents scoring adapted to turns:
    score = recency(0.98^turns_ago) + importance/5 + relevance(token overlap).
    Cheap, deterministic, no embedding call. Unchanged from the MVP."""
    all_events = _event_pool(pt_id)
    if not all_events:
        return []
    q = _tokens(query)
    scored = []
    for e in all_events:
        ago = max(0, current_turn - e["turn"])
        recency = 0.98 ** ago
        importance = e["importance"] / 5.0
        relevance = _overlap(q, _tokens(f"{e['actor']} {e['action']} {e['consequence']}"))
        # Fate and rejections stay retrievable forever - they are load-bearing.
        floor = 0.55 if e["kind"] in ("fate", "rejection") else 0.0
        scored.append((max(floor, recency + importance + relevance * 1.4), e))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    top = [e for _, e in scored[:k]]
    top.sort(key=lambda e: e["id"])
    return top


def compact_timeline(events) -> str:
    if not events:
        return "  (nothing has happened yet)"
    out = []
    for e in events:
        tag = {"fate": "FATE", "rejection": "REFUSED", "npc": "NPC", "beat": "BEAT"}.get(e["kind"], "")
        prefix = f"[{tag}] " if tag else ""
        out.append(f"  T{e['turn']} {prefix}{e['actor']}: {e['action']} -> {e['consequence']}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# (b) relationship vectors
# --------------------------------------------------------------------------

def seed(pt_id, world, player=SOLO):
    for npc in world.npcs:
        r = npc["initial_relationship"]
        db.run(
            "INSERT OR IGNORE INTO relationships (playthrough_id,src,dst,affinity,trust,fear,obligation,last_interaction_turn)"
            " VALUES (?,?,?,?,?,?,?,-1)",
            (pt_id, player, npc["id"], r["affinity"], r["trust"], r["fear"], r["obligation"]),
        )
        db.run(
            "INSERT OR IGNORE INTO npc_state (playthrough_id,npc_id,location,plan,reflections,alive)"
            " VALUES (?,?,?,'[]','[]',1)",
            (pt_id, npc["id"], npc["start_location"]),
        )
        for seed_text in npc["seed_memories"]:
            db.run(
                "INSERT INTO npc_memories (playthrough_id,npc_id,player_id,turn,kind,text,importance,last_access_turn,created_at)"
                " VALUES (?,?,?,0,'seed',?,5,0,?)",
                (pt_id, npc["id"], SHARED, seed_text, db.now()),
            )
    for src, dst, vals in world.get("npc_edges", []) or []:
        db.run(
            "INSERT OR IGNORE INTO relationships (playthrough_id,src,dst,affinity,trust,fear,obligation,last_interaction_turn)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (pt_id, src, dst, *vals),
        )
    rt.invalidate_playthrough(pt_id)


def seed_player(pt_id, world, player):
    """A player joining a running session gets their own relationship row set."""
    for npc in world.npcs:
        r = npc["initial_relationship"]
        db.run(
            "INSERT OR IGNORE INTO relationships (playthrough_id,src,dst,affinity,trust,fear,obligation,last_interaction_turn)"
            " VALUES (?,?,?,?,?,?,?,-1)",
            (pt_id, player, npc["id"], r["affinity"], r["trust"], r["fear"], r["obligation"]),
        )
    rt.cache_drop(f"sl:pt:{pt_id}:rels")


def _rel_map(pt_id):
    key = f"sl:pt:{pt_id}:rels"
    cached = rt.cache_get(key)
    if cached is not None:
        return cached
    rows = db.rows("SELECT * FROM relationships WHERE playthrough_id=?", (pt_id,))
    out: dict = {}
    for r in rows:
        out.setdefault(r["src"], {})[r["dst"]] = r
    rt.cache_set(key, out)
    return out


def relationships(pt_id, src=SOLO):
    return list(_rel_map(pt_id).get(src, {}).values())


def rel_to(pt_id, npc_id, src=SOLO):
    return _rel_map(pt_id).get(src, {}).get(npc_id)


def apply_deltas(pt_id, deltas, turn, player=SOLO):
    """WM-authored relationship changes. Each delta is bounded so no single
    turn can whiplash a relationship - drift has to be earned over turns."""
    applied = []
    for d in deltas or []:
        src = d.get("src") or player
        dst = d.get("npc") or d.get("dst")
        if not dst:
            continue
        cur = rel_to(pt_id, dst, src)
        if not cur:
            db.run("INSERT OR IGNORE INTO relationships (playthrough_id,src,dst) VALUES (?,?,?)",
                   (pt_id, src, dst))
            rt.cache_drop(f"sl:pt:{pt_id}:rels")
            cur = rel_to(pt_id, dst, src)
            if not cur:
                continue
        new = {}
        for k in REL_KEYS:
            try:
                step = float(d.get(k, 0) or 0)
            except (TypeError, ValueError):
                step = 0.0
            step = max(-25.0, min(25.0, step))
            new[k] = clamp(cur[k] + step)
        db.run(
            "UPDATE relationships SET affinity=?,trust=?,fear=?,obligation=?,last_interaction_turn=?"
            " WHERE playthrough_id=? AND src=? AND dst=?",
            (new["affinity"], new["trust"], new["fear"], new["obligation"], turn, pt_id, src, dst),
        )
        applied.append({"src": src, "npc": dst, **new, "note": d.get("note", "")})
    if applied:
        rt.cache_drop(f"sl:pt:{pt_id}:rels", f"sl:pt:{pt_id}:snapshot")
    return applied


def relationship_block(pt_id, world, npc_ids=None, player=SOLO) -> str:
    rels = relationships(pt_id, player)
    if npc_ids:
        rels = [r for r in rels if r["dst"] in npc_ids]
    rels.sort(key=lambda r: -(abs(r["affinity"]) + abs(r["trust"]) + abs(r["fear"]) + abs(r["obligation"])))
    lines = []
    for r in rels[:8]:
        seen = "never met" if r["last_interaction_turn"] < 0 else f"last T{r['last_interaction_turn']}"
        lines.append(
            f"  {world.npc_name(r['dst'])}: affinity {r['affinity']:+.0f}, trust {r['trust']:+.0f}, "
            f"fear {r['fear']:+.0f}, obligation {r['obligation']:+.0f} ({seen})"
        )
    return "\n".join(lines) if lines else "  (no one has an opinion of you yet)"


def between_block(pt_id, world, npc_ids) -> str:
    """What the people in this room think OF EACH OTHER.

    The world seeds npc_edges (Nessa distrusts Corvin, Tamsin resents her
    mother) and then only ever used them to prime the relationship table. The
    narrator was never told, so two characters who cannot stand each other
    stood in the same room being uniformly pleasant.

    Companion banter is repeatedly cited as what makes a party feel populated
    rather than staged - it costs nothing here, because the feelings already
    exist and only had to be shown."""
    ids = set(npc_ids or [])
    if len(ids) < 2:
        return ""
    rows = db.rows(
        "SELECT src, dst, affinity, trust, fear FROM relationships"
        " WHERE playthrough_id=? AND src!=? AND dst!=?", (pt_id, SOLO, SOLO))
    lines = []
    for r in rows:
        if r["src"] not in ids or r["dst"] not in ids or r["src"] == r["dst"]:
            continue
        # Only tensions worth writing. Mild mutual indifference is not a scene.
        strength = abs(r["affinity"]) + abs(r["trust"]) + abs(r["fear"])
        if strength < 35:
            continue
        if r["affinity"] <= -20 or r["trust"] <= -25:
            feel = "cannot stand" if r["affinity"] <= -35 else "does not trust"
        elif r["affinity"] >= 35:
            feel = "is close to"
        elif r["fear"] >= 25:
            feel = "is wary of"
        else:
            continue
        lines.append((strength, f"  {world.npc_name(r['src'])} {feel} "
                                f"{world.npc_name(r['dst'])}."))
    if not lines:
        return ""
    lines.sort(key=lambda x: -x[0])
    # Terse on purpose: this is additive context, and the flat-context
    # guarantee (a 4-player prompt must not exceed the single-player ceiling)
    # matters more than a well-phrased instruction.
    return ("\nBETWEEN THEM:\n" + "\n".join(t for _, t in lines[:3])
            + "\n  They may speak to each other, not only to you.")


# --------------------------------------------------------------------------
# NPC minds - memory stream, retrieval, reflections, plans
# --------------------------------------------------------------------------

def npc_observe(pt_id, npc_id, turn, text, importance=3, kind="observation", player=SHARED):
    db.run(
        "INSERT INTO npc_memories (playthrough_id,npc_id,player_id,turn,kind,text,importance,last_access_turn,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (pt_id, npc_id, player, turn, kind, text, int(importance), turn, db.now()),
    )


def npc_recall(pt_id, npc_id, turn, query, k=6, player=SOLO):
    """This character's memories OF THIS PLAYER, plus what they know regardless.
    Scoring is byte-identical to the single-player engine."""
    mems = db.rows(
        "SELECT * FROM npc_memories WHERE playthrough_id=? AND npc_id=? AND player_id IN (?,?)"
        " ORDER BY id DESC LIMIT 200",
        (pt_id, npc_id, player, SHARED),
    )
    if not mems:
        return []
    q = _tokens(query)
    scored = []
    for m in mems:
        ago = max(0, turn - m["last_access_turn"])
        recency = 0.98 ** ago
        importance = m["importance"] / 5.0
        relevance = _overlap(q, _tokens(m["text"]))
        bonus = 0.6 if m["kind"] in ("seed", "reflection") else 0.0
        scored.append((recency + importance + relevance * 1.4 + bonus, m))
    scored.sort(key=lambda x: -x[0])
    top = [m for _, m in scored[:k]]
    if top:
        db.run(
            "UPDATE npc_memories SET last_access_turn=? WHERE id IN (%s)" % ",".join("?" * len(top)),
            (turn, *[m["id"] for m in top]),
        )
    return top


def npc_state(pt_id, npc_id):
    return db.row("SELECT * FROM npc_state WHERE playthrough_id=? AND npc_id=?", (pt_id, npc_id))


def all_npc_states(pt_id):
    key = f"sl:pt:{pt_id}:npcstate"
    cached = rt.cache_get(key)
    if cached is not None:
        return cached
    rows = db.rows("SELECT * FROM npc_state WHERE playthrough_id=?", (pt_id,))
    rt.cache_set(key, rows)
    return rows


def npc_player_state(pt_id, npc_id, player):
    row = db.row(
        "SELECT * FROM npc_player WHERE playthrough_id=? AND npc_id=? AND player_id=?",
        (pt_id, npc_id, player))
    if row:
        return row
    db.run(
        "INSERT OR IGNORE INTO npc_player (playthrough_id,npc_id,player_id) VALUES (?,?,?)",
        (pt_id, npc_id, player))
    return db.row(
        "SELECT * FROM npc_player WHERE playthrough_id=? AND npc_id=? AND player_id=?",
        (pt_id, npc_id, player))


def set_npc_player_state(pt_id, npc_id, player, **fields):
    if not fields:
        return
    npc_player_state(pt_id, npc_id, player)
    cols = ", ".join(f"{k}=?" for k in fields)
    db.run(
        f"UPDATE npc_player SET {cols} WHERE playthrough_id=? AND npc_id=? AND player_id=?",
        (*fields.values(), pt_id, npc_id, player))


def set_npc_location(pt_id, npc_id, location):
    db.run("UPDATE npc_state SET location=? WHERE playthrough_id=? AND npc_id=?",
           (location, pt_id, npc_id))
    rt.cache_drop(f"sl:pt:{pt_id}:npcstate")


def kill_npc(pt_id, npc_id):
    db.run("UPDATE npc_state SET alive=0 WHERE playthrough_id=? AND npc_id=?", (pt_id, npc_id))
    rt.cache_drop(f"sl:pt:{pt_id}:npcstate", f"sl:pt:{pt_id}:snapshot")


def hold_in_scene(pt_id, npc_ids, turn):
    """Keep somebody in the room for a beat because they are mid-conversation.

    Position is schedule-driven, and the schedule does not care that the player
    just asked a question: npcs_at drifts everyone back unless the SIM moved
    them, so a character the PLAYER engaged was never pinned. Played live, this
    reads as the world walking out on you - ask Giyu what the notice accuses you
    of, get a straight answer, ask the obvious follow-up, and the reply is that
    there is "no quiet blue haori in the crowd to answer". Twice in four turns.

    Marking them as having acted this turn buys exactly one more turn before the
    schedule reclaims them, which is a conversation rather than a residency."""
    for npc_id in {i for i in (npc_ids or []) if i}:
        db.run("UPDATE npc_state SET last_act_turn=? WHERE playthrough_id=? AND npc_id=?",
               (turn, pt_id, npc_id))
    if npc_ids:
        rt.cache_drop(f"sl:pt:{pt_id}:npcstate")


def move_companions(pt_id, world, location, turn):
    """A companion goes where the player goes.

    "I and my girlfriend charlie" is a statement about who is travelling, and
    she was being left behind: the player walked out to the Butterfly Mansion
    and on into the next chapter, and Charlie stayed in the ward they started
    in, because position is schedule-driven and her schedule was written for a
    town she was no longer in. The whole premise of the run was a person who
    was not in the room for any of it.

    Held on arrival the same way a conversation is held, so the schedule does
    not reclaim them the moment they get there."""
    moved = []
    for npc in world.npcs:
        if not npc.get("companion"):
            continue
        st = db.row("SELECT location, alive FROM npc_state WHERE playthrough_id=? AND npc_id=?",
                    (pt_id, npc["id"]))
        if not st or not st["alive"] or st["location"] == location:
            continue
        db.run("UPDATE npc_state SET location=?, last_act_turn=? "
               "WHERE playthrough_id=? AND npc_id=?",
               (location, turn, pt_id, npc["id"]))
        moved.append(npc["id"])
    if moved:
        rt.cache_drop(f"sl:pt:{pt_id}:npcstate")
        rt.invalidate_playthrough(pt_id)
    return moved


def npcs_at(pt_id, world, location, turn):
    """Who is here. Schedule drives position unless the sim moved someone.

    One exception, and it is the difference between meeting the cast and never
    meeting them at all. A build seats its opening cast in the place the player
    arrives, but a schedule is a DAILY template - so the opening cast is only
    there for the opening phase, and by the next turn the room is empty. A live
    Demon Slayer world read "CHARACTERS PRESENT: nobody" on turn one with
    Tanjiro, Nezuko and Charlie all still on the map, one turn after the player
    had met them. The people who were in the room when you walked in are still
    in the room for the rest of the first day; after that the schedule owns them
    again and the world moves normally."""
    here = []
    moved = False
    start = world.get("start_location")
    opening = bool(start) and location == start and turn < OPENING_TURNS
    for st in all_npc_states(pt_id):
        if not st["alive"]:
            continue
        npc = world.by_id.get(st["npc_id"])
        if not npc:
            continue
        scheduled = npc["schedule"][world.phase_for(turn)]
        loc = st["location"]
        # The opening cast has not scattered yet - see the note on npcs_at.
        if opening and npc["start_location"] == start:
            loc = start
        # NPCs drift back to their schedule unless the sim pinned them this turn.
        elif st["last_act_turn"] < turn - 1:
            loc = scheduled
            if loc != st["location"]:
                db.run("UPDATE npc_state SET location=? WHERE playthrough_id=? AND npc_id=?",
                       (loc, pt_id, st["npc_id"]))
                moved = True
        if loc == location:
            here.append(st["npc_id"])
    if moved:
        rt.cache_drop(f"sl:pt:{pt_id}:npcstate")
    return here
