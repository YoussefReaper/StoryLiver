"""The competitive profile - scores, seasons, achievements, leaderboards.

The spec's §4, and the retention argument behind it: competition is what brings
people back, spending is a minority behaviour, and retention is what makes the
minority large enough to matter. So this exists to give a player a reason to
open the app on a day they had not planned to.

Three scores, because three families of mode are three different games and one
number would flatten them:

    LEGACY   solo. What the world became because you were in it - places found,
             organisations that outlived you, characters whose arc you caused.
    CO-OP    multiplayer. Shared victories, and trust actually built with the
             people in the world rather than points farmed off them.
    PVP      competitive. Points, and a seasonal rank.

The halal lock, and it is a design constraint not a disclaimer:

    Competitive points are COSMETIC STATUS ONLY. They buy nothing, they unlock
    no power, and they cannot be purchased. Mana is the separate paid token and
    it buys TURNS - more story, never an advantage over another player. There
    is no wagering anywhere in this file and there is no path to any.

Seasons reset POINTS and keep PRESTIGE. That is the standard ladder shape for a
reason: a reset that wiped everything would punish the people who played most,
and a ladder that never reset would be unreachable by anyone who started late.

Profile persistence is gated behind an account (Workstream D). Without one,
everything here still works and is scoped to the session - play is never
blocked to make somebody sign up.
"""
from __future__ import annotations

import json
import math

from . import db, modetree

# ---------------------------------------------------------------------------
# Seasons
# ---------------------------------------------------------------------------
# A season is a calendar quarter. Derived rather than stored so there is no job
# to run and no way for two servers to disagree about which season it is.

def season_of(day: str = "") -> str:
    day = day or db.today()
    year, month = int(day[:4]), int(day[5:7])
    return f"{year}Q{(month - 1) // 3 + 1}"


def season_bounds(season: str) -> tuple:
    year, q = int(season[:4]), int(season[-1])
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    last = (31 if end_month in (1, 3, 5, 7, 8, 10, 12)
            else 30 if end_month != 2 else 29 if year % 4 == 0 else 28)
    return (f"{year}-{start_month:02d}-01", f"{year}-{end_month:02d}-{last}")


# ---------------------------------------------------------------------------
# Tiers - status, and nothing but status
# ---------------------------------------------------------------------------
# The percentage bands are the ones the research names as durable across WoW,
# LoL and CoD: a top band small enough to mean something (~4%) and a second
# band (~6%) that is visibly reachable from below. They are computed from the
# LIVE distribution rather than fixed point thresholds, so a quiet season does
# not hand out top ranks and a busy one does not make them unreachable.

TIERS = (
    ("duelist",   0.04, "Duelist",   "Top 4% this season."),
    ("rival",     0.10, "Rival",     "Top 10% this season."),
    ("contender", 0.25, "Contender", "Top quarter."),
    ("ranked",    0.60, "Ranked",    "Placed."),
    ("unranked",  1.00, "Unranked",  "Not enough matches yet."),
)

# Below this you are simply unranked. A rank off two matches is noise, and
# showing it would make the ladder look random.
PLACEMENT_MATCHES = 5


def tier_for(percentile: float, matches: int) -> dict:
    """`percentile` is 0.0 at the top. Cosmetic - it grants nothing."""
    if matches < PLACEMENT_MATCHES:
        return {"id": "unranked", "label": "Unranked",
                "blurb": f"{PLACEMENT_MATCHES - matches} more to place.",
                "cosmetic": True}
    for tid, cut, label, blurb in TIERS:
        if percentile <= cut:
            return {"id": tid, "label": label, "blurb": blurb, "cosmetic": True}
    return {"id": "ranked", "label": "Ranked", "blurb": "Placed.", "cosmetic": True}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
# What each outcome is worth. Flat and readable on purpose: a player should be
# able to work out where their points came from without a wiki.

PVP_POINTS = {"win": 25, "loss": -8, "draw": 4, "abandon": -15}

# A loss never takes you below zero for the season. Negative points read as
# punishment for playing, which is the opposite of what a ladder is for.
PVP_FLOOR = 0


def _key(account_id, player_id, session_id) -> tuple:
    """Where this profile lives.

    An ACCOUNT is durable across runs and devices. Without one, the profile is
    scoped to the session and dies with it - the spec's rule is that play is
    never blocked, not that anonymous play earns nothing permanent.
    """
    if account_id:
        return ("account", account_id)
    return ("session", f"{session_id}:{player_id}")


def profile(account_id="", player_id="", session_id="", *, season="") -> dict:
    season = season or season_of()
    kind, owner = _key(account_id, player_id, session_id)
    row = db.row("SELECT * FROM profiles WHERE owner_kind=? AND owner_id=?", (kind, owner))
    if not row:
        return {
            "owner_kind": kind, "owner_id": owner, "anonymous": kind == "session",
            "legacy": 0, "coop": 0,
            "pvp": {"season": season, "points": 0, "wins": 0, "losses": 0,
                    "matches": 0, "streak": 0, "prestige": 0,
                    "tier": tier_for(1.0, 0)},
            "achievements": [], "feuds": [],
            "note": ("This profile lives only in this session. Sign in and it "
                     "follows you." if kind == "session" else ""),
        }
    pvp = db.jload(row["pvp"], {}) or {}
    # A season that has rolled over resets POINTS and keeps PRESTIGE. Done on
    # read so there is no scheduled job that can fail to run.
    if pvp.get("season") != season:
        pvp = _roll_season(kind, owner, pvp, season)
    matches = int(pvp.get("matches", 0))
    return {
        "owner_kind": kind, "owner_id": owner, "anonymous": kind == "session",
        "legacy": row["legacy"], "coop": row["coop"],
        "pvp": {**pvp, "tier": tier_for(percentile(owner, kind, pvp.get("points", 0),
                                                   season), matches)},
        "achievements": db.jload(row["achievements"], []) or [],
        "feuds": db.jload(row["feuds"], []) or [],
        "note": ("This profile lives only in this session. Sign in and it "
                 "follows you." if kind == "session" else ""),
    }


def _roll_season(kind, owner, pvp, season) -> dict:
    """Points to zero, prestige up by one band earned. The record of what you
    did last season is kept - a ladder with no history is a ladder nobody is
    attached to."""
    history = pvp.get("history", [])
    if pvp.get("season"):
        history = (history + [{"season": pvp["season"], "points": pvp.get("points", 0),
                               "wins": pvp.get("wins", 0),
                               "matches": pvp.get("matches", 0)}])[-12:]
    fresh = {"season": season, "points": 0, "wins": 0, "losses": 0, "matches": 0,
             "streak": 0, "by_mode": {},
             # Prestige is the thing that survives, and it only ever goes up.
             "prestige": int(pvp.get("prestige", 0)) + (1 if pvp.get("points", 0) >= 200 else 0),
             "history": history}
    db.run("UPDATE profiles SET pvp=?, updated_at=? WHERE owner_kind=? AND owner_id=?",
           (json.dumps(fresh), db.now(), kind, owner))
    return fresh


def _ensure(kind, owner):
    if not db.row("SELECT 1 FROM profiles WHERE owner_kind=? AND owner_id=?", (kind, owner)):
        db.run("INSERT INTO profiles (owner_kind,owner_id,legacy,coop,pvp,achievements,"
               "feuds,created_at,updated_at) VALUES (?,?,0,0,'{}','[]','[]',?,?)",
               (kind, owner, db.now(), db.now()))


def record_match(*, account_id="", player_id="", session_id="", mode, outcome,
                 opponent="", opponent_name="") -> dict:
    """A PvP match ended. Points move; nothing else does.

    Deliberately refuses non-PvP modes rather than silently scoring them: a
    co-op session that quietly fed the competitive ladder would make the ladder
    mean something different from what it says."""
    if outcome not in PVP_POINTS:
        raise ValueError(f"unknown outcome {outcome!r}")
    if modetree.family(mode) != "pvp":
        raise ValueError("only a PvP match moves PvP points")

    kind, owner = _key(account_id, player_id, session_id)
    _ensure(kind, owner)
    season = season_of()
    prof = profile(account_id, player_id, session_id, season=season)
    pvp = {k: v for k, v in prof["pvp"].items() if k != "tier"}

    delta = PVP_POINTS[outcome]
    pvp["points"] = max(PVP_FLOOR, int(pvp.get("points", 0)) + delta)
    pvp["matches"] = int(pvp.get("matches", 0)) + 1
    if outcome == "win":
        pvp["wins"] = int(pvp.get("wins", 0)) + 1
        pvp["streak"] = max(0, int(pvp.get("streak", 0))) + 1
    elif outcome == "loss":
        pvp["losses"] = int(pvp.get("losses", 0)) + 1
        pvp["streak"] = min(0, int(pvp.get("streak", 0))) - 1
    pvp["season"] = season
    # Per-mode tallies, so a Duel board is a Duel board. The leaderboard took
    # a `mode` argument, echoed it back in the response, and never filtered on
    # anything - so every "per-mode" board was the global one wearing a label.
    by_mode = pvp.get("by_mode") or {}
    entry = by_mode.get(mode) or {"points": 0, "wins": 0, "losses": 0, "matches": 0}
    entry["points"] = max(PVP_FLOOR, int(entry["points"]) + delta)
    entry["matches"] = int(entry["matches"]) + 1
    if outcome == "win":
        entry["wins"] = int(entry["wins"]) + 1
    elif outcome == "loss":
        entry["losses"] = int(entry["losses"]) + 1
    by_mode[mode] = entry
    pvp["by_mode"] = by_mode

    db.run("UPDATE profiles SET pvp=?, updated_at=? WHERE owner_kind=? AND owner_id=?",
           (json.dumps(pvp), db.now(), kind, owner))

    feud = None
    if opponent:
        feud = _note_feud(kind, owner, opponent, opponent_name, outcome, mode)

    unlocked = check_achievements(account_id=account_id, player_id=player_id,
                                  session_id=session_id)
    return {"delta": delta, "points": pvp["points"], "outcome": outcome,
            "mode": mode, "season": season, "feud": feud, "unlocked": unlocked,
            "tier": tier_for(percentile(owner, kind, pvp["points"], season),
                             pvp["matches"])}


def add_legacy(points, *, account_id="", player_id="", session_id="", why="") -> dict:
    """Solo world-impact. Earned from what the world became, never from time
    spent - a score that rewards sitting there is a score that rewards nothing."""
    kind, owner = _key(account_id, player_id, session_id)
    _ensure(kind, owner)
    db.run("UPDATE profiles SET legacy=legacy+?, updated_at=? WHERE owner_kind=? AND owner_id=?",
           (int(points), db.now(), kind, owner))
    # Re-check here, not only after a match. Two of the eight trophies are
    # earned by solo and co-op play and could never have unlocked otherwise.
    unlocked = check_achievements(account_id=account_id, player_id=player_id,
                                  session_id=session_id)
    return {"legacy": profile(account_id, player_id, session_id)["legacy"],
            "added": int(points), "why": why, "unlocked": unlocked}


def add_coop(points, *, account_id="", player_id="", session_id="", why="") -> dict:
    kind, owner = _key(account_id, player_id, session_id)
    _ensure(kind, owner)
    db.run("UPDATE profiles SET coop=coop+?, updated_at=? WHERE owner_kind=? AND owner_id=?",
           (int(points), db.now(), kind, owner))
    unlocked = check_achievements(account_id=account_id, player_id=player_id,
                                  session_id=session_id)
    return {"coop": profile(account_id, player_id, session_id)["coop"],
            "added": int(points), "why": why, "unlocked": unlocked}


# ---------------------------------------------------------------------------
# Cross-run feuds - the one thing that DOES follow a player between worlds
# ---------------------------------------------------------------------------
# NPCs do not carry over. People do. This is the moat the spec names: not that
# a character remembers you, but that a PERSON does, and that the record of
# what passed between you is on both your profiles.

def _note_feud(kind, owner, opponent, opponent_name, outcome, mode) -> dict:
    row = db.row("SELECT feuds FROM profiles WHERE owner_kind=? AND owner_id=?",
                 (kind, owner))
    feuds = db.jload(row["feuds"], []) or []
    entry = next((f for f in feuds if f["opponent"] == opponent), None)
    if not entry:
        entry = {"opponent": opponent, "name": opponent_name or opponent,
                 "wins": 0, "losses": 0, "met": 0, "last_mode": mode}
        feuds.append(entry)
    entry["met"] += 1
    entry["last_mode"] = mode
    if outcome == "win":
        entry["wins"] += 1
    elif outcome == "loss":
        entry["losses"] += 1
    entry["standing"] = entry["wins"] - entry["losses"]
    feuds = sorted(feuds, key=lambda f: -f["met"])[:40]
    db.run("UPDATE profiles SET feuds=?, updated_at=? WHERE owner_kind=? AND owner_id=?",
           (json.dumps(feuds), db.now(), kind, owner))
    return entry


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------
# Each is a fact about the profile, checked on read. Stored as a list of ids so
# adding one later cannot retroactively un-earn another.

ACHIEVEMENTS = (
    {"id": "first_blood", "name": "First Blood", "blurb": "Win a PvP match.",
     "test": lambda p: p["pvp"].get("wins", 0) >= 1},
    {"id": "placed", "name": "Placed", "blurb": f"Play {PLACEMENT_MATCHES} ranked matches.",
     "test": lambda p: p["pvp"].get("matches", 0) >= PLACEMENT_MATCHES},
    {"id": "streak_3", "name": "On a Run", "blurb": "Win three in a row.",
     "test": lambda p: p["pvp"].get("streak", 0) >= 3},
    {"id": "veteran", "name": "Veteran", "blurb": "Play twenty matches.",
     "test": lambda p: p["pvp"].get("matches", 0) >= 20},
    {"id": "prestige_1", "name": "Seasoned", "blurb": "Carry prestige out of a season.",
     "test": lambda p: p["pvp"].get("prestige", 0) >= 1},
    {"id": "worldshaper", "name": "Worldshaper", "blurb": "Reach 500 legacy.",
     "test": lambda p: p["legacy"] >= 500},
    {"id": "good_company", "name": "Good Company", "blurb": "Reach 300 co-op.",
     "test": lambda p: p["coop"] >= 300},
    {"id": "nemesis", "name": "Nemesis", "blurb": "Meet the same rival five times.",
     "test": lambda p: any(f["met"] >= 5 for f in p["feuds"])},
)


def check_achievements(*, account_id="", player_id="", session_id="") -> list:
    kind, owner = _key(account_id, player_id, session_id)
    _ensure(kind, owner)
    prof = profile(account_id, player_id, session_id)
    have = set(prof["achievements"])
    fresh = []
    for a in ACHIEVEMENTS:
        if a["id"] in have:
            continue
        try:
            if a["test"](prof):
                fresh.append(a["id"])
        except (KeyError, TypeError):
            continue
    if fresh:
        db.run("UPDATE profiles SET achievements=?, updated_at=? WHERE owner_kind=? AND owner_id=?",
               (json.dumps(sorted(have | set(fresh))), db.now(), kind, owner))
    return [{"id": a["id"], "name": a["name"], "blurb": a["blurb"]}
            for a in ACHIEVEMENTS if a["id"] in fresh]


def trophy_room(account_id="", player_id="", session_id="") -> dict:
    """Everything earned, and everything still out there. Showing the locked
    ones is the point - a trophy case with no empty shelves gives a player
    nothing to come back for."""
    prof = profile(account_id, player_id, session_id)
    have = set(prof["achievements"])
    return {
        "earned": [{"id": a["id"], "name": a["name"], "blurb": a["blurb"], "have": True}
                   for a in ACHIEVEMENTS if a["id"] in have],
        "locked": [{"id": a["id"], "name": a["name"], "blurb": a["blurb"], "have": False}
                   for a in ACHIEVEMENTS if a["id"] not in have],
        "count": len(have), "total": len(ACHIEVEMENTS),
    }


# ---------------------------------------------------------------------------
# Leaderboards
# ---------------------------------------------------------------------------

def percentile(owner, kind, points, season) -> float:
    """Where this score sits in the live distribution. 0.0 is the top."""
    rows = db.rows("SELECT pvp FROM profiles WHERE owner_kind='account'")
    scores = []
    for r in rows:
        p = db.jload(r["pvp"], {}) or {}
        if p.get("season") == season and int(p.get("matches", 0)) >= PLACEMENT_MATCHES:
            scores.append(int(p.get("points", 0)))
    if not scores:
        return 1.0
    above = sum(1 for s in scores if s > points)
    return round(above / len(scores), 4)


def leaderboard(*, board="pvp", mode="", day="", limit=50) -> dict:
    """Global, per-mode, or the daily seed.

    `mode` filters to one sub-mode's own tally rather than decorating the
    global list with a label.

    Only ACCOUNT profiles are listed. A session-scoped score is real to the
    player who earned it and meaningless to everyone else, and putting it on a
    public board would make the board look padded."""
    season = season_of()
    rows = db.rows("SELECT owner_id, legacy, coop, pvp FROM profiles"
                   " WHERE owner_kind='account'")
    entries = []
    for r in rows:
        pvp = db.jload(r["pvp"], {}) or {}
        if board == "pvp":
            if pvp.get("season") != season:
                continue
            if mode:
                # A board for one mode reads that mode's own tally. Absent
                # means they have not played it, which is not a zero - it is
                # not being on this board at all.
                entry = (pvp.get("by_mode") or {}).get(mode)
                if not entry:
                    continue
                value, matches = int(entry.get("points", 0)), int(entry.get("matches", 0))
            else:
                value, matches = int(pvp.get("points", 0)), int(pvp.get("matches", 0))
        elif board == "legacy":
            value, matches = r["legacy"], 0
        elif board == "coop":
            value, matches = r["coop"], 0
        else:
            raise ValueError(f"unknown board {board!r}")
        if value <= 0:
            continue
        entries.append({"owner": r["owner_id"], "value": value, "matches": matches,
                        "prestige": int(pvp.get("prestige", 0))})
    entries.sort(key=lambda e: (-e["value"], e["owner"]))
    total = len(entries)
    for i, e in enumerate(entries):
        e["rank"] = i + 1
        if board == "pvp":
            e["tier"] = tier_for((i / total) if total else 1.0, e["matches"])
    return {"board": board, "season": season if board == "pvp" else "",
            "mode": mode, "day": day, "total": total,
            "entries": entries[:limit],
            "halal_note": "Status only. Points buy nothing and cannot be bought."}


def daily_board(day="", limit=50) -> dict:
    """The Daily Challenge board: everyone played the same world, so the
    comparison is honest in a way no other board is."""
    day = day or db.today()
    rows = db.rows("SELECT * FROM daily_results WHERE day=? ORDER BY score DESC,"
                   " turns ASC, created_at ASC LIMIT ?", (day, limit))
    return {"day": day, "seed": modetree.daily_seed(day),
            "entries": [{"rank": i + 1, "owner": r["owner_id"], "name": r["name"],
                         "score": r["score"], "turns": r["turns"],
                         "at": r["created_at"]} for i, r in enumerate(rows)],
            "total": len(rows)}


def submit_daily(*, owner_id, name, score, turns, day="") -> dict:
    """One result per player per day. A resubmission replaces it only if it is
    better, so a player cannot farm the board by replaying until a bad run
    scrolls off."""
    day = day or db.today()
    existing = db.row("SELECT * FROM daily_results WHERE day=? AND owner_id=?",
                      (day, owner_id))
    if existing and existing["score"] >= score:
        return {"submitted": False, "kept": existing["score"],
                "note": "Your earlier run today was better."}
    db.run("INSERT INTO daily_results (day,owner_id,name,score,turns,created_at)"
           " VALUES (?,?,?,?,?,?)"
           " ON CONFLICT(day,owner_id) DO UPDATE SET score=excluded.score,"
           " turns=excluded.turns, name=excluded.name, created_at=excluded.created_at",
           (day, owner_id, name[:40], int(score), int(turns), db.now()))
    return {"submitted": True, "score": int(score), "day": day}


# What a finished run is worth, per thing the world actually kept. Reading the
# world rather than the clock is the difference between a score that rewards
# playing well and one that rewards leaving the tab open.
LEGACY_WEIGHTS = {
    "places": 8,        # somewhere you found that was hidden
    "lore": 4,          # something you learned and carried out
    "orgs": 40,         # something you founded that outlived the run
    "reach": 15,        # a person who answers to it
    "turned": 12,       # a character whose arc you caused, for good or ill
    "depth": 2,         # per turn reached, deliberately the smallest term
}


def score_run(pt_id, world, *, player, account_id="", session_id="",
              family="solo") -> dict:
    """Settle a finished run into the profile.

    Called once, when a story closes. Everything it counts is a fact about the
    world at the end, so two players who played the same length can score very
    differently - which is the point."""
    from . import legacy as _legacy

    places = len(db.rows("SELECT 1 FROM atlas_places WHERE playthrough_id=?"
                         " AND status!='hidden'", (pt_id,)))
    lore = len(db.rows("SELECT 1 FROM knowledge WHERE playthrough_id=?"
                       " AND holder_kind='player' AND holder_id=?", (pt_id, player)))
    orgs = _legacy.orgs_led(pt_id, world, player)
    reach = sum(o["reach"] for o in orgs)
    turned = len([d for d in _legacy.drift(pt_id, world, player, only_turned=True)])
    turn = (db.row("SELECT current_turn FROM playthroughs WHERE id=?",
                   (pt_id,)) or {}).get("current_turn", 0)

    parts = {
        "places found": places * LEGACY_WEIGHTS["places"],
        "things learned": lore * LEGACY_WEIGHTS["lore"],
        "organisations founded": len(orgs) * LEGACY_WEIGHTS["orgs"],
        "people who answered you": reach * LEGACY_WEIGHTS["reach"],
        "arcs you caused": turned * LEGACY_WEIGHTS["turned"],
        "how deep you got": turn * LEGACY_WEIGHTS["depth"],
    }
    total = int(sum(parts.values()))
    if family == "multiplayer":
        out = add_coop(total, account_id=account_id, player_id=player,
                       session_id=session_id, why="a run finished together")
    else:
        out = add_legacy(total, account_id=account_id, player_id=player,
                         session_id=session_id, why="a run finished")
    return {"added": total, "parts": parts, "board": family, **out}


def public(account_id="", player_id="", session_id="") -> dict:
    prof = profile(account_id, player_id, session_id)
    return {**prof, "trophies": trophy_room(account_id, player_id, session_id),
            "season": season_of(), "season_ends": season_bounds(season_of())[1],
            "tiers": [{"id": t, "label": label, "blurb": blurb, "cosmetic": True}
                      for t, _, label, blurb in TIERS],
            "halal_note": ("Competitive standing is cosmetic. Mana is a separate "
                           "token and buys turns, never an advantage.")}
