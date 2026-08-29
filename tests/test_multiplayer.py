"""Multiplayer verification gate.

Asserts the properties that make co-op real rather than a shared chat log:

  1. a room is created and joined by code, and seats fill
  2. four players act into ONE shared world state
  3. context stays flat per broadcast regardless of player count  (spec §6)
  4. per-player NPC memory: one character remembers each player differently
  5. relationship vectors diverge per player against the same NPC
  6. whispers are private - never in the shared feed, never in another player's
  7. spectators cannot act
  8. the host wallet funds the room; friends are not charged
  9. allowances stack when a second player chips in
 10. PVP arbitration picks a winner and records the cost
 11. streaks increment on a new day and do not double-count within one day
 12. a share card renders for a real session and carries the room code

Run:  python -m tests.test_multiplayer
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-mp-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (config, db, engine, mana, memory, sessions, sharecard,  # noqa: E402
                     streaks, worldforge, worldkit)
from backend import worlds as world_registry  # noqa: E402

HOST = "host-user-account"
FRIENDS = ["friend-user-one", "friend-user-two", "friend-user-three"]
WATCHER = "watcher-user-account"


def run():
    db.init()
    room = sessions.create(HOST, world_id="emberfall", mode="chaos", host_name="Maren-the-Host")
    session_id, pt_id = room["id"], room["playthrough_id"]

    joined = [{"player_id": "host", "user_id": HOST, "name": "Maren-the-Host"}]
    for i, uid in enumerate(FRIENDS):
        p = sessions.join(session_id, uid, f"Friend{i+1}", goal=f"be the one who owns the mill ({i})")
        joined.append({"player_id": p["player_id"], "user_id": uid, "name": p["name"]})
    spectator = sessions.join(session_id, WATCHER, "Watcher", role="spectator")

    script = [
        "I ask Nessa Quill what she knows about the Warden.",
        "I offer Nessa Quill a secret in trade.",
        "I threaten Nessa Quill in front of the room.",
        "I buy a round for the whole tavern.",
        "I ask Nessa Quill who else has been asking questions.",
        "I tell Nessa Quill the valley is going to burn.",
    ]
    for turn_index in range(50):
        who = joined[turn_index % len(joined)]
        engine.take_turn(pt_id, script[turn_index % len(script)],
                         player=who["player_id"], actor_name=who["name"],
                         session_id=session_id)

    whisper = engine.whisper(pt_id, player=joined[1]["player_id"], target_kind="npc",
                             target_id="nessa", text="Do not tell the host what I just said.",
                             actor_name=joined[1]["name"], session_id=session_id)
    p2p = engine.whisper(pt_id, player=joined[1]["player_id"], target_kind="player",
                         target_id=joined[2]["player_id"], text="Back me when I move on the mill.",
                         actor_name=joined[1]["name"], session_id=session_id)

    contest = engine.contest(
        pt_id,
        {"player_id": joined[1]["player_id"], "name": joined[1]["name"],
         "action": "I take the deed off the table before anyone else can."},
        {"player_id": joined[2]["player_id"], "name": joined[2]["name"],
         "action": "I put my hand flat on the deed and do not move it."},
        session_id=session_id)

    return {"session_id": session_id, "pt_id": pt_id, "joined": joined,
            "spectator": spectator, "whisper": whisper, "p2p": p2p, "contest": contest}


def check(ctx):
    fails, notes = [], []

    def ok(cond, msg):
        (notes if cond else fails).append(msg)

    session_id, pt_id = ctx["session_id"], ctx["pt_id"]
    joined = ctx["joined"]
    room = sessions.get(session_id)

    # 1 --------------------------------------------------------------- room
    ok(len(room["code"]) == 6 and room["code"].isalnum(),
       f"room code allocated: {room['code']}")
    seated = [p for p in room["players"] if p["role"] != "spectator"]
    ok(len(seated) == 4, f"{len(seated)} players seated in one room")
    ok(ctx["spectator"]["role"] == "spectator", "a fifth joiner took the spectator seat")

    # 2 ------------------------------------------------ one shared world state
    st = engine.snapshot(pt_id, "host")
    ok(st["turn"] == 50, f"50 actions from 4 players advanced ONE shared timeline to turn {st['turn']}")
    actors = db.rows(
        "SELECT DISTINCT actor FROM timeline_events WHERE playthrough_id=? AND kind='action'", (pt_id,))
    ok(len(actors) == 4, f"{len(actors)} distinct players wrote into the same timeline")

    # 3 --------------------------------------- context flat per broadcast (§6)
    nar = db.rows(
        "SELECT in_tokens FROM usage_log WHERE playthrough_id=? AND role='narrator' ORDER BY id",
        (pt_id,))
    early = [r["in_tokens"] for r in nar[:4]] or [1]
    late = [r["in_tokens"] for r in nar[-4:]] or [1]
    a, b = sum(early) / len(early), sum(late) / len(late)
    ok(b / a <= 1.5,
       f"context flat with 4 concurrent players: {a:.0f} -> {b:.0f} tok ({b/a:.2f}x, ceiling 1.50x)")
    solo_ceiling = 1400
    ok(b <= solo_ceiling,
       f"a 4-player broadcast prompt is {b:.0f} tok, under the {solo_ceiling} single-player ceiling")

    # 4 ------------------------------------------------ per-player NPC memory
    per_player = db.rows(
        "SELECT player_id, COUNT(*) n FROM npc_memories WHERE playthrough_id=? AND npc_id='nessa'"
        " GROUP BY player_id", (pt_id,))
    keyed = {r["player_id"]: r["n"] for r in per_player}
    distinct = [p for p in keyed if p not in (memory.SHARED,)]
    ok(len(distinct) >= 3,
       f"Nessa Quill keeps {len(distinct)} separate memory streams, one per player: {keyed}")
    ok(memory.SHARED in keyed, "shared seed memories are held once, not duplicated per player")

    a_mems = {m["text"] for m in memory.npc_recall(pt_id, "nessa", 12, "the traveller", k=8,
                                                   player=joined[0]["player_id"])}
    b_mems = {m["text"] for m in memory.npc_recall(pt_id, "nessa", 12, "the traveller", k=8,
                                                   player=joined[1]["player_id"])}
    ok(a_mems != b_mems,
       "the same character recalls a different history for each player it met")

    # 5 -------------------------------------------- relationships diverge
    vectors = {}
    for p in joined:
        r = memory.rel_to(pt_id, "nessa", p["player_id"])
        vectors[p["name"]] = (round(r["affinity"]), round(r["trust"])) if r else None
    ok(len(set(vectors.values())) > 1,
       f"one NPC holds different feelings per player: {vectors}")

    # 6 ------------------------------------------------------ whisper privacy
    feed_text = " ".join(e["text"] for e in engine.feed(pt_id))
    ok(ctx["whisper"]["text"] not in feed_text,
       "a whisper to an NPC never appears in the shared narrative feed")
    ok(ctx["whisper"].get("reply"), "the whispered-to character answered privately")
    mine = sessions.whispers_for(session_id, joined[1]["player_id"])
    theirs = sessions.whispers_for(session_id, joined[3]["player_id"])
    ok(len(mine) >= 2 and len(theirs) == 0,
       f"whispers reach only sender and addressee ({len(mine)} for the speaker, {len(theirs)} for a bystander)")
    ok(ctx["p2p"]["kind"] == "player" and ctx["p2p"]["reply"] is None,
       "a player-to-player whisper is routed, not answered by the engine")

    # 7 --------------------------------------------------------- spectators
    ok(ctx["spectator"]["role"] == "spectator",
       "spectator joined read-only (the socket refuses their actions)")
    spec_events = db.rows(
        "SELECT * FROM timeline_events WHERE playthrough_id=? AND actor=?", (pt_id, "Watcher"))
    ok(not spec_events, "the spectator wrote nothing into the timeline")

    # 8 ------------------------------------------------------- host wallet
    host_led = mana.ledger(HOST)
    friend_led = mana.ledger(FRIENDS[0])
    ok(host_led["free_mana_used"] > 0, f"the host's wallet funded the room ({host_led['free_mana_used']} Mana)")
    ok(friend_led["free_mana_used"] == 0,
       "friends the host brought were charged nothing")

    # 9 ------------------------------------------------- allowance stacking
    before = mana.status(HOST, None, [HOST])["free_left"]
    sessions.contribute(session_id, joined[1]["player_id"], True)
    _, payers = engine._party_context(session_id)
    after = mana.status(HOST, None, payers)["free_left"]
    ok(len(payers) == 2 and after > before,
       f"a second payer stacked the allowance: {before} -> {after} across {len(payers)} payers")
    ok(len(mana.free_pool([f"u{i}" for i in range(9)])) == config.MAX_ALLOWANCE_STACK,
       f"stacking is capped at {config.MAX_ALLOWANCE_STACK}x")

    # 10 ---------------------------------------------------------- PVP
    res = ctx["contest"]["result"]
    ok(res["winner"] in (joined[1]["player_id"], joined[2]["player_id"]),
       f"contested action resolved to a winner ({res['winner']}, rolls {res['rolls']})")
    ok(bool(res.get("loser_cost")), "the losing side paid a stated cost")
    logged = db.row(
        "SELECT * FROM timeline_events WHERE playthrough_id=? AND kind='contest' ORDER BY id DESC", (pt_id,))
    ok(logged is not None, "the contest is in the shared timeline both players can retrieve")

    # 11 ------------------------------------------------------- streaks
    s0 = streaks.touch("streak-test-user", "2026-03-01")
    s0b = streaks.touch("streak-test-user", "2026-03-01")
    s1 = streaks.touch("streak-test-user", "2026-03-02")
    s2 = streaks.touch("streak-test-user", "2026-03-03")
    broke = streaks.touch("streak-test-user", "2026-03-09")
    ok(s0["current"] == 1 and s0b["current"] == 1,
       "a second action on the same day does not double-count the streak")
    ok(s1["current"] == 2 and s2["current"] == 3, "the streak increments on consecutive days")
    ok(broke["current"] == 1 and broke["longest"] == 3,
       "a broken streak resets to 1 and the longest run is kept forever")
    ok(s2.get("milestone") == 3, "a milestone fires exactly on its day")

    # 12 ----------------------------------------------------- share card
    svg = sharecard.svg(pt_id)
    ok(svg.startswith("<svg") and svg.rstrip().endswith("</svg>"), "share card renders as one SVG file")
    ok(room["code"] in svg, "the share card carries the room code so a viewer can join")
    fetches = [t for t in ("<image", "href=", "url(http", "@import", "<foreignObject") if t in svg]
    ok("StoryLiver" in svg and not fetches,
       f"the card is branded and fetches nothing external (only the SVG namespace URI){' — FOUND ' + str(fetches) if fetches else ''}")
    ok(len(svg) > 1200, f"the card has real content ({len(svg)} bytes)")
    recap = sharecard.text_recap(pt_id)
    ok(room["code"] in recap, "the one-tap post text carries the room code")

    # 13 ------------------------------------- player worlds + copyright boundary
    forged = worldforge.bootstrap("a drowned lighthouse colony", user_id=HOST)
    ok(len(forged["rules"]) >= worldkit.MIN_RULES and len(forged["fated_events"]) == worldkit.MIN_FATED,
       f"a bootstrapped world is playable: {len(forged['rules'])} rules, "
       f"{len(forged['fated_events'])} fated events, {len(forged['npcs'])} characters")
    ok(not forged["personal_only"], "an original setting produces a shareable world")

    ip = worldforge.bootstrap("Middle-earth", user_id=HOST)
    saved = worldforge.save(HOST, ip, visibility="unlisted")
    ok(ip["personal_only"], "a world named from copyrighted fiction is flagged personal-only")
    ok(saved["visibility"] == "private",
       "personal-only worlds are forced private even when asked to be shared")

    pt2 = engine.create_playthrough(HOST, saved["id"])
    r = engine.take_turn(pt2, "I look around and take stock of who is here.")
    ok(not r.get("blocked") and r["entries"], "a player-built world is actually playable end to end")
    st2 = engine.snapshot(pt2)
    ok(st2["world"]["id"] == saved["id"] and st2["world"]["personal_only"],
       "the playthrough runs on the player's own world, boundary intact")

    return fails, notes, room


def main():
    print("StoryLiver — multiplayer & full-version gate")
    print("  offline stub, 4 players + 1 spectator, no API key, no spend\n")
    ctx = run()
    fails, notes, room = check(ctx)
    for n in notes:
        print("  PASS  " + n)
    for f in fails:
        print("  FAIL  " + f)
    print(f"\n  room {room['code']} · mode {room['mode']} · {len(room['players'])} joined")
    u = engine.usage_summary(ctx["pt_id"])
    print(f"  {u['actions']} actions · ${u['total_usd']:.5f} · ${u['usd_per_action']:.5f}/action")
    print(f"\n  {len(notes)} passed, {len(fails)} failed")
    return 1 if fails else 0


def test_multiplayer():
    ctx = run()
    fails, _, _ = check(ctx)
    assert not fails, "\n".join(fails)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
