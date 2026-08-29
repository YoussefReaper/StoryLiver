"""Campaign streaks - loss-aversion retention without the dark pattern.

Duolingo's streak works because people do not want to lose a number they built.
The part we deliberately do not copy is the guilt: nothing here nags, no streak
freeze is sold, a broken streak is stated in one neutral line, and the longest
run is kept forever so the thing you built is never actually taken away.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import db

MILESTONES = [3, 7, 14, 30, 60, 100, 365]


def _row(user_id):
    row = db.row("SELECT * FROM streaks WHERE user_id=?", (user_id,))
    if row:
        return row
    db.run("INSERT OR IGNORE INTO streaks (user_id) VALUES (?)", (user_id,))
    return db.row("SELECT * FROM streaks WHERE user_id=?", (user_id,))


def touch(user_id, today=None):
    """Called once per action. Advances the streak on the first action of a
    new day; leaves it alone for the rest of that day."""
    today = today or db.today()
    row = _row(user_id)
    if row["last_day"] == today:
        return status(user_id)

    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    broke = bool(row["last_day"]) and row["last_day"] != yesterday
    current = 1 if (broke or not row["last_day"]) else row["current"] + 1
    longest = max(row["longest"], current)

    hit = [m for m in MILESTONES if m == current]
    milestones = db.jload(row["milestones"], [])
    for m in hit:
        milestones.append({"days": m, "on": today})

    db.run(
        "UPDATE streaks SET current=?, longest=?, last_day=?, total_days=total_days+1, milestones=?"
        " WHERE user_id=?",
        (current, longest, today, json.dumps(milestones[-24:]), user_id))
    out = status(user_id, today)
    out["broke"] = broke
    out["milestone"] = hit[0] if hit else None
    return out


def status(user_id, today=None):
    today = today or db.today()
    row = _row(user_id)
    nxt = next((m for m in MILESTONES if m > row["current"]), None)
    return {
        "current": row["current"],
        "longest": row["longest"],
        "total_days": row["total_days"],
        "last_day": row["last_day"],
        "active_today": row["last_day"] == today,
        "next_milestone": nxt,
        "to_next": (nxt - row["current"]) if nxt else None,
        "milestones": db.jload(row["milestones"], []),
        # Stated once, neutrally. Never a notification, never a guilt hook.
        "note": "Your longest run is kept forever. Breaking a streak costs you nothing else.",
    }
