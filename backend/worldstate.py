"""Layer 2 - the living world vector. Fully deterministic, $0 LLM.

Time, weather, light, noise and unrest evolve every turn from a seeded chain,
then feed three systems that would otherwise be guesswork:

  * stealth       - light and noise ARE the detection math (§5)
  * NPC routines  - who is where depends on phase, and on weather
  * narration     - the Narrator is handed the world, not asked to invent it

Weather is a Markov walk seeded per playthrough, so a story replays identically
and a test can assert on it.
"""
from __future__ import annotations

import json

from . import db, llm

PHASES = ["morning", "midday", "evening", "night"]

# state -> [(next_state, weight)]. Chosen so weather persists but is not static.
WEATHER_CHAIN = {
    "clear":   [("clear", 5), ("cloud", 3), ("wind", 2)],
    "cloud":   [("cloud", 4), ("clear", 3), ("rain", 3), ("fog", 2)],
    "rain":    [("rain", 4), ("cloud", 4), ("storm", 2)],
    "storm":   [("storm", 2), ("rain", 4), ("cloud", 3)],
    "fog":     [("fog", 3), ("cloud", 4), ("clear", 2)],
    "wind":    [("wind", 3), ("clear", 3), ("cloud", 3), ("storm", 1)],
    "snow":    [("snow", 4), ("cloud", 3), ("fog", 2)],
    "ashfall": [("ashfall", 3), ("cloud", 3), ("fog", 3)],
}

# Ambient light 0 (pitch dark) .. 5 (glare), before weather.
PHASE_LIGHT = {"morning": 4, "midday": 5, "evening": 3, "night": 1}
PHASE_NOISE = {"morning": 3, "midday": 4, "evening": 3, "night": 1}

WEATHER_LIGHT = {"clear": 0, "cloud": -1, "rain": -1, "storm": -2,
                 "fog": -2, "wind": 0, "snow": -1, "ashfall": -2}
WEATHER_NOISE = {"clear": 0, "cloud": 0, "rain": 2, "storm": 3,
                 "fog": -1, "wind": 2, "snow": -1, "ashfall": 0}

WEATHER_WORDS = {
    "clear": "clear", "cloud": "overcast", "rain": "raining", "storm": "storming",
    "fog": "fogbound", "wind": "blowing hard", "snow": "snowing", "ashfall": "ashfall",
}

# Places whose interior ignores the sky.
INDOOR_KINDS = {"tavern", "work", "sacred", "civic"}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def get(pt_id):
    row = db.row("SELECT * FROM world_state WHERE playthrough_id=?", (pt_id,))
    if row:
        return row
    db.run("INSERT OR IGNORE INTO world_state (playthrough_id, updated_at) VALUES (?,?)",
           (pt_id, db.now()))
    return db.row("SELECT * FROM world_state WHERE playthrough_id=?", (pt_id,))


def seed(pt_id, world):
    st = get(pt_id)
    start = (world.get("weather") or "clear")
    if start not in WEATHER_CHAIN:
        start = "clear"
    db.run(
        "UPDATE world_state SET turn=0, day=1, phase='morning', weather=?, temperature=?,"
        " light=?, noise=?, flags=?, updated_at=? WHERE playthrough_id=?",
        (start, int(world.get("base_temperature", 12)),
         PHASE_LIGHT["morning"] + WEATHER_LIGHT.get(start, 0),
         PHASE_NOISE["morning"] + WEATHER_NOISE.get(start, 0),
         json.dumps({}), db.now(), pt_id))
    return get(pt_id)


def _next_weather(pt_id, turn, current):
    options = WEATHER_CHAIN.get(current) or WEATHER_CHAIN["clear"]
    rng = llm.rng(pt_id, turn, "weather")
    total = sum(w for _, w in options)
    pick = rng.random() * total
    acc = 0.0
    for name, weight in options:
        acc += weight
        if pick <= acc:
            return name
    return options[-1][0]


def tick(pt_id, world, turn, *, events=None):
    """One turn of world time. Called after the turn commits, never by a model."""
    st = get(pt_id)
    phase = PHASES[turn % 4]
    day = turn // 4 + 1

    # Weather only rolls when the phase rolls over into a new day-part.
    weather = st["weather"]
    if turn != st["turn"]:
        weather = _next_weather(pt_id, turn, weather)

    forced = (world.get("forced_weather") or {}).get(str(turn))
    if forced in WEATHER_CHAIN:
        weather = forced

    light = _clamp(PHASE_LIGHT[phase] + WEATHER_LIGHT.get(weather, 0), 0, 5)
    noise = _clamp(PHASE_NOISE[phase] + WEATHER_NOISE.get(weather, 0), 0, 5)

    base_t = int(world.get("base_temperature", 12))
    temperature = base_t + {"morning": -2, "midday": 3, "evening": 0, "night": -5}[phase]
    temperature += {"storm": -3, "snow": -8, "rain": -2, "fog": -1, "clear": 1}.get(weather, 0)

    unrest = st["unrest"]
    for e in events or []:
        unrest += {"fate": 12, "contest": 6, "npc": 1, "rejection": 0}.get(e, 2)
    unrest = _clamp(int(unrest * 0.94), 0, 100)     # decays without new pressure

    db.run(
        "UPDATE world_state SET turn=?, day=?, phase=?, weather=?, light=?, noise=?,"
        " temperature=?, unrest=?, updated_at=? WHERE playthrough_id=?",
        (turn, day, phase, weather, light, noise, temperature, unrest, db.now(), pt_id))
    return get(pt_id)


def local(pt_id, world, place_id):
    """The world as felt in one place. Indoors mutes the sky."""
    st = get(pt_id)
    place = world.loc_by_id.get(place_id) or {}
    indoors = place.get("kind") in INDOOR_KINDS
    light = st["light"]
    noise = st["noise"]
    if indoors:
        light = _clamp(max(light, 2) if st["phase"] != "night" else 2, 0, 5)
        noise = _clamp(noise + (1 if place.get("kind") == "tavern" else -1), 0, 5)
    return {
        **dict(st),
        "indoors": indoors,
        "light": light,
        "noise": noise,
        "place_id": place_id,
        "weather_word": WEATHER_WORDS.get(st["weather"], st["weather"]),
    }


def line(pt_id, world, place_id) -> str:
    """One clause for the Narrator's context. Cheap, factual, no adjectives."""
    w = local(pt_id, world, place_id)
    bits = [f"day {w['day']}", w["phase"]]
    if not w["indoors"]:
        bits.append(w["weather_word"])
    bits.append(f"{w['temperature']}C")
    if w["light"] <= 1:
        bits.append("almost no light")
    elif w["light"] >= 5:
        bits.append("hard light")
    if w["unrest"] >= 50:
        bits.append("the place is on edge")
    return ", ".join(bits)


def set_flag(pt_id, key, value=True):
    st = get(pt_id)
    flags = db.jload(st["flags"], {}) or {}
    flags[key] = value
    db.run("UPDATE world_state SET flags=? WHERE playthrough_id=?",
           (json.dumps(flags), pt_id))
    return flags


def flags(pt_id):
    return db.jload(get(pt_id)["flags"], {}) or {}


def public(pt_id, world, place_id=None):
    st = get(pt_id)
    out = dict(st)
    out["weather_word"] = WEATHER_WORDS.get(st["weather"], st["weather"])
    out["flags"] = db.jload(st["flags"], {}) or {}
    if place_id:
        out["here"] = local(pt_id, world, place_id)
    return out
