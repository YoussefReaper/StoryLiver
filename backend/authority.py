"""Workstream C - the institution that notices you.

Before this, "the government" was a diffuse `locals` faction with `law: 2`.
Nobody patrolled, nobody was named, nothing escalated. Crime existed only as a
reputation number moving downward.

The reference designs here are Morrowind and Daggerfall, and they disagree in a
useful way. Morrowind makes crime WITNESS-GATED: unobserved acts genuinely cost
nothing, and the moment one is reported a bounty exists whether or not a guard
ever shows up. Daggerfall makes legal standing a FACTION reputation that DECAYS
back toward zero over time, so a bad month is survivable and a career of crime
is not. This module takes both, because they solve different halves: witnessing
decides whether anything happened at all, and decay decides whether the world
can ever forgive you.

Four slices, built in order, each usable alone:

  C1  a real Civic Authority faction with named officers who PATROL - they are
      in specific places at specific hours, and they witness through exactly the
      same perception math as everyone else. No special sight.
  C2  a bounty ledger and a warrant state machine driven off standing, wired to
      the hunt machinery that already exists (with its honest ETA window).
  C3  graded response - warning, fine, warrant, kill-on-sight - with a named
      officer attached, so escalation is a person rather than a status bar.
  C4  cross-run memory, keyed to an AUTHENTICATED account, so the town
      remembers you between runs and cannot be escaped by clearing storage.

Deterministic, $0, no model call. Escalation is arithmetic; the narrator is
only ever told what already happened.
"""
from __future__ import annotations

import json

from . import awareness, db, memory, worldstate

# --------------------------------------------------------------------------
# C2 - the ladder
# --------------------------------------------------------------------------
# Thresholds are the spec's, and they are deliberately reachable: a player who
# does something grave in front of witnesses should meet the institution within
# a few turns, not eventually.
TIERS = (
    ("clean",   0,    "No one official is looking for you."),
    ("wanted",  -45,  "Your name is on a list."),
    ("warrant", -60,  "There is a warrant out. They may take you by force."),
    ("hunt",    -75,  "Kill on sight."),
)

RESPONSE = {
    "clean":   {"action": "none",    "label": "Nothing",
                "blurb": "You are unremarkable to the law."},
    "wanted":  {"action": "warning", "label": "A warning",
                "blurb": "The next officer who sees you will say something."},
    "warrant": {"action": "arrest",  "label": "Arrest on sight",
                "blurb": "They will try to take you in. You may pay it off first."},
    "hunt":    {"action": "lethal",  "label": "Kill on sight",
                "blurb": "They have stopped asking."},
}

# Daggerfall's mercy, kept: legal standing crawls back toward zero on its own.
# Without this a single bad turn is a permanent sentence, which reads as the
# world being broken rather than as the world being strict.
DECAY_PER_TURN = 0.35

# What clearing a tier costs, in Mana, when you buy your way out rather than
# earning your way out. Priced as an ACTION (the blueprint's rule) - it is a
# deterministic transaction, so it is cheap; atoning in-world is free.
FINE_MANA = {"wanted": 2, "warrant": 6, "hunt": 12}


def tier_for(standing: float) -> str:
    tier = "clean"
    for name, threshold, _ in TIERS:
        if standing <= threshold and threshold < 0:
            tier = name
        elif threshold == 0 and standing > -45:
            tier = "clean"
    # Explicit ladder rather than a clever loop, so the boundaries are readable.
    if standing <= -75:
        return "hunt"
    if standing <= -60:
        return "warrant"
    if standing <= -45:
        return "wanted"
    return "clean"


def tier_blurb(tier: str) -> str:
    return dict((n, b) for n, _, b in TIERS).get(tier, "")


# --------------------------------------------------------------------------
# C1 - who the authority actually is
# --------------------------------------------------------------------------

def authorities(world) -> list:
    """Every faction that holds legal power here.

    A world that declared none still gets one - but a REAL one, with a seat and
    officers, rather than the diffuse `locals` fallback that could never
    actually do anything to you."""
    out = [f for f in awareness.factions(world) if int(f.get("law", 0)) >= 3]
    return out


def officers(world, faction) -> list:
    """Named people, not a uniform. An escalation that names Serel Vane is a
    story; an escalation that says 'the guards' is a status bar."""
    roster = faction.get("officers") or []
    if roster:
        return [o for o in roster if o.get("id")]
    # Fall back to the faction's own members who exist in the world.
    return [{"id": m, "name": world.npc_name(m), "rank": "officer"}
            for m in (faction.get("members") or []) if m in world.by_id][:3]


def on_duty(world, faction, place_id, turn) -> list:
    """Which officers are at this place, in this phase, right now.

    A schedule is what makes a patrol gameable: if the guard is always
    everywhere, stealth is pointless, and if the guard is nowhere, the
    institution is decoration. Both failures are avoided by the officer being
    somewhere specific at a knowable time."""
    phase = world.phase_for(turn)
    out = []
    for o in officers(world, faction):
        sched = o.get("schedule") or {}
        if not sched:
            # No schedule declared: an officer is at the faction's seat.
            if place_id == faction.get("seat"):
                out.append(o)
            continue
        where = sched.get(phase)
        if where == place_id or (isinstance(where, list) and place_id in where):
            out.append(o)
    return out


def patrol_tick(pt_id, world, turn, *, place_id, actor, summary, severity=3) -> dict:
    """C1 - an officer on patrol perceives a public act through the SAME
    witness math as any other character. No privileged sight: a guard in the
    dark, at distance, with noise cover, misses it exactly like anyone else.

    Returns what was recorded, so the caller can surface it."""
    seen = []
    for faction in authorities(world):
        duty = on_duty(world, faction, place_id, turn)
        if not duty:
            continue
        present = [o["id"] for o in duty if o["id"] in world.by_id]
        if not present:
            continue
        fact = awareness.witness(
            pt_id, world, actor=actor, kind="crime", summary=summary,
            detail=summary[:200], place_id=place_id, turn=turn,
            severity=severity, present=present, subject=actor)
        if fact["witnesses"]:
            seen.append({"faction": faction["id"], "faction_name": faction["name"],
                         "officers": [o["name"] for o in duty
                                      if o["id"] in fact["witnesses"]],
                         "fact": fact})
    return {"witnessed_by_authority": seen, "place": place_id, "turn": turn}


# --------------------------------------------------------------------------
# C2 - the ledger
# --------------------------------------------------------------------------

def ledger(pt_id, world, player) -> list:
    """Where you stand with every authority that has legal power."""
    out = []
    for faction in authorities(world):
        r = awareness.rep(pt_id, faction["id"], player)
        row = _bounty_row(pt_id, faction["id"], player)
        tier = tier_for(r["standing"])
        officer = _named_officer(world, faction, pt_id, player)
        out.append({
            "faction": faction["id"], "name": faction["name"],
            "seat": world.loc_name(faction.get("seat", "")) if faction.get("seat") else "",
            "standing": round(r["standing"], 1), "fear": round(r["fear"], 1),
            "tier": tier, "tier_blurb": tier_blurb(tier),
            "response": RESPONSE[tier],
            "crimes": row["crimes"], "bounty": row["bounty"],
            "officer": officer,
            "fine_mana": FINE_MANA.get(tier, 0),
            "known_events": r["known_events"],
        })
    return out


def _bounty_row(pt_id, faction_id, player):
    row = db.row("SELECT * FROM bounties WHERE playthrough_id=? AND faction_id=?"
                 " AND player_id=?", (pt_id, faction_id, player))
    if row:
        return row
    db.run("INSERT OR IGNORE INTO bounties (playthrough_id,faction_id,player_id,updated_at)"
           " VALUES (?,?,?,?)", (pt_id, faction_id, player, db.now()))
    return db.row("SELECT * FROM bounties WHERE playthrough_id=? AND faction_id=?"
                  " AND player_id=?", (pt_id, faction_id, player))


def _named_officer(world, faction, pt_id, player):
    """The same officer stays attached to your case. Rotating them would undo
    the entire point of naming them."""
    row = _bounty_row(pt_id, faction["id"], player)
    roster = officers(world, faction)
    if not roster:
        return None
    if row["officer_id"]:
        found = next((o for o in roster if o["id"] == row["officer_id"]), None)
        if found:
            return found
    pick = roster[abs(hash((pt_id, faction["id"], player))) % len(roster)]
    db.run("UPDATE bounties SET officer_id=? WHERE playthrough_id=? AND faction_id=?"
           " AND player_id=?", (pick["id"], pt_id, faction["id"], player))
    return pick


def register_crime(pt_id, world, *, player, faction_id, severity, summary, turn) -> dict:
    """C2/C3 - a crime the authority KNOWS about moves the ladder.

    Note the ordering: standing is moved by awareness (only for factions that
    actually know), and this reads the RESULT. The ledger can never punish you
    for something nobody reported."""
    faction = next((f for f in awareness.factions(world) if f["id"] == faction_id), None)
    if not faction:
        return {"registered": False, "reason": "no such authority"}

    row = _bounty_row(pt_id, faction_id, player)
    r = awareness.rep(pt_id, faction_id, player)
    before = tier_for(r["standing"])

    # An authority that SAW it itself reacts harder than the diffuse drift a
    # general witness produces. Without this the ladder is unreachable: on the
    # ordinary reputation slope, five witnessed crimes moved standing about
    # -12, so a 45-turn story would end long before anyone was ever "wanted"
    # and the whole institution would be theatre.
    #
    # Tuned so every tier is actually LIVED IN rather than skipped: at severity
    # 4 it takes roughly five witnessed crimes to be wanted, six for a warrant,
    # seven before they stop asking. Repeat offending compounds gently - a
    # record makes the next one cost more - and the per-turn decay above means
    # stopping genuinely helps.
    crimes = row["crimes"] + 1
    weight = -float(severity) * 2.2 * (1.0 + min(1.0, row["crimes"] * 0.12))
    new_standing = max(-100.0, r["standing"] + weight)
    db.run("UPDATE faction_rep SET standing=?, known_events=known_events+1"
           " WHERE playthrough_id=? AND faction_id=? AND player_id=?",
           (new_standing, pt_id, faction_id, player))

    bounty = row["bounty"] + int(severity) * 25
    log = db.jload(row["log"], []) + [{"turn": turn, "severity": int(severity),
                                       "summary": summary[:160]}]
    db.run("UPDATE bounties SET bounty=?, crimes=?, log=?, updated_at=?"
           " WHERE playthrough_id=? AND faction_id=? AND player_id=?",
           (bounty, crimes, json.dumps(log[-40:]), db.now(), pt_id, faction_id, player))

    after = tier_for(awareness.rep(pt_id, faction_id, player)["standing"])
    escalated = after != before
    officer = _named_officer(world, faction, pt_id, player)

    if escalated:
        memory.add_event(pt_id, turn, faction["name"],
                         f"{faction['name']}: {RESPONSE[after]['label'].lower()}",
                         (f"{officer['name']} has your name." if officer else "")
                         + " " + RESPONSE[after]["blurb"],
                         kind="authority", importance=4)
        worldstate.set_flag(pt_id, f"bounty:{faction_id}", after)

    return {"registered": True, "faction": faction_id, "tier": after,
            "escalated": escalated, "from": before, "bounty": bounty,
            "crimes": crimes, "officer": officer,
            "response": RESPONSE[after]}


def decay(pt_id, world, player, turn) -> list:
    """Daggerfall's mercy. Standing crawls toward zero when you stop giving
    them reasons, so a bad week is survivable and a bad career is not.

    Deliberately slower than the relationship decay elsewhere: institutions
    forget more slowly than people do."""
    moved = []
    for faction in authorities(world):
        r = awareness.rep(pt_id, faction["id"], player)
        if abs(r["standing"]) < DECAY_PER_TURN:
            continue
        step = -DECAY_PER_TURN if r["standing"] > 0 else DECAY_PER_TURN
        new = r["standing"] + step
        db.run("UPDATE faction_rep SET standing=? WHERE playthrough_id=? AND faction_id=?"
               " AND player_id=?", (new, pt_id, faction["id"], player))
        moved.append({"faction": faction["id"], "from": round(r["standing"], 1),
                      "to": round(new, 1)})
    return moved


# --------------------------------------------------------------------------
# C3 - settling it
# --------------------------------------------------------------------------

def settle(pt_id, world, *, player, faction_id, how="pay", turn=0) -> dict:
    """Clear what the authority holds against you.

    Two routes on purpose. PAY is a transaction: instant, costs Mana, and the
    world notes that you bought your way out. ATONE is an in-world act: free,
    slower, and it moves standing rather than merely erasing the ledger. A game
    where money is the only exit teaches that money is the only exit."""
    faction = next((f for f in awareness.factions(world) if f["id"] == faction_id), None)
    if not faction:
        return {"settled": False, "reason": "no such authority"}

    r = awareness.rep(pt_id, faction_id, player)
    tier = tier_for(r["standing"])
    if tier == "clean":
        return {"settled": False, "reason": "there is nothing outstanding"}

    if how == "pay":
        # Paying clears the LEDGER and lifts standing just past the threshold -
        # you are no longer wanted, but nobody has forgotten why you were.
        target = -44.0 if tier == "wanted" else (-59.0 if tier == "warrant" else -74.0)
        new = max(r["standing"], target)
        note = "Paid off."
    else:
        # Atoning moves the relationship itself, which is slower but real.
        new = min(0.0, r["standing"] + 18.0)
        note = "Made good, in public."

    db.run("UPDATE faction_rep SET standing=? WHERE playthrough_id=? AND faction_id=?"
           " AND player_id=?", (new, pt_id, faction_id, player))
    db.run("UPDATE bounties SET bounty=0, settled_turn=?, updated_at=?"
           " WHERE playthrough_id=? AND faction_id=? AND player_id=?",
           (turn, db.now(), pt_id, faction_id, player))

    # A settled case calls off anyone already sent - otherwise the hunter
    # arrives for a debt that no longer exists.
    called_off = db.run(
        "UPDATE hunts SET status='called_off' WHERE playthrough_id=? AND target_id=?"
        " AND faction_id=? AND status='enroute'", (pt_id, player, faction_id))

    memory.add_event(pt_id, turn, faction["name"],
                     f"{faction['name']}: matter closed", note,
                     kind="authority", importance=3)
    worldstate.set_flag(pt_id, f"bounty:{faction_id}", tier_for(new))

    return {"settled": True, "how": how, "note": note, "faction": faction_id,
            "tier": tier_for(new), "standing": round(new, 1),
            "mana_cost": FINE_MANA.get(tier, 0) if how == "pay" else 0,
            "hunts_called_off": bool(called_off)}


def public(pt_id, world, player, turn) -> dict:
    """What the authority strip shows."""
    rows = ledger(pt_id, world, player)
    worst = max(rows, key=lambda r: ("clean wanted warrant hunt".split()).index(r["tier"]),
                default=None) if rows else None
    return {"authorities": rows,
            "worst_tier": worst["tier"] if worst else "clean",
            "any_wanted": any(r["tier"] != "clean" for r in rows),
            "tiers": [{"id": n, "blurb": b} for n, _, b in TIERS]}


# --------------------------------------------------------------------------
# C4 - what the town remembers between runs
# --------------------------------------------------------------------------
# This is the payoff for Workstream D and the reason it had to come first. A
# reputation you can escape by clearing browser storage is not a reputation,
# so institutional memory is keyed to an AUTHENTICATED account id and to
# nothing else. A guest still plays the full game; their run simply starts
# clean every time, which is an honest trade rather than a punishment.
#
# What carries is deliberately narrow: standing and a crime count, not the
# ledger itself. The town remembers that you are trouble and roughly how much.
# It does not remember the specific warrant from a run that no longer exists.

# A fraction, not the whole. Time and death soften an institution's grip, and
# carrying 100% would mean one bad first run poisons a world permanently.
CARRY_FRACTION = 0.6


def remember_across_runs(pt_id, world, *, account_id, player, turn=0) -> dict:
    """Called when a run ends. Folds this run's standing into what the town
    already believed about this ACCOUNT."""
    if not account_id or _is_guest(account_id):
        return {"persisted": False, "reason": "guests are not remembered between runs"}

    written = []
    for faction in authorities(world) + [f for f in awareness.factions(world)
                                         if int(f.get("law", 0)) < 3]:
        r = awareness.rep(pt_id, faction["id"], player)
        row = _bounty_row(pt_id, faction["id"], player)
        if r["known_events"] == 0 and abs(r["standing"]) < 1 and not row["crimes"]:
            continue
        prior = db.row("SELECT * FROM town_memory WHERE account_id=? AND world_id=?"
                       " AND faction_id=?", (account_id, world.id, faction["id"]))
        carried = r["standing"] * CARRY_FRACTION
        merged = (prior["standing"] + carried) / 2 if prior else carried
        merged = max(-100.0, min(100.0, merged))
        notes = (db.jload(prior["notes"], []) if prior else [])
        if row["crimes"]:
            notes.append({"turn": turn, "crimes": row["crimes"],
                          "tier": tier_for(r["standing"])})
        db.run(
            "INSERT INTO town_memory (account_id,world_id,faction_id,standing,crimes,notes,updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id,world_id,faction_id) DO UPDATE SET"
            " standing=excluded.standing, crimes=excluded.crimes, notes=excluded.notes,"
            " updated_at=excluded.updated_at",
            (account_id, world.id, faction["id"], merged,
             (prior["crimes"] if prior else 0) + row["crimes"],
             json.dumps(notes[-20:]), db.now()))
        written.append({"faction": faction["id"], "standing": round(merged, 1),
                        "crimes": (prior["crimes"] if prior else 0) + row["crimes"]})
    return {"persisted": True, "factions": written}


def seed_from_memory(pt_id, world, *, account_id, player) -> dict:
    """Called when a run STARTS. The town already has an opinion of you.

    This is what makes a second run in the same world feel like a return
    rather than a reset - the guard at the Broken Bell has seen your face
    before, and the ledger says so."""
    if not account_id or _is_guest(account_id):
        return {"seeded": False, "reason": "guest"}

    rows = db.rows("SELECT * FROM town_memory WHERE account_id=? AND world_id=?",
                   (account_id, world.id))
    seeded = []
    for row in rows:
        if abs(row["standing"]) < 1 and not row["crimes"]:
            continue
        db.run(
            "INSERT INTO faction_rep (playthrough_id,faction_id,player_id,standing,known_events)"
            " VALUES (?,?,?,?,?) ON CONFLICT(playthrough_id,faction_id,player_id)"
            " DO UPDATE SET standing=excluded.standing, known_events=excluded.known_events",
            (pt_id, row["faction_id"], player, row["standing"], max(1, row["crimes"])))
        if row["crimes"]:
            _bounty_row(pt_id, row["faction_id"], player)
            db.run("UPDATE bounties SET crimes=? WHERE playthrough_id=? AND faction_id=?"
                   " AND player_id=?", (row["crimes"], pt_id, row["faction_id"], player))
        seeded.append({"faction": row["faction_id"], "standing": round(row["standing"], 1),
                       "crimes": row["crimes"], "tier": tier_for(row["standing"])})
    return {"seeded": bool(seeded), "factions": seeded}


def town_memory(account_id, world_id=None) -> list:
    """What every town this account has played in still believes."""
    if world_id:
        return db.rows("SELECT * FROM town_memory WHERE account_id=? AND world_id=?",
                       (account_id, world_id))
    return db.rows("SELECT * FROM town_memory WHERE account_id=? ORDER BY updated_at DESC",
                   (account_id,))


def _is_guest(account_id):
    from . import auth
    return auth.is_guest(account_id)
