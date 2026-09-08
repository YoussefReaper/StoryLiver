"""The world moved without you.

This is the one promise a chat window structurally cannot make. Everything
else in this engine can be imitated by a good prompt; a place that kept going
in the hours you were not looking cannot, because it needs state that outlives
the session and a clock that does not wait for you.

Before this module the claim was false. `worldstate.tick()` is documented as
"one turn of world time, called after the turn commits" - so the world advanced
ONLY when the player typed. Close the tab on a rainy Tuesday night, come back
in a week, and it was still that same rainy Tuesday night, with the same people
standing in the same doorways. The product's third pillar was a sentence in a
brief and nothing in the build.

TWO CLOCKS, AND ONLY ONE OF THEM WAITS FOR YOU
------------------------------------------------------------------
The fix is not "advance the story while they are gone" - that would play the
player's game for them. It is that a world has two clocks and they are not the
same clock:

  THE STORY CLOCK  (playthroughs.current_turn) is the player's. Fated events
                   hang off it, Mana is spent against it, and the seventh night
                   arrives when they get there. It does not move while they are
                   away. Fate is fixed and it waits.

  THE AMBIENT CLOCK (world_state phase/day/weather, rumours in transit,
                   hunters en route) is the world's. Dawn does not wait to be
                   observed. Gossip does not pause. Someone sent to find you
                   keeps walking.

So an absence drifts the AMBIENT clock and never touches the story clock. That
division is the whole design: come back and the hour, the weather and who is
looking for you have all moved, while the story is exactly where you left it.

WHAT IT IS ALLOWED TO DO WHILE YOU ARE GONE
------------------------------------------------------------------
Strictly bounded, because "the world kept going" must never mean "the world
resolved your story without you":

  * It may move the hour, the day and the weather.
  * It may deliver a rumour that was already in flight, and move a hunter that
    was already en route - both of which are consequences of things the player
    already did, merely arriving on schedule.
  * It may NOT fire a fated event, kill anyone, move the player, spend Mana,
    or invent an occurrence. Every line in the digest is read back out of state
    that deterministic systems computed - the maths is the truth and the digest
    only reports it, the same rule fastforward.py works to.

The rate is deliberately slow (one ambient phase per REAL hour, capped at two
days' worth) so that a week away and a month away read the same. An absence is
a texture, not a punishment, and nobody should return to find the valley burned
down because they had a busy fortnight.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import db, worldstate

# One ambient phase (morning -> midday -> evening -> night) per real hour.
HOURS_PER_PHASE = 1.0
# Two days of ambient drift, whatever the absence. A month away and a week away
# both come back to "a couple of days have passed", because the alternative is
# punishing people for having lives.
MAX_PHASES = 8
# Under this, nothing is claimed at all. Reloading the tab, or stepping away for
# lunch, must never produce a "while you were away" card - a moment that fires
# when nothing happened is worse than no moment.
MIN_AWAY_MINUTES = 50


def _parse(stamp: str):
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def note_seen(pt_id: str) -> None:
    """Stamp the wall clock. Called when a player opens a story and after every
    turn, so 'away' means away from THIS story rather than away from the app."""
    db.run("UPDATE playthroughs SET last_seen_at=? WHERE id=?", (db.now(), pt_id))


def away_minutes(pt_id: str, *, now=None) -> float:
    row = db.row("SELECT last_seen_at FROM playthroughs WHERE id=?", (pt_id,))
    seen = _parse((row or {}).get("last_seen_at") or "")
    if not seen:
        return 0.0
    delta = ((now or _now()) - seen).total_seconds() / 60.0
    return max(0.0, delta)


def phases_owed(minutes: float) -> int:
    """How far the ambient clock drifts for an absence of this long."""
    if minutes < MIN_AWAY_MINUTES:
        return 0
    return max(1, min(MAX_PHASES, int(minutes // (HOURS_PER_PHASE * 60))))


def _humanise(minutes: float) -> str:
    if minutes < 90:
        return "about an hour"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(round(hours))} hours"
    days = hours / 24.0
    if days < 2:
        return "a day"
    if days < 14:
        return f"{int(days)} days"
    if days < 60:
        return f"{max(2, int(days // 7))} weeks"
    return "a long time"


def _drift_ambient(pt_id: str, world, phases: int) -> dict:
    """Move the hour, the day and the weather - and nothing else.

    Deliberately does NOT call worldstate.tick(): that function derives day and
    phase from the STORY turn, so using it here would drag the story clock
    along with the weather, which is the one thing an absence must not do.
    """
    st = worldstate.get(pt_id)
    before = {"phase": st["phase"], "day": st["day"], "weather": st["weather"]}

    idx = worldstate.PHASES.index(st["phase"]) if st["phase"] in worldstate.PHASES else 0
    weather = st["weather"]
    day = st["day"]
    for step in range(phases):
        idx += 1
        if idx >= len(worldstate.PHASES):
            idx = 0
            day += 1
        # Seeded on the ambient step so a story still replays identically.
        weather = worldstate._next_weather(pt_id, 10_000 + step, weather)

    phase = worldstate.PHASES[idx]
    light = worldstate._clamp(
        worldstate.PHASE_LIGHT[phase] + worldstate.WEATHER_LIGHT.get(weather, 0), 0, 5)
    noise = worldstate._clamp(
        worldstate.PHASE_NOISE[phase] + worldstate.WEATHER_NOISE.get(weather, 0), 0, 5)
    base_t = int((world.get("base_temperature", 12) if hasattr(world, "get") else 12) or 12)
    temperature = base_t + {"morning": -2, "midday": 3, "evening": 0, "night": -5}[phase]
    temperature += {"storm": -3, "snow": -8, "rain": -2, "fog": -1, "clear": 1}.get(weather, 0)
    # Nothing pressed on the world while nobody was in it, so tension bleeds off.
    unrest = worldstate._clamp(int(st["unrest"] * (0.97 ** phases)), 0, 100)

    db.run(
        "UPDATE world_state SET day=?, phase=?, weather=?, light=?, noise=?,"
        " temperature=?, unrest=?, updated_at=? WHERE playthrough_id=?",
        (day, phase, weather, light, noise, temperature, unrest, db.now(), pt_id))
    return {"before": before, "after": {"phase": phase, "day": day, "weather": weather}}


WEATHER_LINE = {
    "clear": "the sky cleared", "cloud": "the cloud came down", "rain": "the rain set in",
    "storm": "a storm came through", "fog": "fog filled the low ground",
    "wind": "the wind got up", "snow": "snow started", "ashfall": "ash began falling",
}


def catch_up(pt_id: str, world, *, player="user", here="", now=None) -> dict | None:
    """What happened while nobody was looking. None when nothing did.

    Returns a digest the UI shows once, on return. Every line is read back out
    of deterministic state - this function never asks a model what happened,
    because a model would happily invent a war.
    """
    minutes = away_minutes(pt_id, now=now)
    phases = phases_owed(minutes)
    if not phases:
        note_seen(pt_id)
        return None

    moved = _drift_ambient(pt_id, world, phases)
    items = []

    after, before = moved["after"], moved["before"]
    if after["day"] > before["day"]:
        nights = after["day"] - before["day"]
        items.append(f"{'A night' if nights == 1 else str(nights) + ' nights'} passed. "
                     f"It is {after['phase']} on day {after['day']}.")
    else:
        items.append(f"The hour moved on. It is {after['phase']} now.")

    if after["weather"] != before["weather"]:
        items.append(WEATHER_LINE.get(after["weather"], f"the weather turned to {after['weather']}")
                     .capitalize() + ".")

    items.extend(_routines_moved(world, before["phase"], after["phase"], here))
    items.extend(_standing_threats(pt_id, player))
    if len(items) < 3:
        items.extend(_rumoured_places(pt_id))

    note_seen(pt_id)
    return {
        "away": _humanise(minutes),
        "away_minutes": int(minutes),
        "phases": phases,
        "items": items[:4],
        "day": after["day"],
        "phase": after["phase"],
        "weather": after["weather"],
    }


def _routines_moved(world, before_phase: str, after_phase: str, here: str) -> list:
    """Who is no longer where you left them.

    This is the most honest "the world moved" line available, because it is not
    a claim at all - it is a read of each character's OWN schedule, which is
    world data the builder wrote and the engine already uses to place people.
    The hour genuinely changed, so the people genuinely moved, and the digest
    is just reporting where their routine now has them.
    """
    if before_phase == after_phase or not here:
        return []
    left, arrived = [], []
    for npc in (getattr(world, "npcs", None) or []):
        sched = npc.get("schedule") or {}
        was, now = sched.get(before_phase), sched.get(after_phase)
        name = npc.get("name") or ""
        if not name or not was or not now or was == now:
            continue
        if was == here:
            try:
                where = world.loc_name(now)
            except Exception:
                continue
            left.append(f"{name} has gone — by this hour they are at {where}.")
        elif now == here:
            # The better line of the two: you come back and somebody is
            # standing where you left nobody.
            arrived.append(f"{name} is here now, and was not when you left.")
    return (arrived + left)[:2]


def _standing_threats(pt_id: str, player: str) -> list:
    """Somebody the player already provoked is still out there.

    Deliberately phrased as a STANDING fact, not as movement. Hunts are
    scheduled against the STORY turn, which does not advance while the player
    is away - so saying they "got closer" would be exactly the invented
    occurrence this module refuses to produce. Only hunts the player already
    knows about are mentioned: an absence does not lift the witness gate.
    """
    from . import awareness
    out = []
    try:
        rows = db.rows("SELECT id FROM hunts WHERE playthrough_id=? AND target_id=?"
                       " AND status='enroute'", (pt_id, player))
    except Exception:
        return out
    for h in rows:
        try:
            if not awareness.knows(pt_id, "player", player, f"hunt:{h['id']}"):
                continue
        except Exception:
            continue
        out.append("Whoever was sent after you is still out there, and none of this waited.")
        break
    return out


def _rumoured_places(pt_id: str) -> list:
    """A place the player has only heard of is still only heard of. Reported as
    a nudge, never as news that arrived while they were gone."""
    out = []
    try:
        rows = db.rows("SELECT note FROM atlas_places WHERE playthrough_id=?"
                       " AND status='rumoured' AND note != '' LIMIT 1", (pt_id,))
    except Exception:
        return out
    for r in rows:
        note = (r.get("note") or "").strip()
        if note:
            out.append(f"Still only a rumour, and still unvisited: {note}")
    return out
