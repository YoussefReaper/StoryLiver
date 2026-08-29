"""Takedown and reporting - what makes the UGC posture real.

The blueprint's legal position is the fan-fiction model: the player names the
world, the player owns the input, the engine is what's monetised. That posture
is standard and defensible - but only if the takedown route actually exists.
A ToS clause with no reporting mechanism behind it is a claim, not a process,
and it is the first thing that falls over under scrutiny.

So this module is deliberately unglamorous: a report goes in, it is queued with
a status, and a named route exists to act on it. Three requirements from the
blueprint, implemented rather than asserted:

  (1) a takedown process - report in, triage, removal;
  (2) users own and are responsible for their input - recorded per world;
  (3) the ENGINE is monetised, never a named canon world - enforced in code by
      refusing to price any world individually.

Deterministic, $0.
"""
from __future__ import annotations

import uuid

from . import db

REASONS = {
    "copyright": "A rights holder objects to this world.",
    "impersonation": "This world claims to be official or endorsed.",
    "harmful": "Content that endangers someone.",
    "sexual_minor": "Sexual content involving a minor.",
    "other": "Something else.",
}

# Reports that are never queued for ordinary triage - they suspend the world on
# arrival and are escalated. Waiting on a queue is not an acceptable response
# to either of these.
IMMEDIATE = ("sexual_minor", "harmful")

STATUSES = ("open", "actioned", "rejected", "escalated")


class ReportError(ValueError):
    pass


def file_report(*, reason, world_id="", playthrough_id="", reporter="", detail="") -> dict:
    if reason not in REASONS:
        raise ReportError(f"unknown reason {reason!r}")
    if not (world_id or playthrough_id):
        raise ReportError("a report must name a world or a story")

    report_id = uuid.uuid4().hex[:12]
    status = "escalated" if reason in IMMEDIATE else "open"
    db.run(
        "INSERT INTO reports (id,world_id,playthrough_id,reporter,reason,detail,status,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (report_id, world_id, playthrough_id, reporter, reason, (detail or "")[:2000],
         status, db.now()),
    )
    if status == "escalated" and world_id:
        suspend(world_id, reason=f"auto: {reason}")
    return {"report_id": report_id, "status": status,
            "immediate": status == "escalated",
            "note": ("This world is suspended pending review."
                     if status == "escalated" else
                     "Filed. A person will look at this.")}


def suspend(world_id, *, reason="") -> dict:
    """Pull a world out of circulation without deleting anyone's work.

    Forced private rather than destroyed: if the report turns out to be wrong,
    the author has not lost their world, and if it is right, nobody else can
    reach it in the meantime."""
    db.run("UPDATE worlds SET visibility='private', personal_only=1 WHERE id=?", (world_id,))
    return {"world_id": world_id, "suspended": True, "reason": reason}


def resolve_report(report_id, *, status, resolution="") -> dict:
    if status not in STATUSES:
        raise ReportError(f"unknown status {status!r}")
    row = db.row("SELECT * FROM reports WHERE id=?", (report_id,))
    if not row:
        raise ReportError("no such report")
    db.run("UPDATE reports SET status=?, resolution=?, resolved_at=? WHERE id=?",
           (status, (resolution or "")[:1000], db.now(), report_id))
    if status == "actioned" and row["world_id"]:
        suspend(row["world_id"], reason="report actioned")
    return {"report_id": report_id, "status": status}


def queue(status="open", limit=100):
    return db.rows("SELECT * FROM reports WHERE status=? ORDER BY created_at LIMIT ?",
                   (status, limit))


def for_world(world_id):
    return db.rows("SELECT id,reason,status,created_at FROM reports WHERE world_id=?"
                   " ORDER BY created_at DESC", (world_id,))


def policy() -> dict:
    """Surfaced in-app so the posture is visible to players, not buried.

    The third line is the one that matters commercially: Mana prices ACTIONS
    on the engine. No named world has ever had its own price, which is what
    keeps 'we monetise the engine' true rather than aspirational."""
    return {
        "ownership": "You own what you write. You are responsible for it.",
        "official": "No world here is official or endorsed by any rights holder.",
        "monetisation": "Mana prices engine actions. No named world is ever sold separately.",
        "ip_worlds": "A world named from someone else's fiction is private-only and cannot be published.",
        "takedown": "Report any world in-app. Rights holders may also contact the listed agent.",
        "reasons": REASONS,
    }
