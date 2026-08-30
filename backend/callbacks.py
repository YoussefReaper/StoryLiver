"""Reincorporation - the thing that makes a world feel like it remembers you.

The research on what makes a long campaign memorable is unanimous and it is not
about prose quality: it is CALLBACKS. The blacksmith you helped twenty sessions
ago having a gift waiting. The merchant who still will not stop talking about
cheese. Improv calls it reincorporation; tabletop GMs call it the single
cheapest way to make a world feel alive.

StoryLiver already retrieved old events - `memory.retrieve_events` has scored
them by recency + importance + relevance since the MVP. But it handed them to
the narrator under the heading ESTABLISHED FACTS YOU MUST NOT CONTRADICT. That
is a CONSTRAINT. It tells the model what it is forbidden to break; it never
once invites the model to reach back and USE any of it. So the world had a
memory and never referred to it, which reads exactly like not having one.

This module picks the events worth calling back to and frames them as material.
Two different jobs, deliberately separated:

  MUST NOT CONTRADICT   recent, load-bearing facts. A constraint. Unchanged.
  WORTH CALLING BACK TO older, human-scale, specific. An invitation.

What makes a good callback is not importance - a fated cataclysm is not a
callback, it is the plot. It is a SMALL SPECIFIC THING that the player would
be surprised anyone remembered. So the selection here deliberately inverts the
usual ranking: old beats recent, and a promise or a slight beats a battle.

Deterministic, $0. No model call - this only changes what the narrator is
handed, never how many times it is asked.
"""
from __future__ import annotations

from . import db, memory

# How long ago an event has to be before reaching back to it reads as memory
# rather than as continuity. Anything inside this window is just "what is
# happening"; the narrator already has it under the no-contradict heading.
MIN_AGE = 6

# The kinds of thing a person would actually be touched (or unnerved) that
# somebody remembered. Weighted, because a promise landing later is worth more
# than a place being discovered later.
CALLBACK_WEIGHT = {
    "promise": 3.0,      # you said you would. they remember you said it.
    "rejection": 2.6,    # the world told you no, and it still holds
    "betrayal": 2.6,
    "gift": 2.2,
    "kindness": 2.2,
    "slight": 2.0,
    "bond": 1.8,
    "discovery": 1.2,
    "combat": 1.0,
    "action": 1.0,
    "fate": 0.0,         # fate is the plot, never a callback
    "world": 0.4,
    "authority": 1.6,
}

# One, not two. This is additive prompt text and the flat-context guarantee
# (a 4-player prompt must not exceed the single-player ceiling) is worth more
# than a second suggestion the narrator was told to use at most one of anyway.
MAX_CALLBACKS = 1


def _weight(kind: str) -> float:
    return CALLBACK_WEIGHT.get(kind, 1.0)


def candidates(pt_id, turn, *, present=None, limit=MAX_CALLBACKS) -> list:
    """Old, specific, human-scale moments worth reaching back to now.

    Scored the opposite way to `retrieve_events`: age is a BONUS here, not a
    penalty, because the whole effect depends on the thing being old enough
    that the player has stopped expecting it. Bounded and deterministic - the
    same turn always offers the same callbacks."""
    cutoff = turn - MIN_AGE
    if cutoff < 1:
        # The story is younger than the window. Flooring this at 0 (as it was)
        # let the turn-0 arrival event through, so turn two offered "you
        # arrived here" as a callback - the opposite of the intended effect.
        return []
    rows = db.rows(
        "SELECT id, turn, actor, action, consequence, kind, importance, location"
        " FROM timeline_events WHERE playthrough_id=? AND turn<=? AND turn>0 ORDER BY id",
        (pt_id, cutoff))
    if not rows:
        return []

    present = set(present or [])
    scored = []
    for e in rows:
        w = _weight(e["kind"])
        if w <= 0:
            continue
        ago = max(0, turn - e["turn"])
        # Age helps, with a ceiling so a turn-1 event does not dominate forever.
        age_bonus = min(2.0, ago / 12.0)
        # Something involving a character who is IN THE ROOM is far more
        # affecting than a callback to somebody absent - the person can react.
        here = 1.3 if (e["actor"] in present or
                       any(p in (e["action"] or "") for p in present)) else 0.0
        # Small things beat big ones: a 5-importance cataclysm is the plot.
        smallness = (6 - min(5, e["importance"])) / 5.0
        scored.append((w + age_bonus + here + smallness, e))

    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [e for _, e in scored[:limit]]


def block(pt_id, turn, *, present=None) -> str:
    """The prompt fragment. Empty early on, which is correct - a story six
    turns old has nothing to remember yet, and inviting a callback to
    something that just happened produces the opposite of the effect."""
    picks = candidates(pt_id, turn, present=present)
    if not picks:
        return ""
    lines = []
    for e in picks:
        ago = max(0, turn - e["turn"])
        who = e["actor"] or "someone"
        what = (e["action"] or "").strip()
        lines.append(f"  - {ago} turns ago, {who}: {what[:150]}")
    # Terse on purpose: this is additive prompt text, and a 4-player prompt
    # must not exceed the single-player ceiling. That guarantee outranks a
    # well-phrased instruction.
    return ("\nMAY REMEMBER (optional, only if it fits):\n" + "\n".join(lines)
            + "\n  A half-sentence, not an explanation. Ignore if it does not fit.")


def note_promise(pt_id, turn, player, text, *, location="") -> None:
    """A promise is the highest-value callback there is, and the timeline has
    no way to mark one otherwise - it would be filed as an ordinary action and
    scored like one. Recorded as its own kind so it stays reachable."""
    memory.add_event(pt_id, turn, player, text[:200],
                     "They will remember that this was said.",
                     kind="promise", importance=3, location=location)


PROMISE_MARKERS = (
    "i promise", "i swear", "i'll come back", "i will come back", "i give you my word",
    "you have my word", "i'll protect", "i will protect", "i'll find", "i will find",
    "i vow", "count on me", "i won't let", "i will not let",
)


def looks_like_promise(action: str) -> bool:
    """Deliberately literal. A false negative costs nothing; a false positive
    files an ordinary sentence as a vow the world will hold you to."""
    low = (action or "").lower()
    return any(m in low for m in PROMISE_MARKERS)
