"""Runs and meta-progression - the roguelike loop.

A run is one entry into a world: it starts, stakes escalate, and it ENDS
(death, victory, or retiring on your own terms). This is the frame that
separates StoryLiver from an infinite chat thread - a thread has no shape and
therefore no stakes, while a run has both.

Meta-progression is the other half, and it is not decoration. The research on
permadeath is consistent: permadeath alone is punishing, permadeath plus a
carried fraction is a loop players re-enter voluntarily. So a run that ends
badly still pays out - what you learned about the world, who you met, how deep
you got - and the next run starts knowing it.

What carries is deliberately NOT power. Carrying stats would make run 5
trivially easier than run 1 and flatten the whole point. What carries is
KNOWLEDGE and STANDING: lore you uncovered, places you found, and the world's
memory of your name. You start no stronger. You start less lost.

Deterministic, $0, no model call anywhere in this module.
"""
from __future__ import annotations

import json
import uuid

from . import db, modes

# A run ends one of these ways. Only 'died' is a failure, and even it pays out.
ENDINGS = ("died", "victory", "retired", "abandoned")

# The fraction of discovered lore that survives a failed run. Deliberately
# below 1.0: a run should be worth re-playing, not merely re-loaded.
CARRY_FRACTION = 0.5


def start(pt_id, user_id, *, world_id="", run_no=None) -> dict:
    """Open a run. Idempotent per playthrough: an active run is returned as-is
    rather than silently opening a second one."""
    existing = active(pt_id)
    if existing:
        return existing

    if run_no is None:
        prev = db.row("SELECT MAX(run_no) AS n FROM runs WHERE playthrough_id=?", (pt_id,))
        run_no = (prev["n"] or 0) + 1 if prev else 1

    run_id = uuid.uuid4().hex[:12]
    db.run(
        "INSERT INTO runs (id,playthrough_id,user_id,run_no,world_id,status,started_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, pt_id, user_id, run_no, world_id, "active", db.now()),
    )
    db.run("UPDATE playthroughs SET run_no=?, run_state='active' WHERE id=?", (run_no, pt_id))
    return get(run_id)


def active(pt_id):
    return db.row("SELECT * FROM runs WHERE playthrough_id=? AND status='active'"
                  " ORDER BY started_at DESC LIMIT 1", (pt_id,))


def get(run_id):
    return db.row("SELECT * FROM runs WHERE id=?", (run_id,))


def end(pt_id, *, reason="retired", world_id="", turn=0) -> dict:
    """Close the run and settle meta-progression.

    Called on death in HARDCORE, on a victory condition, or when a player
    retires a character deliberately. Returns what carried forward, because
    that payout is the thing that makes the next run start."""
    if reason not in ENDINGS:
        raise ValueError(f"unknown ending {reason!r}")

    run = active(pt_id)
    if not run:
        return {"ended": False, "reason": "no active run"}

    db.run("UPDATE runs SET status='ended', ended_reason=?, turns=?, peak_turn=?, ended_at=?"
           " WHERE id=?",
           ("ended:" + reason, turn, turn, db.now(), run["id"]))
    db.run("UPDATE playthroughs SET run_state=? WHERE id=?", ("ended", pt_id))

    carried = _settle(run["user_id"], world_id or run["world_id"], pt_id,
                      reason=reason, turn=turn,
                      full_carry=modes.meta_progression_on(pt_id) is False)
    return {"ended": True, "run_no": run["run_no"], "reason": reason,
            "turns": turn, "carried": carried}


# --------------------------------------------------------------------------
# Meta-progression
# --------------------------------------------------------------------------

def progress(user_id, world_id) -> dict:
    row = db.row("SELECT * FROM meta_progress WHERE user_id=? AND world_id=?",
                 (user_id, world_id))
    if not row:
        return {"user_id": user_id, "world_id": world_id, "runs_completed": 0,
                "deepest_turn": 0, "unlocks": [], "known_lore": [], "echoes": []}
    return {**row,
            "unlocks": db.jload(row["unlocks"], []),
            "known_lore": db.jload(row["known_lore"], []),
            "echoes": db.jload(row["echoes"], [])}


def _settle(user_id, world_id, pt_id, *, reason, turn, full_carry=False) -> dict:
    """What survives the run.

    Knowledge and standing, never power. A victory carries everything it
    found; a death carries CARRY_FRACTION of it - enough that the run mattered,
    little enough that re-running is still the point."""
    prior = progress(user_id, world_id)

    discovered = _discovered_lore(pt_id)
    keep_all = full_carry or reason in ("victory", "retired")
    if keep_all:
        kept = discovered
    else:
        cut = max(1, int(len(discovered) * CARRY_FRACTION)) if discovered else 0
        kept = discovered[:cut]

    lore = sorted(set(prior["known_lore"]) | set(kept))
    unlocks = sorted(set(prior["unlocks"]) | set(_unlocks_for(reason, turn, prior)))
    echo = _echo(reason, turn, prior["runs_completed"] + 1)
    echoes = (prior["echoes"] + [echo])[-24:]

    db.run(
        "INSERT INTO meta_progress (user_id,world_id,runs_completed,deepest_turn,unlocks,"
        "known_lore,echoes,updated_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(user_id,world_id) DO UPDATE SET"
        " runs_completed=excluded.runs_completed, deepest_turn=excluded.deepest_turn,"
        " unlocks=excluded.unlocks, known_lore=excluded.known_lore,"
        " echoes=excluded.echoes, updated_at=excluded.updated_at",
        (user_id, world_id, prior["runs_completed"] + 1,
         max(prior["deepest_turn"], turn), json.dumps(unlocks), json.dumps(lore),
         json.dumps(echoes), db.now()),
    )
    return {"lore_carried": len(kept), "lore_total": len(lore),
            "unlocks": unlocks, "echo": echo,
            "runs_completed": prior["runs_completed"] + 1,
            "deepest_turn": max(prior["deepest_turn"], turn)}


def _discovered_lore(pt_id):
    """Places found and world-changing events witnessed. These are facts about
    the world, so knowing them next run is knowledge, not power."""
    out = []
    for r in db.rows("SELECT place_id FROM atlas_places WHERE playthrough_id=?"
                     " AND status != 'hidden'", (pt_id,)):
        out.append(f"place:{r['place_id']}")
    for r in db.rows("SELECT DISTINCT action FROM timeline_events WHERE playthrough_id=?"
                     " AND importance>=4 ORDER BY id LIMIT 40", (pt_id,)):
        out.append(f"event:{r['action'][:60]}")
    return out


def _unlocks_for(reason, turn, prior):
    """Milestones, not upgrades. Each says 'you have seen this much of the
    world', which is why they are safe to carry."""
    out = []
    if reason == "victory":
        out.append("ending:seen")
    if turn >= 25:
        out.append("depth:25")
    if turn >= 60:
        out.append("depth:60")
    if prior["runs_completed"] + 1 >= 3:
        out.append("veteran")
    return out


def _echo(reason, turn, run_no):
    """One line the next run can be told about the last one. This is what makes
    a second run feel like a continuation rather than a reset."""
    if reason == "died":
        return f"Run {run_no} ended at turn {turn}. Someone did not walk out."
    if reason == "victory":
        return f"Run {run_no} reached its ending at turn {turn}."
    if reason == "retired":
        return f"Run {run_no} was set down at turn {turn}, still standing."
    return f"Run {run_no} was left at turn {turn}."


def history(user_id, limit=20):
    return db.rows("SELECT id,playthrough_id,run_no,world_id,status,ended_reason,turns,"
                   "started_at,ended_at FROM runs WHERE user_id=?"
                   " ORDER BY started_at DESC LIMIT ?", (user_id, limit))


def public(pt_id, user_id, world_id) -> dict:
    """The run banner: which run this is, and what the last one left behind."""
    run = active(pt_id)
    meta = progress(user_id, world_id)
    return {
        "run_no": run["run_no"] if run else 1,
        "run_active": bool(run),
        "started_at": run["started_at"] if run else "",
        "runs_completed": meta["runs_completed"],
        "deepest_turn": meta["deepest_turn"],
        "unlocks": meta["unlocks"],
        "lore_known": len(meta["known_lore"]),
        "last_echo": meta["echoes"][-1] if meta["echoes"] else "",
        "permadeath": modes.permadeath_on(pt_id),
    }
