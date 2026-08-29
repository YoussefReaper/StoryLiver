"""Workstream A - making the awake world READABLE.

The simulation underneath is the moat, and until now it was invisible. Every
turn the engine decides who could see you, what they now believe, how far a
rumour has travelled and whether anyone has been sent after you - and the
player saw a paragraph of prose and a chat box. That is the exact reason the
product read as a chatbot: not because the depth was missing, but because it
never reached the surface.

The design rule here is borrowed from what roguelikes actually do well, and it
is the opposite of a stats dump: SIGNAL FIRST, NUMBERS ON DEMAND. The player is
never asked to understand the math. They are told "someone is watching" and
"the Warden's men are two days out", and only if they want the number do they
get the number.

Three things make that honest rather than decorative:

  1. Nothing here computes anything. Every line is READ from state the engine
     already produced. If the panel says a rumour is two turns from the market,
     that is because the rumour genuinely arrives in two turns - the panel
     cannot lie, because it has no numbers of its own.

  2. It is filtered by what the PLAYER knows, not by what is true. A hunt the
     player has not heard about does not appear. This is the same
     non-omniscience rule the rest of the engine obeys, applied to the UI.

  3. Depth is mode-gated. Cozy gets a pulse; Hardcore gets the full
     intelligence board. The scaffolding research is consistent that expertise
     is built by revealing complexity in layers, not by hiding a "show
     advanced" checkbox.

Deterministic, $0, no model call.
"""
from __future__ import annotations

from . import awareness, db, memory, modes, relationships

# --------------------------------------------------------------------------
# Bands. Naming these in code rather than in the template means the panel, the
# tooltip and the faction chip can never disagree about what -50 means.
# --------------------------------------------------------------------------

STANDING_BANDS = (
    (-100, -60, "hunted", "They want you gone."),
    (-60, -35, "hated", "They would turn you in."),
    (-35, -12, "disliked", "They watch you sideways."),
    (-12, 12, "unknown", "You are nobody to them yet."),
    (12, 35, "tolerated", "They would hear you out."),
    (35, 60, "trusted", "They would take your word."),
    (60, 101, "beloved", "They would stand in front of you."),
)

FEAR_BANDS = ((0, 15, ""), (15, 40, "wary"), (40, 70, "afraid"), (70, 101, "terrified"))

# How loud each signal is, so the panel can sort by what matters rather than by
# what happened to be computed last.
URGENCY = {"hunt": 5, "warrant": 5, "witness": 3, "rumour": 2, "standing": 2, "quiet": 0}


def _reads_plural(name: str) -> bool:
    """Good enough, and wrong in the harmless direction: a false negative just
    gives a singular verb to a singular-sounding name."""
    low = (name or "").lower()
    return (low.startswith("the people") or low.startswith("the folk")
            or " men" in low or low.endswith("s") and not low.endswith("ss"))


def band(standing: float) -> tuple:
    for lo, hi, name, blurb in STANDING_BANDS:
        if lo <= standing < hi:
            return name, blurb
    return "unknown", "You are nobody to them yet."


def fear_band(fear: float) -> str:
    for lo, hi, name in FEAR_BANDS:
        if lo <= fear < hi:
            return name
    return ""


# --------------------------------------------------------------------------
# A1 - the pulse
# --------------------------------------------------------------------------

def pulse(pt_id, world, player, turn) -> list:
    """The always-on signals. Short, plain, and never numeric unless the number
    IS the point (an ETA is; a standing score is not)."""
    out = []

    # Someone is watching - derived from facts NPCs learned this turn about
    # this player, which is exactly what witness() writes.
    watchers = _watchers(pt_id, world, player, turn)
    if watchers:
        names = [world.npc_name(w) for w in watchers[:3] if w in world.by_id]
        out.append({
            "kind": "witness", "sigil": "eye", "tone": "warn",
            "text": "Someone is watching." if len(watchers) == 1 else "You are being watched.",
            "detail": ("Seen by " + ", ".join(names) + ".") if names else "",
            "urgency": URGENCY["witness"], "count": len(watchers),
        })

    # A rumour in transit, with where it is going and roughly when.
    for r in _rumours_in_transit(pt_id, world, turn):
        out.append({
            "kind": "rumour", "sigil": "wave", "tone": "warn",
            "text": f"Word is travelling toward {r['to']}.",
            "detail": f"Arrives in about {r['turns']} turn{'s' if r['turns'] != 1 else ''}"
                      f" — {r['summary'][:80]}",
            "urgency": URGENCY["rumour"], "eta": r["turns"],
        })

    # How the places you are known react to you.
    for f in _standings(pt_id, world, player):
        if f["band"] in ("unknown",):
            continue
        # Faction names are often plural ("The people of Emberfall"), so the
        # verb has to agree or the panel reads as broken English.
        plural = _reads_plural(f["name"])
        verb = {
            "hunted":    "want you gone" if plural else "wants you gone",
            "hated":     "would turn you in",
            "disliked":  "do not trust you" if plural else "does not trust you",
            "tolerated": "will hear you out",
            "trusted":   "trust you" if plural else "trusts you",
            "beloved":   "would stand with you",
        }[f["band"]]
        out.append({
            "kind": "standing", "sigil": "banner",
            "tone": "warn" if f["standing"] < -12 else "good",
            "text": f"{f['name']} {verb}.",
            "detail": f["fear"] and f"They are {f['fear']} of you." or "",
            "urgency": URGENCY["standing"], "faction": f["id"],
        })

    # Someone is coming, and when. The window is honest because hunt_tick
    # already committed to it - the panel is only reading it back.
    for h in awareness.hunt_panel(pt_id, world, player, turn):
        if h["status"] != "enroute":
            continue
        out.append({
            "kind": "hunt", "sigil": "blade", "tone": "danger",
            "text": f"{h['faction']} has sent {h['hunter']} after you.",
            "detail": f"Arriving in {h['window']} — heading for {h['to']}.",
            "urgency": URGENCY["hunt"], "eta": h["turns_out"], "hunt_id": h["id"],
        })

    if not out:
        out.append({"kind": "quiet", "sigil": "calm", "tone": "calm",
                    "text": "Nobody is paying attention to you.",
                    "detail": "Which is its own kind of opportunity.",
                    "urgency": URGENCY["quiet"]})

    out.sort(key=lambda s: -s["urgency"])
    return out


def _watchers(pt_id, world, player, turn, window=1):
    """NPCs who learned something about this player in the last turn or two."""
    rows = db.rows(
        "SELECT DISTINCT holder_id FROM knowledge WHERE playthrough_id=?"
        " AND holder_kind='npc' AND subject=? AND turn_learned>=?",
        (pt_id, player, max(0, turn - window)))
    return [r["holder_id"] for r in rows]


def _rumours_in_transit(pt_id, world, turn):
    """A rumour carries only a fact KEY; the readable summary lives on the
    knowledge row the carrier holds. Joining them here keeps the panel honest -
    it shows the same words the receiving NPC will actually learn."""
    out = []
    for r in db.rows("SELECT * FROM rumors WHERE playthrough_id=? AND delivered=0"
                     " AND arrive_turn>?", (pt_id, turn)):
        fact = db.row("SELECT summary, confidence FROM knowledge WHERE playthrough_id=?"
                      " AND fact_key=? ORDER BY confidence DESC LIMIT 1",
                      (pt_id, r["fact_key"]))
        out.append({
            "to": world.loc_name(r["to_place"]) if r["to_place"] else "elsewhere",
            "to_id": r["to_place"],
            "turns": max(1, r["arrive_turn"] - turn),
            "summary": (fact or {}).get("summary", "something you did"),
            # Detail decays in transit, so what lands is less certain than
            # what was seen. Showing that is the point.
            "confidence": round(max(0.0, (fact or {}).get("confidence", 1.0)
                                    - r["distortion"]), 2),
        })
    # One fact reaching the same place by two carriers is still one piece of
    # news arriving. Collapse by destination and keep the earliest arrival,
    # otherwise the panel repeats itself and reads as noise.
    by_place = {}
    for r in sorted(out, key=lambda x: x["turns"]):
        by_place.setdefault(r["to_id"] or r["to"], r)
    return list(by_place.values())[:3]


def _standings(pt_id, world, player):
    out = []
    for f in awareness.factions(world):
        r = awareness.rep(pt_id, f["id"], player)
        if r["known_events"] == 0 and abs(r["standing"]) < 1:
            continue
        name, blurb = band(r["standing"])
        out.append({"id": f["id"], "name": f["name"], "standing": round(r["standing"], 1),
                    "band": name, "blurb": blurb, "fear": fear_band(r["fear"]),
                    "fear_value": round(r["fear"], 1),
                    "known_events": r["known_events"], "law": f.get("law", 0)})
    return sorted(out, key=lambda x: x["standing"])


# --------------------------------------------------------------------------
# A2 - relationship faces
# --------------------------------------------------------------------------

def faces(pt_id, world, player, *, present_only=True, location=None, turn=0) -> list:
    """Trust and fear toward YOU, per character. Two bars, not seven numbers -
    the other five scalars are real and used, but showing all of them is how a
    relationship panel becomes a spreadsheet nobody reads."""
    ids = (memory.npcs_at(pt_id, world, location, turn)
           if present_only and location else [n["id"] for n in world.npcs])
    out = []
    for npc_id in ids:
        vec = relationships.vector(pt_id, npc_id, player)
        if not vec:
            continue
        state = memory.npc_state(pt_id, npc_id)
        out.append({
            "id": npc_id, "name": world.npc_name(npc_id),
            "alive": bool(state["alive"]),
            "trust": vec["trust"], "fear": vec["fear"],
            "affinity": vec["affinity"],
            "disposition": vec["disposition"],
            "trust_pips": _pips(vec["trust"]), "fear_pips": _pips(vec["fear"], signed=False),
            "knows_about_you": _npc_knows_count(pt_id, npc_id, player),
            "will_cover": vec["will_cover"],
        })
    return out


def _pips(value, signed=True, n=5):
    """A -100..100 scalar as N filled pips. Signed values are centred so a
    negative relationship reads as damage rather than as a low score."""
    if signed:
        filled = round((value + 100) / 200 * n)
    else:
        filled = round(max(0.0, value) / 100 * n)
    return max(0, min(n, int(filled)))


def _npc_knows_count(pt_id, npc_id, player):
    row = db.row("SELECT COUNT(*) AS n FROM knowledge WHERE playthrough_id=?"
                 " AND holder_kind='npc' AND holder_id=? AND subject=?",
                 (pt_id, npc_id, player))
    return row["n"] if row else 0


# --------------------------------------------------------------------------
# A5 - what each mode is allowed to show
# --------------------------------------------------------------------------

DEPTH = {
    "chill":    {"pulse": True, "faces": False, "factions": False, "board": False},
    "normal":   {"pulse": True, "faces": True,  "factions": True,  "board": False},
    "hardcore": {"pulse": True, "faces": True,  "factions": True,  "board": True},
}


def depth_for(pt_id) -> tuple:
    """Cozy tone or Hardcore stakes decide how much of the sim is exposed.

    Deliberately keyed off the modes the player already chose rather than a
    separate 'UI complexity' setting: someone who asked for a cozy story has
    already said how much machinery they want to see."""
    m = modes.get(pt_id)
    if m["tone"] == "chill":
        return "chill", DEPTH["chill"]
    if m["stakes"] == "hardcore":
        return "hardcore", DEPTH["hardcore"]
    return "normal", DEPTH["normal"]


# --------------------------------------------------------------------------
# The one call the client makes
# --------------------------------------------------------------------------

def panel(pt_id, world, player, turn, *, location=None, overrides=None) -> dict:
    """Everything the awareness HUD needs, already filtered by mode and by
    what this player actually knows.

    `overrides` lets a player turn any section on or off regardless of mode -
    the standing rule is that every surface element is toggleable, and a
    Hardcore player who finds the board noisy should be able to close it
    without leaving Hardcore."""
    level, allow = depth_for(pt_id)
    allow = {**allow, **{k: bool(v) for k, v in (overrides or {}).items() if k in allow}}

    out = {"level": level, "shows": allow, "turn": turn}
    out["pulse"] = pulse(pt_id, world, player, turn) if allow["pulse"] else []
    out["faces"] = (faces(pt_id, world, player, location=location, turn=turn)
                    if allow["faces"] else [])
    out["factions"] = _standings(pt_id, world, player) if allow["factions"] else []
    out["board"] = intelligence_board(pt_id, world, player, turn) if allow["board"] else None
    out["hunts"] = awareness.hunt_panel(pt_id, world, player, turn)
    return out


def intelligence_board(pt_id, world, player, turn) -> dict:
    """Hardcore only: who knows what about you, and how they came to know it.

    This is the Nemesis-board idea applied to knowledge rather than to enemies -
    the value is not the list, it is seeing that Nessa knows because Corvin told
    her, and deciding what to do about Corvin."""
    rows = db.rows(
        "SELECT holder_id, fact_key, summary, source, confidence, turn_learned"
        " FROM knowledge WHERE playthrough_id=? AND holder_kind='npc' AND subject=?"
        " ORDER BY turn_learned DESC LIMIT 60", (pt_id, player))
    by_npc = {}
    for r in rows:
        by_npc.setdefault(r["holder_id"], []).append({
            "summary": r["summary"], "source": r["source"],
            "confidence": round(r["confidence"], 2), "turn": r["turn_learned"],
        })
    return {
        "knows": [{"id": k, "name": world.npc_name(k) if k in world.by_id else k,
                   "facts": v[:6], "count": len(v)}
                  for k, v in sorted(by_npc.items(), key=lambda kv: -len(kv[1]))],
        "in_transit": _rumours_in_transit(pt_id, world, turn),
        "unwitnessed": _unwitnessed_count(pt_id, player),
    }


def _unwitnessed_count(pt_id, player):
    """How many of your own acts nobody ever learned about.

    Counted by comparing the turns you acted against the turns anybody learned
    something about you - the most reassuring number in the game, and it is
    only meaningful because witnessing is genuinely gated rather than assumed."""
    acted = {r["turn"] for r in db.rows(
        "SELECT DISTINCT turn FROM timeline_events WHERE playthrough_id=? AND actor=?",
        (pt_id, player))}
    seen = {r["turn_learned"] for r in db.rows(
        "SELECT DISTINCT turn_learned FROM knowledge WHERE playthrough_id=?"
        " AND holder_kind='npc' AND subject=?", (pt_id, player))}
    return len(acted - seen)


# --------------------------------------------------------------------------
# B2 - the explainers, kept next to the thing they explain
# --------------------------------------------------------------------------

TOOLTIPS = {
    "pulse": ("What the world has noticed about you.",
              "Only things that actually happened. If nobody saw it, it is not here."),
    "witness": ("Someone perceived what you just did.",
                "Being seen is what turns an action into a consequence. Light, noise and "
                "cover all decide it — and so does who was in the room."),
    "rumour": ("What one person knows is travelling to other people.",
               "It moves at walking pace and loses detail on the way, so you have time to "
               "get ahead of it — or to be somewhere else when it lands."),
    "standing": ("How one group feels about you, based only on what THEY know.",
                 "There is no global reputation score. A faction that heard nothing "
                 "thinks nothing."),
    "hunt": ("Someone has been sent to find you.",
             "The window is honest: they are really that far away, and they really are "
             "coming. Move, hide, or deal with the reason."),
    "faces": ("Trust and fear, per person, toward you specifically.",
              "Each character remembers you separately. What one knows, another may not."),
    "factions": ("Every group that has an opinion, and how strong it is.",
                 "Standing drifts back toward neutral if you stop giving them reasons."),
    "board": ("Who knows what about you, and how they found out.",
              "Follow a fact back to who spread it. That is usually the person to talk to."),
    "bounty": ("What the authority here is prepared to do about you.",
               "It escalates in steps, and every step can be undone before the next one."),
}


def tooltips() -> dict:
    return {k: {"what": v[0], "why": v[1]} for k, v in TOOLTIPS.items()}
