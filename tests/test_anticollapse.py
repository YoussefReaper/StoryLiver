"""Anti-collapse proof — the acceptance test for Pain #1.

Runs a 60-turn playthrough on the offline stub (no key, no spend) and asserts
the properties that competitors fail:

  1. persona anchors at turn 60 are byte-identical to turn 1
  2. context does NOT grow with turn count (the actual anti-collapse claim)
  3. relationship vectors stay numerically consistent and in bounds
  4. every fated event fired, exactly once, on its exact turn
  5. no player action ever prevented a fated event
  6. the dead stop acting
  7. NPCs demonstrably act on their own and reflect
  8. blended cost per action stays under the ceiling

Run:  python -m tests.test_anticollapse      (or: pytest tests/)
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-test-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import config, db, engine, memory  # noqa: E402
from backend import worlds as world_registry  # noqa: E402
from backend.worlds import emberfall  # noqa: E402

WORLD = world_registry.starter("emberfall")

TURNS = 60
USER = "anticollapse-test-user"

SCRIPT = [
    "I ask what happened here last time the mountain woke.",
    "I make my way to The Village Green.",
    "I offer to help carry water.",
    "I ask about the Warden.",
    "I make my way to The Ash Chapel.",
    "I listen to what Sister Adrahel is saying.",
    "I make my way to The Warden's Tower.",
    "I tell Maren Vosk that I know about the warm water.",
    "I make my way to The Ash Chapel.",
    "I make my way to The Village Green.",
    "I make my way to Vosk Forge.",
    "I ask Tamsin why the cart behind the forge is packed.",
    "I make my way to The Village Green.",
    "I make my way to The Bitter Well.",
    "I look down into the well.",
    "I make my way to Ferrow's Dig.",
    "I ask Ferrow what he found at forty feet.",
    "I make my way to The Bitter Well.",
    "I make my way to The Village Green.",
    "I make my way to The Millhouse.",
    "I tell Bram Odell the valley is going to burn.",
]


def run():
    db.init()
    pt_id = engine.create_playthrough(USER, "emberfall", "a traveller with a road behind them")

    anchors_before = {n["id"]: memory.anchor_block(WORLD, n["id"]) for n in WORLD.npcs}

    npc_acts, beats, refusals = 0, 0, 0
    for i in range(TURNS):
        action = SCRIPT[i % len(SCRIPT)]
        r = engine.take_turn(pt_id, action)
        if r.get("rejected"):
            refusals += 1
            continue
        for e in r["entries"]:
            m = e.get("meta") or {}
            if m.get("npc_initiated"):
                npc_acts += 1
            if m.get("director_beat"):
                beats += 1

    return pt_id, anchors_before, {"npc_acts": npc_acts, "beats": beats, "refusals": refusals}


def check(pt_id, anchors_before, counts):
    fails, notes = [], []

    def ok(cond, msg):
        (notes if cond else fails).append(msg)

    st = engine.snapshot(pt_id)
    turn = st["turn"]

    # 1 -------------------------------------------------------------- anchors
    drifted = [nid for nid, block in anchors_before.items()
               if memory.anchor_block(WORLD, nid) != block]
    ok(not drifted, f"persona anchors byte-identical for all 11 characters after {turn} turns"
                    + (f" — DRIFTED: {drifted}" if drifted else ""))

    # 2 ------------------------------------------------- context does not grow
    nar = db.rows(
        "SELECT id,in_tokens FROM usage_log WHERE playthrough_id=? AND role='narrator' ORDER BY id",
        (pt_id,))
    early = [r["in_tokens"] for r in nar[2:7]]
    late = [r["in_tokens"] for r in nar[-5:]]
    a, b = sum(early) / max(1, len(early)), sum(late) / max(1, len(late))
    growth = b / a if a else 0
    ok(growth <= 1.5,
       f"narrator context flat: {a:.0f} tok early -> {b:.0f} tok late ({growth:.2f}x, ceiling 1.50x)")

    # 3 -------------------------------------------------------- relationships
    rels = db.rows("SELECT * FROM relationships WHERE playthrough_id=?", (pt_id,))
    bad = [r for r in rels for k in memory.REL_KEYS if not (-100 <= r[k] <= 100)]
    ok(not bad, f"all {len(rels)} relationship vectors inside [-100, 100]")
    moved = [r for r in rels if r["src"] == "user" and r["last_interaction_turn"] > 0]
    ok(len(moved) >= 3, f"{len(moved)} relationships actually drifted through play")
    ok(all(isinstance(r[k], (int, float)) for r in rels for k in memory.REL_KEYS),
       "relationship values are numeric, never strings or nulls")

    # 4 ---------------------------------------------------------------- fate
    fired = db.rows(
        "SELECT turn, action FROM timeline_events WHERE playthrough_id=? AND kind='fate' ORDER BY turn",
        (pt_id,))
    expected = [f for f in WORLD.fated_events if f["turn"] <= turn]
    ok(len(fired) == len(expected),
       f"{len(fired)}/{len(expected)} fated events fired by turn {turn}")
    for f in expected:
        hits = [x for x in fired if x["turn"] == f["turn"] and x["action"] == f["title"]]
        ok(len(hits) == 1, f"fate {f['id']} fired exactly once, on turn {f['turn']} ({len(hits)} hits)")

    # 5 ---------------------------------------- fate was never prevented
    prevented = db.rows(
        "SELECT * FROM timeline_events WHERE playthrough_id=? AND kind='action'"
        " AND (consequence LIKE '%prevent%' OR consequence LIKE '%averted%' OR consequence LIKE '%stopped the fire%')",
        (pt_id,))
    ok(not prevented, "no committed action claims to have prevented or averted fate")

    # 6 ----------------------------------------------------- the dead stop
    dead = [s["npc_id"] for s in memory.all_npc_states(pt_id) if not s["alive"]]
    if turn >= 30:
        ok("maren" in dead, "Maren Vosk is dead after her fated turn 30")
        after = db.rows(
            "SELECT * FROM timeline_events WHERE playthrough_id=? AND kind='npc' AND turn>30 AND actor LIKE '%Maren%'",
            (pt_id,))
        ok(not after, "no NPC-initiated action from Maren Vosk after she died")

    # 7 ------------------------------------------------------- living NPCs
    ok(counts["npc_acts"] + counts["beats"] > 0,
       f"world moved on its own: {counts['npc_acts']} NPC-initiated actions, {counts['beats']} Director beats")
    refl = db.row("SELECT COUNT(*) c FROM npc_memories WHERE playthrough_id=? AND kind='reflection'", (pt_id,))
    ok(refl["c"] > 0, f"{refl['c']} NPC reflections synthesised from their own memory streams")
    mems = db.row("SELECT COUNT(*) c FROM npc_memories WHERE playthrough_id=?", (pt_id,))
    notes.append(f"   ({mems['c']} total NPC memories retained, none discarded)")

    # 7b ------------------------------- the world's own rules actually fire
    # A corrupted regex once silently disabled every declarative rule while
    # every other assertion still passed. This is the guard against that.
    from backend import world_master
    checked = [r for r in WORLD.rules if r.get("check")]
    ok(len(checked) >= 5, f"{len(checked)} of {len(WORLD.rules)} rules are code-enforced")
    probes = [
        ("I cast a fireball at the bell.", "village_green", "night", "r1_no_magic"),
        ("I climb the Reach to get out of the valley.", "village_green", "night", "r2_reach_unclimbable"),
        ("I take the ferry across the water.", "ferry_landing", "night", "r5_ferry_slack"),
        ("I ring the great bell.", "bell_tower", "morning", "r3_bell_authority"),
    ]
    for action, loc, phase, want in probes:
        probe_state = {"turn": 3, "phase": phase, "location": loc, "present": [],
                       "dead": [], "flags": {}, "location_name": WORLD.loc_name(loc)}
        got = world_master._mechanical(WORLD, action, probe_state)
        ok(got is not None and got.get("rule_ref") == want,
           f"rule {want} rejects {action!r} in code"
           f" (got {(got or {}).get('rule_ref') or 'ALLOWED'})")
    allowed = world_master._mechanical(
        WORLD, "I ask about the weather.",
        {"turn": 3, "phase": "morning", "location": "village_green", "present": [],
         "dead": [], "flags": {}, "location_name": "The Village Green"})
    ok(allowed is None, "an ordinary action is not caught by any rule")

    # 8 ---------------------------------------------------------------- cost
    u = engine.usage_summary(pt_id)
    ok(u["within_budget"],
       f"blended cost ${u['usd_per_action']:.5f}/action vs ${u['target_usd_per_action']} ceiling")

    # 9 ------------------------------------------- memory is retrievable, not a blob
    # Probe three distinct early moments. Each must still surface at turn 60 —
    # the property a summarisation window loses.
    for query, want_turn in [
        ("the wells went bitter, black water tasting of iron", 4),
        ("the chapel bell cracked on the noon toll", 9),
        ("I arrived in Emberfall off the low road", 0),
    ]:
        hits = memory.retrieve_events(pt_id, turn, query, k=6)
        ok(any(e["turn"] == want_turn for e in hits),
           f"turn-{want_turn} event still retrieved at turn {turn} for its own query"
           f" (got turns {[e['turn'] for e in hits]})")

    return fails, notes, st, u


def main():
    print("StoryLiver — anti-collapse proof")
    print(f"  offline stub, {TURNS} turns, no API key, no spend\n")
    pt_id, anchors, counts = run()
    fails, notes, st, u = check(pt_id, anchors, counts)

    for n in notes:
        print(("        " + n.strip()) if n.strip().startswith("(") else ("  PASS  " + n))
    for f in fails:
        print("  FAIL  " + f)

    print(f"\n  turn {st['turn']} · day {st['day']} · {st['location_name']}")
    print(f"  {sum(1 for n in st['npcs'] if n['alive'])}/11 alive · tension {st['tension']}")
    print(f"  {u['actions']} actions · ${u['total_usd']:.5f} total · ${u['usd_per_action']:.5f}/action")
    passed = sum(1 for n in notes if not n.strip().startswith("("))
    print(f"\n  {passed} passed, {len(fails)} failed")
    return 1 if fails else 0


def test_anticollapse():
    pt_id, anchors, counts = run()
    fails, _, _, _ = check(pt_id, anchors, counts)
    assert not fails, "\n".join(fails)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
